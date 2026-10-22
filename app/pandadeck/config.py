"""settings.json: load (with defaults + validation), atomic save, partial updates.

Secrets never live here (see secrets.py). Every value is validated on load
(bad values fall back to the default) and on update from the settings page
(bad values are rejected with a readable message).
"""
from __future__ import annotations

import copy
import logging
import re
import threading
from pathlib import Path
from typing import Callable

from .deck.grammar import (ActionError, validate_app_overrides, validate_custom_pages, validate_sounds,
                           validate_targets)
from .util import load_json_file, write_json_atomic

log = logging.getLogger("config")

VERSION = 2   # 2: the SOUNDS deck page (added once to existing decks that have room)
DEFAULT_PORT = 47321
PORT_SPAN = 20  # 47321..47340: the next free port is used when the first is busy

PAGE_IDS = ["ai", "deck", "music", "hud"]
DECK_PAGE_IDS = ["apps", "media", "windows", "dev", "macros", "sounds"]
MAX_DECK_APPS = 18
MAX_DECK_PAGES = 6

DEFAULTS: dict = {
    "version": VERSION,
    "panel": {"link": "auto", "host": ""},
    # AI is off by default: plenty of people never code with an assistant.
    "pages": ["deck", "music", "hud"],
    "deck": {"apps": [], "pages": ["apps", "media", "windows", "sounds"], "custom": [],
             "app_overrides": {},   # APPS tile (app id or target:<id>) -> {"label", "image", "color"}
             "targets": {},         # id -> {"kind": file|folder|url, "path"|"url", "label"}
             "sounds": {},          # sound id -> {"label", "icon", "color", "image", "file", "volume"}
             "style": "classic",    # button look: "classic" cards, or "icons" (the picture is the button)
             "labels": True},       # "icons" style: names under the icons
    "music": {"source": "auto", "spotify_client_id": "", "backdrop": "cover"},
    "ai": {"claude_hooks": False, "codex_hooks": False,
           "claude_usage": "off", "codex_usage": "off",          # off | token/auth | url
           "claude_usage_url": "", "codex_usage_url": "",
           "usage_poll_s": 60, "perm_wait_s": 600, "host_label": ""},
    "hud": {"pc_name": "", "lab_url": "", "lab_name": ""},  # names: "" = the host name
    "startup": True,
    "server": {"port": DEFAULT_PORT},
    "ext": {},  # settings of optional extensions (service.load_extensions)
}


def register_extension_settings(defaults: dict, pages: tuple | list = ()) -> None:
    """Optional extensions (see service.load_extensions) add their own settings
    under "ext" and their own panel page ids. Idempotent."""
    DEFAULTS["ext"].update(copy.deepcopy(defaults))
    for p in pages:
        if p not in PAGE_IDS:
            PAGE_IDS.append(p)


_HOST_RE = re.compile(r"^[A-Za-z0-9.\-:\[\]]{0,253}$")
_URL_RE = re.compile(r"^https?://[A-Za-z0-9.\-:\[\]]+(?::\d+)?(?:/[^\s]*)?$")
_CLIENT_ID_RE = re.compile(r"^[0-9a-fA-F]{32}$")


class SettingsError(ValueError):
    """A rejected settings update; the message is shown to the user."""


# ---- validators: (value) -> cleaned value, or raise SettingsError -------------------

def _enum(*allowed: str) -> Callable:
    def v(x):
        if x not in allowed:
            raise SettingsError(f"must be one of {', '.join(allowed)}")
        return x
    return v


def _bool(x):
    if not isinstance(x, bool):
        raise SettingsError("must be true or false")
    return x


def _int(lo: int, hi: int) -> Callable:
    def v(x):
        if isinstance(x, bool) or not isinstance(x, (int, float)) or int(x) != x:
            raise SettingsError("must be a whole number")
        if not lo <= int(x) <= hi:
            raise SettingsError(f"must be between {lo} and {hi}")
        return int(x)
    return v


def _str(max_len: int, pattern: re.Pattern | None = None, what: str = "text") -> Callable:
    def v(x):
        if not isinstance(x, str):
            raise SettingsError(f"must be {what}")
        x = x.strip()
        if len(x) > max_len:
            raise SettingsError(f"is too long (max {max_len} characters)")
        if x and pattern is not None and not pattern.match(x):
            raise SettingsError(f"doesn't look like a valid {what}")
        return x
    return v


