"""Per-OS folders (no extra dependencies).

    config  Windows %APPDATA%\\PandaDeck
            macOS   ~/Library/Application Support/PandaDeck
            Linux   $XDG_CONFIG_HOME/pandadeck (default ~/.config/pandadeck)
    cache   %LOCALAPPDATA%\\PandaDeck\\Cache | ~/Library/Caches/PandaDeck | $XDG_CACHE_HOME/pandadeck
    logs    %LOCALAPPDATA%\\PandaDeck\\Logs  | ~/Library/Logs/PandaDeck   | $XDG_STATE_HOME/pandadeck

PANDADECK_HOME puts everything under one folder (tests, portable installs).
PANDADECK_USER_HOME replaces the user's home when looking for ~/.claude and
~/.codex (tests and dry runs never touch the real ones).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

APP_DIR_NAME = "PandaDeck"
APP_DIR_NAME_LINUX = "pandadeck"


def _env_path(name: str) -> Path | None:
    v = os.environ.get(name, "").strip()
    return Path(v).expanduser() if v else None


def _home() -> Path:
    return Path.home()


def config_dir(platform: str | None = None) -> Path:
    override = _env_path("PANDADECK_HOME")
    if override:
        return override
    plat = platform or sys.platform
    if plat == "win32":
        base = _env_path("APPDATA") or (_home() / "AppData" / "Roaming")
        return base / APP_DIR_NAME
    if plat == "darwin":
        return _home() / "Library" / "Application Support" / APP_DIR_NAME
    base = _env_path("XDG_CONFIG_HOME") or (_home() / ".config")
    return base / APP_DIR_NAME_LINUX


def cache_dir(platform: str | None = None) -> Path:
    override = _env_path("PANDADECK_HOME")
    if override:
        return override / "cache"
    plat = platform or sys.platform
    if plat == "win32":
        base = _env_path("LOCALAPPDATA") or (_home() / "AppData" / "Local")
        return base / APP_DIR_NAME / "Cache"
    if plat == "darwin":
        return _home() / "Library" / "Caches" / APP_DIR_NAME
    base = _env_path("XDG_CACHE_HOME") or (_home() / ".cache")
    return base / APP_DIR_NAME_LINUX


def log_dir(platform: str | None = None) -> Path:
    override = _env_path("PANDADECK_HOME")
    if override:
        return override / "logs"
    plat = platform or sys.platform
    if plat == "win32":
        base = _env_path("LOCALAPPDATA") or (_home() / "AppData" / "Local")
        return base / APP_DIR_NAME / "Logs"
    if plat == "darwin":
        return _home() / "Library" / "Logs" / APP_DIR_NAME
    base = _env_path("XDG_STATE_HOME") or (_home() / ".local" / "state")
    return base / APP_DIR_NAME_LINUX


def user_home() -> Path:
    """Where ~/.claude and ~/.codex live."""
    return _env_path("PANDADECK_USER_HOME") or _home()


def claude_dir() -> Path:
    """Claude Code's config folder (CLAUDE_CONFIG_DIR, else ~/.claude)."""
    if not _env_path("PANDADECK_USER_HOME") and _env_path("CLAUDE_CONFIG_DIR"):
        return _env_path("CLAUDE_CONFIG_DIR")
    return user_home() / ".claude"


def codex_dir() -> Path:
    """Codex's config folder (CODEX_HOME, else ~/.codex)."""
    if not _env_path("PANDADECK_USER_HOME") and _env_path("CODEX_HOME"):
        return _env_path("CODEX_HOME")
    return user_home() / ".codex"


def settings_file() -> Path:
    return config_dir() / "settings.json"


def secrets_file() -> Path:
    return config_dir() / "secrets.json"


def runtime_file() -> Path:
    """Port + pid of the running instance (read by --settings and the tray)."""
    return config_dir() / "runtime.json"


def _homes() -> list[str]:
    out = []
    for h in (str(_home()), str(user_home())):
        for v in (h, h.replace("\\", "/")):
            if v and v not in out and len(v) > 3:
                out.append(v)
    return sorted(out, key=len, reverse=True)


def display(path) -> str:
    """A path for the settings page: the home folder shown as ~ (no user names
    in screenshots or bug reports). The real path stays server-side."""
    text = str(path)
    for h in _homes():
        if text.lower().startswith(h.lower()) and (len(text) == len(h) or text[len(h)] in "\\/"):
            rest = text[len(h):].replace("\\", "/").lstrip("/")
            return "~/" + rest if rest else "~"
    return text.replace("\\", "/") if sys.platform == "win32" else text


def scrub(text: str) -> str:
    """Replace home-folder paths inside a message (errors shown in the UI)."""
    text = str(text or "")
    for h in _homes():
        start = 0
        while True:
            idx = text.lower().find(h.lower(), start)
            if idx < 0:
                break
            end = idx + len(h)
            if end == len(text) or not (text[end].isalnum() or text[end] in "._-"):
                text = text[:idx] + "~" + text[end:]
                start = idx + 1
            else:
                start = end
    return text


def ensure_dirs() -> None:
    for d in (config_dir(), cache_dir(), log_dir()):
        d.mkdir(parents=True, exist_ok=True)
