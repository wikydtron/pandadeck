"""Claude Code / Codex hook bridge.

Claude Code POSTs every hook event (type "http" hooks) to the app's local
server; Codex pipes the same JSON through curl. Ordinary events update a
session state machine; PermissionRequest blocks until the panel answers (or
the request is answered at the PC / times out), then returns the decision JSON.

State model (per session):
    idle  - open, nothing happening
    work  - the agent is running (prompt submitted / tools running)
    wait  - turn finished or idle prompt: your move
    perm  - blocked on a permission prompt
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import selectors
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from .summarize import fits_panel, model_short, one_line, permission_detail_raw, project_name, tool_activity, \
    tool_label

log = logging.getLogger("bridge")

# Tools that go through the permission flow but are really prompts for the
# user (answered in the app, not with allow/deny).
INTERACTIVE_TOOLS = {
    "AskUserQuestion": "Question for you (answer in the app)",
    "ExitPlanMode": "Plan ready for your review",
    "EnterPlanMode": "Wants to switch to plan mode",
}

SESSION_TTL_S = 3 * 3600        # forget sessions with no events for 3h
WAIT_TO_IDLE_S = 45 * 60        # stop nagging about a finished turn after 45m
MAX_BODY = 16 * 1024 * 1024


@dataclass
class Session:
    sid: str
    proj: str = "session"
    title: str = ""
    act: str = ""
    st: str = "idle"
    model: str = ""
    host: str = ""
    src: str = "claude"  # "claude" | "codex"
    started: float = 0.0
    last: float = 0.0


@dataclass
class Perm:
    pid: str
    sid: str
    proj: str
    host: str
    tool: str
    detail: str
    created: float
    src: str = "claude"
    verdict: str | None = None
    done: threading.Event = field(default_factory=threading.Event)


class Bridge:
    def __init__(self, host: str, clock: Callable[[], float] = time.time):
        self.host = host
        self._clock = clock
        self._lock = threading.Lock()
        self._sessions: dict[str, Session] = {}
        self._perms: dict[str, Perm] = {}
        self.changed = threading.Event()
        self.last_event_at = {"claude": 0.0, "codex": 0.0}
        self.ignore_dirs: list[str] = []  # our own CLI runs (✨ Generate with AI): never on the AI page

    def ignored(self, payload: dict) -> bool:
        cwd = payload.get("cwd")
        if not isinstance(cwd, str) or not cwd or not self.ignore_dirs:
            return False
        here = os.path.normcase(os.path.abspath(cwd))
        for d in self.ignore_dirs:
            root = os.path.normcase(os.path.abspath(d))
            if here == root or here.startswith(root.rstrip("\\/") + os.sep):
                return True
        return False

    # ---- helpers (call with lock held) --------------------------------------
    @staticmethod
    def _sid(payload: dict) -> str:
        sid = str(payload.get("session_id") or "unknown")
        # Codex and Claude ids live in different namespaces; never collide.
        return ("cx-" + sid) if payload.get("_src") == "codex" else sid

    def _session(self, payload: dict) -> Session:
        sid = self._sid(payload)
        now = self._clock()
        s = self._sessions.get(sid)
        if s is None:
            s = Session(sid=sid, host=self.host, started=now, src=payload.get("_src") or "claude")
            self._sessions[sid] = s
        cwd = payload.get("cwd")
        if cwd:
            s.proj = project_name(cwd)
        if payload.get("session_title"):
            s.title = one_line(payload["session_title"], 78)
        s.last = now
        return s

    def _cancel_perms_for(self, sid: str, why: str) -> None:
        for p in list(self._perms.values()):
            if p.sid == sid and p.verdict is None:
                log.info("perm %s abandoned (%s)", p.pid, why)
                p.verdict = "abandoned"
                p.done.set()

    # ---- events -------------------------------------------------------------
    def handle_event(self, payload: dict) -> None:
        ev = payload.get("hook_event_name", "")
        with self._lock:
            self.last_event_at[payload.get("_src") or "claude"] = self._clock()
            if ev == "SessionEnd":
                sid = self._sid(payload)
                self._cancel_perms_for(sid, "session end")
                self._sessions.pop(sid, None)
                self.changed.set()
                return
            s = self._session(payload)
            cwd = payload.get("cwd")
            if ev == "SessionStart":
                if payload.get("model"):
                    s.model = model_short(payload["model"])
                if s.st not in ("work", "perm"):
                    s.st = "idle"
                    src = payload.get("source", "startup")
                    s.act = "Ready" if src == "startup" else f"Ready ({src})"
            elif ev == "UserPromptSubmit":
                s.st = "work"
                s.act = one_line("> " + str(payload.get("prompt", "")))
            elif ev == "PreToolUse":
                s.st = "work"
                s.act = tool_activity(payload.get("tool_name", ""), payload.get("tool_input"), cwd)
            elif ev in ("PostToolUse", "PostToolUseFailure"):
                # The tool ran, so any permission prompt for it was answered
                # somewhere (likely at the PC).
                self._cancel_perms_for(s.sid, ev)
                s.st = "work"
            elif ev == "Notification":
                ntype = payload.get("notification_type", "")
                msg = one_line(payload.get("message", ""))
                if ntype == "permission_prompt":
                    if s.st != "perm":
                        s.st = "perm"
                        s.act = msg or "Needs permission"
                elif ntype in ("idle_prompt", "agent_needs_input", "elicitation_dialog"):
                    s.st = "wait"
                    s.act = msg or "Waiting for you"
                elif msg:
                    s.act = msg
            elif ev in ("Stop", "StopFailure"):
                self._cancel_perms_for(s.sid, ev)
                s.st = "wait"
                last = one_line(str(payload.get("last_assistant_message") or ""))
                if ev == "StopFailure":
                    s.act = "Stopped with an error"
                else:
                    s.act = ("Done: " + last) if last else "Your turn"
            self.changed.set()

    # ---- permissions -------------------------------------------------------------
    def open_perm(self, payload: dict) -> Perm | None:
        """Opens an approval card, or returns None when the request must be
        answered at the PC (too long / non-ASCII to show faithfully, or an
        interactive prompt that isn't a yes/no permission)."""
        with self._lock:
            self.last_event_at[payload.get("_src") or "claude"] = self._clock()
            s = self._session(payload)
            tool = payload.get("tool_name", "")
            if tool in INTERACTIVE_TOOLS:
                # The agent is asking *you* something (questions, plan review): the
                # panel can't answer that, so flag the session instead of a takeover.
                s.st = "wait"
                s.act = one_line(INTERACTIVE_TOOLS[tool])
                self.changed.set()
                return None
            raw = permission_detail_raw(tool, payload.get("tool_input"), payload.get("cwd"))
            s.st = "perm"
            if not fits_panel(raw):
                s.act = one_line(f"Approve at PC: {tool_label(tool)} (too long for the panel)")
                self.changed.set()
                log.info("perm for %s in %s left to the PC dialog (doesn't fit the panel)", tool, s.proj)
                return None
            p = Perm(pid=secrets.token_hex(4), sid=s.sid, proj=s.proj, host=self.host, tool=tool_label(tool),
                     detail=raw, created=self._clock(), src=s.src)
            self._perms[p.pid] = p
            s.act = one_line(f"Needs permission: {p.tool}")
            self.changed.set()
            log.info("perm %s opened: %s in %s", p.pid, p.tool, p.proj)
            return p

    def wait_perm(self, p: Perm, timeout_s: float, alive: Callable[[], bool],
                  panel_alive: Callable[[], bool] = lambda: True) -> str | None:
        """Blocks until decided / abandoned / timeout / client gone / panel gone.
        Returns 'allow' | 'deny' | None (no decision: let the agent ask at the PC)."""
        deadline = self._clock() + timeout_s
        outcome = "decided"
        try:
            while not p.done.wait(0.4):
                if self._clock() >= deadline:
                    outcome = "timeout"
                    log.info("perm %s timed out", p.pid)
                    return None
                if not alive():
                    outcome = "pc"
                    log.info("perm %s: the agent closed the request (answered at PC?)", p.pid)
                    return None
                if not panel_alive():
                    outcome = "panel_down"
                    log.info("perm %s: panel unreachable, handing back to the PC dialog", p.pid)
                    return None
            return p.verdict if p.verdict in ("allow", "deny") else None
        finally:
            with self._lock:
                self._perms.pop(p.pid, None)
                s = self._sessions.get(p.sid)
                if s and s.st == "perm" and not any(x.sid == p.sid for x in self._perms.values()):
                    if p.verdict == "allow":
                        s.st, s.act = "work", one_line(f"Approved: {p.tool}")
                    elif p.verdict == "deny":
                        s.st, s.act = "work", one_line(f"Denied: {p.tool}")
                    elif outcome == "pc":
                        s.st, s.act = "work", one_line(f"Answered at PC: {p.tool}")
                    elif outcome == "panel_down":
                        s.act = one_line(f"Approve at PC: {p.tool}")
                self.changed.set()

    def decide(self, pid: str, verdict: str) -> bool:
        if verdict not in ("allow", "deny"):
            return False
        with self._lock:
            p = self._perms.get(pid)
            if not p or p.verdict is not None:
                return False
            p.verdict = verdict
            p.done.set()
            log.info("perm %s decided on panel: %s", pid, verdict)
            return True

    # ---- snapshot ------------------------------------------------------------------
    def snapshot(self) -> tuple[list[dict], list[dict]]:
        now = self._clock()
        with self._lock:
            for sid in [k for k, s in self._sessions.items() if now - s.last > SESSION_TTL_S]:
                del self._sessions[sid]
            for s in self._sessions.values():
                if s.st == "wait" and now - s.last > WAIT_TO_IDLE_S:
                    s.st = "idle"
            sessions = sorted(self._sessions.values(), key=lambda s: s.last, reverse=True)[:8]
            out_s = [{"id": s.sid[:12], "proj": s.proj, "title": s.title, "act": s.act, "host": s.host,
                      "model": s.model, "st": s.st, "src": s.src, "age": int(now - s.last),
                      "dur": int(now - s.started)} for s in sessions]
            perms = sorted(self._perms.values(), key=lambda p: p.created)[:4]
            out_p = [{"id": p.pid, "sid": p.sid[:12], "proj": p.proj, "host": p.host, "tool": p.tool,
                      "detail": p.detail, "age": int(now - p.created), "src": p.src}
                     for p in perms if p.verdict is None]
            return out_s, out_p

    def has_pending_perms(self) -> bool:
        with self._lock:
            return any(p.verdict is None for p in self._perms.values())

    # ---- one hook request (called by the web server) -------------------------------------
    def handle_hook(self, payload, src: str, client_alive: Callable[[], bool], perm_wait_s: float,
                    panel_alive: Callable[[], bool]) -> bytes:
        """Returns the HTTP body for the hook reply (b"" = no opinion)."""
        if not isinstance(payload, dict) or self.ignored(payload):
            return b""
        payload["_src"] = src
        if payload.get("hook_event_name") == "PermissionRequest":
            p = self.open_perm(payload)
            verdict = None
            if p is not None and panel_alive():
                verdict = self.wait_perm(p, perm_wait_s, client_alive, panel_alive)
            elif p is not None:
                self.wait_perm(p, 0, lambda: True)  # panel down: close the card, the PC decides
            return decision_json(verdict) if verdict else b""
        try:
            self.handle_event(payload)
        except Exception:  # noqa: BLE001 - a bridge bug must never block the agent
            log.exception("event handling failed")
        return b""


def socket_alive(sock: socket.socket) -> bool:
    """False once the client (Claude Code / curl) has closed/aborted the request."""
    try:
        # selectors, not select.select: that one fails for fds >= 1024 on Linux/macOS
        with selectors.DefaultSelector() as sel:
            sel.register(sock, selectors.EVENT_READ)
            readable = sel.select(0)
        if not readable:
            return True
        return sock.recv(1, socket.MSG_PEEK) != b""
    except (OSError, ValueError, KeyError):
        return False


def decision_json(verdict: str) -> bytes:
    if verdict == "allow":
        decision = {"behavior": "allow"}
    else:
        decision = {"behavior": "deny", "message": "Denied from the PandaDeck panel."}
    return json.dumps({"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": decision}}).encode()
