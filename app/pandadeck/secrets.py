"""secrets.json: tokens the app needs but the settings page must never read.

    panel_token   the panel's own token (handed over USB in the hello reply)
    hook_secret   random path segment for the Claude/Codex hook endpoints
    claude_token  `claude setup-token` OAuth token (usage meters)
    lab_token     token for an optional pandadeck-lab homelab server
    spotify       {"refresh_token", "access_token", "expires_at", "client_id"}

Stored 0600 where the OS supports it, written atomically. The web API can
set or clear a value and ask whether one is set; it can never read one back
(the panel token is the one documented exception: "Show token").
"""
from __future__ import annotations

import copy
import secrets as _stdlib_secrets
import threading
from pathlib import Path

from .util import load_json_file, write_json_atomic

USER_SETTABLE = ("claude_token", "lab_token", "panel_token")
MAX_SECRET_LEN = 4096


class SecretStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        data = load_json_file(self.path, {})
        self._data: dict = data if isinstance(data, dict) else {}

    def get(self, name: str, default=None):
        with self._lock:
            return copy.deepcopy(self._data.get(name, default))

    def has(self, name: str) -> bool:
        with self._lock:
            return bool(self._data.get(name))

    def set(self, name: str, value) -> None:
        with self._lock:
            if value in (None, "", {}):
                self._data.pop(name, None)
            else:
                self._data[name] = copy.deepcopy(value)
            write_json_atomic(self.path, self._data, private=True)

    def clear(self, name: str) -> None:
        self.set(name, None)

    def hook_secret(self) -> str:
        """Random path segment for the hook endpoints, created once."""
        with self._lock:
            s = self._data.get("hook_secret")
            if not s:
                s = _stdlib_secrets.token_hex(12)
                self._data["hook_secret"] = s
                write_json_atomic(self.path, self._data, private=True)
            return s

    def presence(self) -> dict[str, bool]:
        """What the settings page may know: which secrets are set."""
        with self._lock:
            return {
                "claude_token": bool(self._data.get("claude_token")),
                "lab_token": bool(self._data.get("lab_token")),
                "panel_token": bool(self._data.get("panel_token")),
                "spotify": bool((self._data.get("spotify") or {}).get("refresh_token")),
            }


def validate_user_secret(name: str, value) -> str:
    """For POST /api/secret: only known names, plain printable text."""
    if name not in USER_SETTABLE:
        raise ValueError("unknown secret")
    if not isinstance(value, str):
        raise ValueError("value must be text")
    value = value.strip()
    if len(value) > MAX_SECRET_LEN or any(ord(c) < 33 or ord(c) > 126 for c in value):
        raise ValueError("that doesn't look like a token (no spaces or line breaks)")
    return value
