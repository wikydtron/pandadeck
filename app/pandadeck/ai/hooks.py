"""Install / remove the Claude Code and Codex hooks that feed the AI page.

Claude Code: `type: http` hooks in ~/.claude/settings.json posting to
    http://127.0.0.1:<port>/pandadeck/<hook_secret>/hook
Codex: command hooks in ~/.codex/hooks.json that pipe the hook JSON through
    curl to .../codex (curl.exe on Windows, curl elsewhere).

Both files are backed up before every change, merged (other hooks are kept)
and written atomically. "Ours" = a hook whose URL/command targets
127.0.0.1 and contains /pandadeck/, so removing takes out exactly what we added.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import shutil
import stat
import sys
from pathlib import Path

from ..paths import claude_dir, codex_dir
from ..util import write_json_atomic

log = logging.getLogger("hooks")

HOOK_MARK = "/pandadeck/"
# Kept deliberately small: on Windows a refused localhost connection costs ~2 s,
# so every registered event is latency the agent pays if the app is ever down.
CLAUDE_EVENTS = ["SessionStart", "UserPromptSubmit", "PreToolUse", "Notification", "Stop", "SessionEnd"]
ALL_KNOWN_EVENTS = CLAUDE_EVENTS + ["PermissionRequest", "PostToolUse", "PostToolUseFailure", "StopFailure"]
CODEX_EVENTS = ["SessionStart", "UserPromptSubmit", "PreToolUse", "Stop", "SessionEnd"]
WINDOWS_CURL = r"C:\Windows\System32\curl.exe"
KEEP_BACKUPS = 5


def claude_settings_path(home: Path | None = None) -> Path:
    return (home / ".claude" if home else claude_dir()) / "settings.json"


def codex_hooks_path(home: Path | None = None) -> Path:
    return (home / ".codex" if home else codex_dir()) / "hooks.json"


def _target(path: Path) -> Path:
    """Write through symlinks (dotfile managers) instead of replacing them."""
    return path.resolve() if path.is_symlink() else path


def hook_url(port: int, secret: str, kind: str = "hook") -> str:
    return f"http://127.0.0.1:{port}/pandadeck/{secret}/{kind}"


def _is_ours_text(text: str) -> bool:
    return "127.0.0.1" in text and HOOK_MARK in text


def _is_ours(h: dict) -> bool:
    return _is_ours_text(str(h.get("url", "")) + str(h.get("command", "")) + str(h.get("commandWindows", "")))


def _strip(hooks: dict) -> dict:
    if not isinstance(hooks, dict):
        raise ValueError("unexpected \"hooks\" format (not an object); leaving the file alone")
    out = {}
    for ev, groups in hooks.items():
        if not isinstance(groups, list):
            raise ValueError(f"unexpected hooks format for {ev!r}; leaving the file alone")
        kept_groups = []
        for g in groups or []:
            if not isinstance(g, dict):
                kept_groups.append(g)
                continue
            kept = [h for h in g.get("hooks", []) if not (isinstance(h, dict) and _is_ours(h))]
            if kept:
                kept_groups.append({**g, "hooks": kept})
        if kept_groups:
            out[ev] = kept_groups
    return out


def _read(path: Path) -> dict:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return {}
    data = json.loads(text)  # a broken file is the user's: refuse instead of overwriting it
    if not isinstance(data, dict):
        raise ValueError(f"{path} isn't a JSON object")
    return data


def _backup(path: Path) -> str:
    if not path.exists():
        return ""
    name = f"{path.name}.bak-pandadeck-{dt.datetime.now().strftime('%Y%m%d-%H%M%S-%f')}"
    shutil.copy2(path, path.with_name(name))
    olds = sorted(path.parent.glob(f"{path.name}.bak-pandadeck-*"))
    for old in olds[:-KEEP_BACKUPS]:
        try:
            old.unlink()
        except OSError:
            pass
    return name


def _write(path: Path, data: dict) -> None:
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else None
    write_json_atomic(path, data, private=False, ensure_ascii=False)
    if mode is not None and os.name == "posix":
        os.chmod(path, mode)


def _apply(path: Path, new_hooks: dict, data: dict) -> dict:
    out = dict(data)
    if new_hooks:
        out["hooks"] = new_hooks
    else:
        out.pop("hooks", None)
    if out == data:
        return {"changed": False, "backup": "", "path": str(path)}
    backup = _backup(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _write(path, out)
    return {"changed": True, "backup": backup, "path": str(path)}


def install_claude(port: int, secret: str, perm_wait_s: int, uninstall: bool = False,
                   path: Path | None = None) -> dict:
    path = _target(path or claude_settings_path())
    data = _read(path)
    hooks = _strip(data.get("hooks") if data.get("hooks") is not None else {})
    if not uninstall:
        url = hook_url(port, secret, "hook")
        for ev in CLAUDE_EVENTS:
            # Short timeout: fire-and-forget; a dead app must never slow Claude down.
            hooks.setdefault(ev, []).append({"hooks": [{"type": "http", "url": url, "timeout": 3}]})
        hooks.setdefault("PermissionRequest", []).append(
            {"hooks": [{"type": "http", "url": url, "timeout": int(perm_wait_s) + 15}]})
    res = _apply(path, hooks, data)
    if res["changed"]:
        log.info("%s PandaDeck hooks in %s", "removed" if uninstall else "installed", path)
    return res


def curl_command(port: int, secret: str, max_s: int, windows: bool) -> str:
    curl = WINDOWS_CURL if windows else "curl"
    return (f'{curl} -s --connect-timeout 1 -m {max_s} -H "Content-Type: application/json" '
            f'--data-binary "@-" {hook_url(port, secret, "codex")}')


def install_codex(port: int, secret: str, perm_wait_s: int, uninstall: bool = False,
                  path: Path | None = None) -> dict:
    path = _target(path or codex_hooks_path())
    data = _read(path)
    hooks = _strip(data.get("hooks") if data.get("hooks") is not None else {})
    if not uninstall:
        def entry(max_s: int, timeout: int) -> dict:
            return {"type": "command", "command": curl_command(port, secret, max_s, windows=False),
                    "commandWindows": curl_command(port, secret, max_s, windows=True), "timeout": timeout}
        for ev in CODEX_EVENTS:
            max_s = 2 if ev == "SessionEnd" else 3  # SessionEnd hooks are capped at 3 s
            hooks.setdefault(ev, []).append({"hooks": [entry(max_s, max_s + 1)]})
        hooks.setdefault("PermissionRequest", []).append(
            {"hooks": [entry(int(perm_wait_s) + 5, int(perm_wait_s) + 15)]})
    res = _apply(path, hooks, data)
    if res["changed"]:
        log.info("%s PandaDeck Codex hooks in %s", "removed" if uninstall else "installed", path)
    return res


def installed_urls(path: Path) -> list[str]:
    """Our hook targets currently in a settings/hooks file (for status + port checks)."""
    try:
        data = _read(path)
    except (OSError, ValueError):
        return []
    out = []
    for groups in (data.get("hooks") or {}).values():
        for g in groups or []:
            for h in (g.get("hooks", []) if isinstance(g, dict) else []):
                if isinstance(h, dict) and _is_ours(h):
                    text = str(h.get("url") or h.get("command") or "")
                    out.append(text.split()[-1] if " " in text else text)
    return out


def status(port: int, secret: str, home: Path | None = None) -> dict:
    """What the AI section shows: CLI present, hooks present, pointing at us."""
    out = {}
    for name, path, kind in (("claude", claude_settings_path(home), "hook"), ("codex", codex_hooks_path(home), "codex")):
        urls = installed_urls(path)
        want = hook_url(port, secret, kind)
        out[name] = {
            "cli": bool(shutil.which(name)),
            "config_dir": path.parent.exists(),
            "installed": bool(urls),
            "current": bool(urls) and all(u == want for u in urls),
            "path": str(path),
        }
    out["windows"] = sys.platform == "win32"
    return out