def _id_list(allowed: list[str], max_n: int, min_n: int = 0) -> Callable:
    def v(x):
        if not isinstance(x, list) or not all(isinstance(i, str) for i in x):
            raise SettingsError("must be a list")
        out = []
        for i in x:
            if i not in allowed:
                raise SettingsError(f"unknown entry {i!r}")
            if i not in out:
                out.append(i)
        if len(out) > max_n:
            raise SettingsError(f"can have at most {max_n} entries")
        if len(out) < min_n:
            raise SettingsError(f"needs at least {min_n} entry")
        return out
    return v


def _deck_pages(x):
    if not isinstance(x, list) or not all(isinstance(i, str) for i in x):
        raise SettingsError("must be a list")
    out = []
    for i in x:
        if i not in DECK_PAGE_IDS and not re.fullmatch(r"c[0-9a-z]{1,8}", i):
            raise SettingsError(f"unknown page {i!r}")
        if i not in out:
            out.append(i)
    if len(out) > MAX_DECK_PAGES:
        raise SettingsError(f"the deck holds at most {MAX_DECK_PAGES} pages")
    return out


def _custom_pages(x):
    try:
        return validate_custom_pages(x)
    except ActionError as e:
        raise SettingsError(str(e)) from None


def _targets(x):
    try:
        return validate_targets(x)
    except ActionError as e:
        raise SettingsError(str(e)) from None


def _app_overrides(x):
    try:
        return validate_app_overrides(x)
    except ActionError as e:
        raise SettingsError(str(e)) from None


def _sounds(x):
    try:
        return validate_sounds(x)
    except ActionError as e:
        raise SettingsError(str(e)) from None


def _app_list(x):
    if not isinstance(x, list) or not all(isinstance(i, str) and 0 < len(i) <= 512 for i in x):
        raise SettingsError("must be a list of app ids")
    out = []
    for i in x:
        if i not in out:
            out.append(i)
    if len(out) > MAX_DECK_APPS:
        raise SettingsError(f"the Apps page holds at most {MAX_DECK_APPS} apps")
    return out


def _schema() -> dict:
    return {
        "panel": {"link": _enum("auto", "usb", "wifi"), "host": _str(253, _HOST_RE, "host name or IP address")},
        "pages": _id_list(PAGE_IDS, len(PAGE_IDS), 1),
        "deck": {"apps": _app_list, "pages": _deck_pages, "custom": _custom_pages,
                 "app_overrides": _app_overrides, "targets": _targets, "sounds": _sounds,
                 "style": _enum("classic", "icons"), "labels": _bool},
        "music": {"source": _enum("auto", "system", "spotify"),
                  "spotify_client_id": _str(64, _CLIENT_ID_RE, "Spotify Client ID (32 letters and digits)"),
                  "backdrop": _enum("cover", "artist")},
        "ai": {"claude_hooks": _bool, "codex_hooks": _bool,
               "claude_usage": _enum("off", "token", "url"), "codex_usage": _enum("off", "auth", "url"),
               "claude_usage_url": _str(300, _URL_RE, "http(s) URL"),
               "codex_usage_url": _str(300, _URL_RE, "http(s) URL"),
               "usage_poll_s": _int(30, 3600), "perm_wait_s": _int(30, 3600),
               "host_label": _str(19)},
        "hud": {"pc_name": _str(31), "lab_url": _str(300, _URL_RE, "http(s) URL"), "lab_name": _str(31)},
        "startup": _bool,
        "server": {"port": _int(1024, 65535)},
        "ext": {k: _validator_like(v) for k, v in DEFAULTS["ext"].items()},
    }


def _validator_like(default) -> Callable:
    if isinstance(default, bool):
        return _bool
    if isinstance(default, int):
        return _int(0, 1 << 30)
    if isinstance(default, str):
        return _str(300)
    if isinstance(default, list):
        return lambda x: x if isinstance(x, list) else (_ for _ in ()).throw(SettingsError("must be a list"))
    return lambda x: x


# ---- load / merge ----------------------------------------------------------------------

def _clean(value, spec, default, path: str):
    """Load-time cleaning: invalid -> default (logged), unknown keys dropped."""
    if isinstance(spec, dict):
        if not isinstance(value, dict):
            return copy.deepcopy(default)
        return {k: _clean(value.get(k, default[k]), spec[k], default[k], f"{path}.{k}") for k in spec}
    try:
        return spec(value)
    except SettingsError as e:
        log.warning("settings %s %s; using the default", path.lstrip("."), e)
        return copy.deepcopy(default)


