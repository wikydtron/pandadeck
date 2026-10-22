"""Usage meters for the AI page (all optional, off by default).

Claude ("token"): a 1-token request to the Messages API with the user's
    `claude setup-token` OAuth token; the reply's rate-limit headers carry the
    5-hour and weekly utilisation (0..1 fractions) and reset times (unix ts).
Codex ("auth"): the login the Codex CLI saved in ~/.codex/auth.json, against
    chatgpt.com/backend-api/wham/usage (redirects refused).
"url": a usage daemon on your network that serves the JSON read below.

Output = the `usage` block the firmware parses (model.cpp parse_usage):
    {"ok", "s5h", "s5h_reset", "week", "week_reset", "codex_week", "codex_reset"}
Percentages 0..100, resets in seconds from now, -1 = unknown.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Callable

from ..paths import codex_dir
from ..util import Loop, http_json, http_request

log = logging.getLogger("usage")

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
CLAUDE_HEADERS = {
    "anthropic-version": "2023-06-01",
    "anthropic-beta": "oauth-2025-04-20",
    "User-Agent": "claude-code/2.1.5",
    "Content-Type": "application/json",
}
CLAUDE_BODY = {"model": "claude-haiku-4-5", "max_tokens": 1, "messages": [{"role": "user", "content": "."}]}
H_PREFIX = "anthropic-ratelimit-unified-"
CODEX_URL = "https://chatgpt.com/backend-api/wham/usage"
FIVE_H = 18000
WEEK = 604800

EMPTY = {"ok": True, "s5h": -1, "s5h_reset": -1, "week": -1, "week_reset": -1, "codex_week": -1, "codex_reset": -1}


def _num(v, default=-1.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _reset_in(ts, now: float) -> int:
    t = _num(ts, -1)
    if t <= 0:
        return -1
    return max(0, int(t - now))


# ---- Claude ---------------------------------------------------------------------------

def parse_claude_headers(headers, now: float) -> dict:
    """Pure: rate-limit headers -> s5h/week (+ resets). Raises ValueError if absent."""
    def h(name):
        v = headers.get(H_PREFIX + name)
        return v if v not in (None, "") else None

    s5h, week = h("5h-utilization"), h("7d-utilization")
    if s5h is None and week is None:
        raise ValueError("no rate-limit headers in the reply")
    return {
        "s5h": round(_num(s5h) * 100, 1) if s5h is not None else -1,
        "s5h_reset": _reset_in(h("5h-reset"), now),
        "week": round(_num(week) * 100, 1) if week is not None else -1,
        "week_reset": _reset_in(h("7d-reset"), now),
    }


def fetch_claude(token: str, now: Callable[[], float] = time.time, request=http_request) -> dict:
    status, headers, body = request(ANTHROPIC_URL, method="POST", body=json.dumps(CLAUDE_BODY).encode(),
                                    headers={**CLAUDE_HEADERS, "Authorization": f"Bearer {token}"},
                                    timeout=15, follow_redirects=False)
    if status == 401:
        raise PermissionError("Claude token rejected: connect Claude again")
    try:
        return parse_claude_headers(headers or {}, now())
    except ValueError:
        detail = ""
        try:
            detail = (json.loads(body or b"{}").get("error") or {}).get("message", "")
        except (ValueError, AttributeError):
            pass
        raise ValueError(f"Claude usage unavailable (HTTP {status}{': ' + detail[:80] if detail else ''})") from None


# ---- Codex ------------------------------------------------------------------------------

def codex_auth_path(home: Path | None = None) -> Path:
    return (home / ".codex" if home else codex_dir()) / "auth.json"


def read_codex_auth(path: Path | None = None) -> tuple[str, str] | None:
    """(access_token, account_id) from the Codex CLI's login, or None."""
    try:
        data = json.loads((path or codex_auth_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    tokens = (data or {}).get("tokens") or {}
    tok, acct = tokens.get("access_token"), tokens.get("account_id")
    if isinstance(tok, str) and tok and isinstance(acct, str) and acct:
        return tok, acct
    return None


def parse_codex_usage(data: dict, now: float) -> dict:
    """Pure: wham/usage JSON -> codex_week / codex_reset (+ 5 h window)."""
    rl = (data or {}).get("rate_limit") or {}
    out = {"codex_week": -1, "codex_reset": -1}
    windows = [w for w in (rl.get("primary_window"), rl.get("secondary_window")) if isinstance(w, dict)]
    if not windows:
        raise ValueError("no rate-limit windows in the Codex reply")

    def reset(w) -> int:
        if w.get("reset_at") is not None:
            return _reset_in(w.get("reset_at"), now)
        r = _num(w.get("reset_after_seconds"), -1)
        return int(r) if r >= 0 else -1

    weekly = next((w for w in windows if int(_num(w.get("limit_window_seconds"), 0)) == WEEK), None)
    if weekly is None:  # plans with a single window: use the longest one
        weekly = max(windows, key=lambda w: _num(w.get("limit_window_seconds"), 0))
    out["codex_week"] = round(_num(weekly.get("used_percent")), 1)
    out["codex_reset"] = reset(weekly)
    return out


def fetch_codex(auth_path: Path | None = None, now: Callable[[], float] = time.time, get=http_json) -> dict:
    auth = read_codex_auth(auth_path)
    if not auth:
        raise FileNotFoundError("no Codex login found: run `codex login` first")
    tok, acct = auth
    status, data = get(CODEX_URL, headers={"Authorization": f"Bearer {tok}", "ChatGPT-Account-Id": acct,
                                           "User-Agent": "pandadeck"}, timeout=15, follow_redirects=False)
    if status in (401, 403):
        raise PermissionError("Codex login expired: run `codex login` again")
    if status != 200 or not isinstance(data, dict):
        raise ValueError(f"Codex usage unavailable (HTTP {status})")
    return parse_codex_usage(data, now())


# ---- "url" mode: a usage daemon's JSON ------------------------------------------------------

def parse_claude_daemon(c: dict) -> dict:
    if not c or not c.get("ok"):
        raise ValueError("Claude usage daemon reports not ok")
    return {"s5h": c.get("session_pct", -1), "s5h_reset": c.get("session_reset_s", -1),
            "week": c.get("weekly_pct", -1), "week_reset": c.get("weekly_reset_s", -1)}


def parse_codex_daemon(x: dict) -> dict:
    wk = (x or {}).get("seven_day") or {}
    if not wk:
        raise ValueError("Codex usage daemon has no seven_day block")
    return {"codex_week": wk.get("used", -1), "codex_reset": wk.get("reset_s", -1)}


def _get_json(url: str, get=http_json) -> dict:
    status, data = get(url, timeout=6)
    if status != 200 or not isinstance(data, dict):
        raise ValueError(f"{url} answered HTTP {status}")
    return data


# ---- collector ----------------------------------------------------------------------------

class UsageCollector(Loop):
    """Polls the enabled sources; snapshot() is None while everything is off."""

    def __init__(self, settings, secrets, *, claude=fetch_claude, codex=fetch_codex, get=http_json,
                 clock: Callable[[], float] = time.time):
        super().__init__("usage", 60.0)
        self.settings, self.secrets = settings, secrets
        self._claude, self._codex, self._get, self._clock = claude, codex, get, clock
        self._data: dict = {}
        self.status: dict = {"claude": {"state": "off", "error": "", "at": 0}, "codex": {"state": "off", "error": "", "at": 0}}

    def next_interval(self) -> float:
        return float((self.settings.section("ai") or {}).get("usage_poll_s", 60))

    def step(self) -> None:
        ai = self.settings.section("ai") or {}
        data: dict = {}
        data.update(self._one("claude", ai.get("claude_usage", "off"), ai))
        data.update(self._one("codex", ai.get("codex_usage", "off"), ai))
        self._data = data

    def _one(self, name: str, mode: str, ai: dict) -> dict:
        st = self.status[name]
        if mode == "off":
            self.status[name] = {"state": "off", "error": "", "at": 0}
            return {}
        try:
            if name == "claude":
                if mode == "token":
                    token = self.secrets.get("claude_token")
                    if not token:
                        raise PermissionError("not connected yet: click Connect Claude")
                    out = self._claude(token)
                else:
                    out = parse_claude_daemon(_get_json(ai.get("claude_usage_url") or "", self._get))
            else:
                if mode == "auth":
                    out = self._codex()
                else:
                    out = parse_codex_daemon(_get_json(ai.get("codex_usage_url") or "", self._get))
            now = self._clock()
            self.status[name] = {"state": "ok", "error": "", "at": now, "ok_at": now, "values": out}
            return out
        except Exception as e:  # noqa: BLE001 - shown in the settings page, never fatal
            msg = str(e) or type(e).__name__
            if st.get("error") != msg:
                log.info("%s usage: %s", name, msg)
            prev, ok_at = st.get("values"), st.get("ok_at", 0)
            self.status[name] = {"state": "error", "error": msg, "at": self._clock(), "ok_at": ok_at,
                                 "values": prev}
            # Keep showing the last good numbers for a while (a blip shouldn't blank the meters).
            return prev if prev and self._clock() - ok_at < 900 else {}

    def snapshot(self) -> dict | None:
        if not self._data:
            return None
        return {**EMPTY, **self._data}