def migrate(raw: dict) -> dict:
    """Upgrade older settings files. Version 1 is the first; future versions
    transform `raw` step by step here."""
    raw = dict(raw)
    ver = raw.get("version", VERSION)
    if not isinstance(ver, int) or ver > VERSION:
        log.warning("settings.json is from a newer app (version %r); reading what we understand", ver)
    deck = raw.get("deck")
    if isinstance(ver, int) and ver < 2 and isinstance(deck, dict) and isinstance(deck.get("pages"), list):
        # v2 brought the SOUNDS page: put it on decks that have room, once
        # (turning it off later sticks: the file is version 2 from then on).
        pages = list(deck["pages"])
        if "sounds" not in pages and len(pages) < MAX_DECK_PAGES:
            raw["deck"] = {**deck, "pages": pages + ["sounds"]}
    if isinstance(raw.get("ai"), dict) and "usage" in raw["ai"]:  # docs/app.md draft key
        ai = dict(raw["ai"])
        if ai.pop("usage") == "url":
            ai.setdefault("claude_usage", "url")
            ai.setdefault("codex_usage", "url")
        raw["ai"] = ai
    raw["version"] = VERSION
    return raw


def _post_check(s: dict) -> dict:
    """Cross-field rules: deck pages may only name custom pages that exist."""
    custom_ids = {p["id"] for p in s["deck"]["custom"]}
    s["deck"]["pages"] = [p for p in s["deck"]["pages"] if p in DECK_PAGE_IDS or p in custom_ids]
    targets = s["deck"]["targets"]
    s["deck"]["apps"] = [a for a in s["deck"]["apps"] if not a.startswith("target:") or a[7:] in targets]
    return s


def clean(raw) -> dict:
    raw = migrate(raw if isinstance(raw, dict) else {})
    out = _post_check(_clean(raw, _schema(), DEFAULTS, ""))
    out["version"] = VERSION
    return out


def apply_patch(current: dict, patch) -> dict:
    """Validate a partial update from the settings page; returns the new settings.
    Raises SettingsError naming the field on the first bad value."""
    if not isinstance(patch, dict):
        raise SettingsError("settings update must be an object")
    out = copy.deepcopy(current)

    def walk(dst: dict, src: dict, spec: dict, path: str):
        for k, v in src.items():
            if k == "version":
                continue
            if k not in spec:
                raise SettingsError(f"unknown setting {path}{k}")
            if isinstance(spec[k], dict):
                if not isinstance(v, dict):
                    raise SettingsError(f"{path}{k} must be an object")
                walk(dst[k], v, spec[k], f"{path}{k}.")
            else:
                try:
                    dst[k] = spec[k](v)
                except SettingsError as e:
                    raise SettingsError(f"{path}{k} {e}") from None

    walk(out, patch, _schema(), "")
    return _post_check(out)


class Settings:
    """Thread-safe settings with change listeners (called after each save)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._listeners: list[Callable[[dict, dict], None]] = []
        self.first_run = not self.path.exists()
        raw = load_json_file(self.path, {})
        self._data = clean(raw)
        if self.first_run or raw != self._data:
            self._save()

    def get(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._data)

    def section(self, name: str):
        with self._lock:
            return copy.deepcopy(self._data.get(name))

    def on_change(self, fn: Callable[[dict, dict], None]) -> None:
        self._listeners.append(fn)

    def update(self, patch: dict, check: Callable[[dict], None] | None = None) -> dict:
        """check(new) may raise SettingsError to refuse an otherwise valid update
        (rules that need live data, like the panel's icon store size)."""
        with self._lock:
            old = copy.deepcopy(self._data)
            new = apply_patch(self._data, patch)
            if new == old:
                return copy.deepcopy(new)
            if check is not None:
                check(copy.deepcopy(new))
            self._data = new
            self._save()
        for fn in list(self._listeners):
            try:
                fn(old, copy.deepcopy(new))
            except Exception:  # noqa: BLE001 - one bad listener must not block the rest
                log.exception("settings listener failed")
        return copy.deepcopy(new)

    def _save(self) -> None:
        write_json_atomic(self.path, self._data, private=True)
