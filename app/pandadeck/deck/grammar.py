"""Deck button grammar (pure, mirrors firmware/PandaDeck/deck_action.cpp).

User-facing actions:
    keys:<KEY>[+<KEY>...]   up to 6 keys, e.g. keys:CTRL+SHIFT+ESC
    media:<name>            playpause | play | next | prev | mute | volup | voldown | stop
    app:<app id>            open one of the user's apps (translated to agent:launch:app:<key>)
    target:<id>             open a file, folder or website (deck.targets; agent:launch:target:<id>)
    agent:<cmd>             advanced: forwarded to the app (e.g. agent:mic)
    agent:sound:<id>        play a sound on this computer (deck.sounds / sounds.library());
    agent:sound:stop        ... or stop every sound playing

The firmware only knows keys:/media:/agent: and keeps 47 characters of an
action, so app:<id> becomes a short per-deck key (see board.py).
"""
from __future__ import annotations

import re

MAX_CHORD = 6
ACTION_MAX = 47       # DeckButton.action[48]
AGENT_MAX = 39        # DeckAction.agent[40]
LABEL_MAX = 19        # DeckButton.label[20]
PAGE_NAME_MAX = 11    # DeckPage.name[12]
MAX_BUTTONS = 18      # MODEL_MAX_DECK_BUTTONS

NAMED_KEYS = {
    "CTRL", "SHIFT", "ALT", "GUI", "WIN", "RCTRL", "RSHIFT", "RALT", "RGUI",
    "UP", "DOWN", "LEFT", "RIGHT", "ENTER", "RETURN", "ESC", "BACKSPACE", "TAB", "SPACE",
    "INSERT", "DELETE", "DEL", "HOME", "END", "PGUP", "PGDN", "PRTSC", "MENU", "CAPSLOCK",
    "PERIOD", "COMMA", "MINUS", "EQUAL", "SLASH", "SEMICOLON", "QUOTE", "BACKSLASH", "GRAVE",
    "LBRACKET", "RBRACKET",
}
MEDIA_NAMES = ("playpause", "play", "next", "prev", "mute", "volup", "voldown", "stop")

# Symbol names the firmware can draw (firmware/PandaDeck/ui_theme.cpp ICONS).
ICON_NAMES = (
    "prev", "play", "pause", "next", "mute", "vol_down", "vol_up", "mic", "mic_off", "audio",
    "camera", "home", "list", "lock", "left", "right", "up", "down", "taskview", "folder",
    "paste", "smile", "settings", "go", "loop", "close", "terminal", "edit", "web", "copy",
    "undo", "redo", "save", "search", "enter", "bolt", "refresh", "power", "warning", "check",
    "robot", "server", "brightness", "wifi", "keyboard", "apps", "cut", "chip", "thermo",
    "speed", "cloud", "game", "image", "run", "crop", "spark", "shuffle", "repeat",
    "repeat_one", "music", "unlock", "car", "sync", "pin",
)

TARGET_ID = re.compile(r"t[0-9a-z]{1,8}")
LAYOUTS = {"keys": 12, "apps": 18}   # 4 x 3 buttons, or the 6 x 3 app grid
_FKEY = re.compile(r"^F([1-9]|1[0-9]|2[0-4])$", re.IGNORECASE)
_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")


class ActionError(ValueError):
    pass


def _key_ok(tok: str) -> bool:
    if tok.upper() in NAMED_KEYS or _FKEY.match(tok):
        return True
    return len(tok) == 1 and " " < tok < "\x7f" and tok != "+"


def normalize_keys(spec: str) -> str:
    toks = [t.strip() for t in spec.split("+")]
    if not toks or any(not t for t in toks):
        raise ActionError("a key combination looks like CTRL+SHIFT+ESC")
    if len(toks) > MAX_CHORD:
        raise ActionError(f"at most {MAX_CHORD} keys per combination")
    out = []
    for t in toks:
        if not _key_ok(t):
            raise ActionError(f"unknown key {t!r}")
        out.append(t.upper() if len(t) > 1 or t.isalpha() else t)
    return "+".join(out)


def validate_action(action: str) -> str:
    """User action -> normalized user action, or ActionError with a friendly message."""
    if not isinstance(action, str):
        raise ActionError("action must be text")
    a = action.strip()
    kind, _, arg = a.partition(":")
    kind = kind.lower()
    if kind == "keys":
        a = "keys:" + normalize_keys(arg)
    elif kind == "media":
        if arg.lower() not in MEDIA_NAMES:
            raise ActionError(f"media keys are {', '.join(MEDIA_NAMES)}")
        a = "media:" + arg.lower()
    elif kind == "app":
        if not arg or len(arg) > 512 or any(ord(c) < 32 for c in arg):
            raise ActionError("pick an app from the list")
        return "app:" + arg
    elif kind == "target":
        if not TARGET_ID.fullmatch(arg):
            raise ActionError("pick a file, folder or website")
        return "target:" + arg
    elif kind == "agent":
        if not arg or len(arg) > AGENT_MAX or not arg.isascii():
            raise ActionError(f"agent commands are at most {AGENT_MAX} characters")
        a = "agent:" + arg
    else:
        raise ActionError("an action starts with keys:, media:, app: or target:")
    if len(a) > ACTION_MAX:
        raise ActionError(f"action is too long (max {ACTION_MAX} characters)")
    return a


def ascii_label(text: str, limit: int = LABEL_MAX) -> str:
    """The panel's fonts are ASCII: fold accents, drop the rest."""
    import unicodedata
    norm = unicodedata.normalize("NFKD", str(text or ""))
    out = "".join(ch for ch in norm if 32 <= ord(ch) < 127)
    return out.strip()[:limit]


def validate_color(c) -> str:
    if c in ("", None):
        return ""
    if not isinstance(c, str) or not _COLOR.match(c):
        raise ActionError("colours look like #D97757")
    return c.upper()


def validate_icon(name) -> str:
    if name in ("", None):
        return ""
    if name not in ICON_NAMES:
        raise ActionError(f"unknown icon {name!r}")
    return name


def validate_image_id(image) -> str:
    """A custom button picture: the sha1 of a stored icon (see deck/userimg.py), or ""."""
    if image in ("", None):
        return ""
    if not isinstance(image, str) or not re.fullmatch(r"[0-9a-f]{40}", image):
        raise ActionError("unknown picture")
    return image


def validate_app_overrides(x) -> dict:
    """{app id: {"label": ..., "image": ...}} for tiles on the APPS page."""
    if not isinstance(x, dict) or len(x) > 64:
        raise ActionError("app overrides must be an object")
    out = {}
    for app_id, ov in x.items():
        if not isinstance(app_id, str) or not 0 < len(app_id) <= 512 or not isinstance(ov, dict):
            raise ActionError("bad app override")
        entry = {"label": ascii_label(ov.get("label") or ""), "image": validate_image_id(ov.get("image")),
                 "color": validate_color(ov.get("color"))}
        if entry["label"] or entry["image"] or entry["color"]:
            out[app_id] = entry
    return out


SOUND_ID = re.compile(r"[a-z][a-z0-9_]{0,23}")         # agent:sound:<id> stays well under AGENT_MAX
SOUND_FILE = re.compile(r"[0-9a-f]{40}\.(?:wav|mp3|ogg|m4a)")  # an upload in <config>/sounds (sounds/store.py)
SOUND_MAX = 64


def validate_sounds(x) -> dict:
    """deck.sounds: {sound id: {"label", "icon", "color", "image", "file", "volume"}}.
    The built-in sounds' changes (label "" = the default name, file "" = the
    default sound) and the user's own sounds (a file is then required)."""
    if not isinstance(x, dict) or len(x) > SOUND_MAX:
        raise ActionError("sounds must be an object")
    out = {}
    for sid, s in x.items():
        if not isinstance(sid, str) or not SOUND_ID.fullmatch(sid) or sid == "stop" or not isinstance(s, dict):
            raise ActionError("bad sound")
        f = s.get("file") or ""
        if f and (not isinstance(f, str) or not SOUND_FILE.fullmatch(f)):
            raise ActionError("unknown sound file")
        vol = s.get("volume", 80)
        if isinstance(vol, bool) or not isinstance(vol, int) or not 0 <= vol <= 100:
            raise ActionError("a sound's volume is 0 to 100")
        origin = s.get("image_origin") or ""
        if origin not in ("", "page", "catalog", "manual", "ai"):
            raise ActionError("bad picture origin")
        src, link = s.get("image_src") or "", s.get("link") or ""
        if not isinstance(src, str) or len(src) > 512 or any(ord(c) < 32 for c in src):
            raise ActionError("bad picture source")
        if not isinstance(link, str) or len(link) > 1024 or (link and not re.match(r"https?://[^\s]+$", link)):
            raise ActionError("a sound's link starts with https://")
        out[sid] = {"label": ascii_label(s.get("label") or ""), "icon": validate_icon(s.get("icon")),
                    "color": validate_color(s.get("color")), "image": validate_image_id(s.get("image")),
                    "file": f, "volume": vol,
                    # where the picture came from: automatic ones ("page"/"catalog") get replaced with
                    # the sound; anything else is the user's and is never touched (sounds/artwork.py)
                    "image_auto": s.get("image_auto") is True, "image_origin": origin, "image_src": src,
                    "link": link}
    return out


def validate_targets(x) -> dict:
    """{id: {"kind": file|folder|url, "path" | "url", "label"}}: structure only
    (whether a file exists is checked when it is added, not on every load)."""
    import urllib.parse
    if not isinstance(x, dict) or len(x) > 64:
        raise ActionError("targets must be an object")
    out = {}
    for tid, t in x.items():
        if not isinstance(tid, str) or not TARGET_ID.fullmatch(tid) or not isinstance(t, dict):
            raise ActionError("bad target")
        kind = t.get("kind")
        label = ascii_label(t.get("label") or "")
        if kind in ("file", "folder"):
            path = t.get("path")
            if not isinstance(path, str) or not path.strip() or len(path) > 1024 or "\0" in path \
                    or any(ord(c) < 32 for c in path):
                raise ActionError("a file or folder needs its full path")
            path = path.strip().strip('"\'').strip()  # Windows "Copy as path" adds quotes
            if not path:
                raise ActionError("a file or folder needs its full path")
            out[tid] = {"kind": kind, "path": path, "label": label}
        elif kind == "url":
            url = (t.get("url") or "").strip() if isinstance(t.get("url"), str) else ""
            u = urllib.parse.urlsplit(url)
            if u.scheme not in ("http", "https") or not u.netloc or len(url) > 2048 or any(c.isspace() for c in url):
                raise ActionError("a website address starts with https:// (or http://)")
            out[tid] = {"kind": "url", "url": url, "label": label}
        else:
            raise ActionError("a target is a file, a folder or a website")
    return out


def validate_custom_pages(pages) -> list[dict]:
    """Custom deck pages from the settings page -> cleaned list (raises ActionError)."""
    if not isinstance(pages, list):
        raise ActionError("custom pages must be a list")
    if len(pages) > 6:
        raise ActionError("at most 6 custom pages")
    out, ids = [], set()
    for p in pages:
        if not isinstance(p, dict):
            raise ActionError("a custom page must be an object")
        pid = str(p.get("id") or "")
        if not re.fullmatch(r"c[0-9a-z]{1,8}", pid) or pid in ids:
            raise ActionError("custom page ids look like c1, c2 ...")
        ids.add(pid)
        name = ascii_label(p.get("name") or "", PAGE_NAME_MAX).upper() or "CUSTOM"
        buttons = p.get("buttons") or []
        layout = p.get("layout") or ("apps" if isinstance(buttons, list) and len(buttons) > 12 else "keys")
        if layout not in LAYOUTS:
            raise ActionError("a page layout is keys (4 x 3) or apps (6 x 3)")
        if not isinstance(buttons, list) or len(buttons) > LAYOUTS[layout]:
            raise ActionError(f"{name} holds at most {LAYOUTS[layout]} buttons"
                              + (" (switch it to the 6 x 3 app grid for 18)" if layout == "keys" else ""))
        clean_btns = []
        for b in buttons:
            if not isinstance(b, dict):
                raise ActionError("a button must be an object")
            label = ascii_label(b.get("label") or "")
            try:
                clean_btns.append({"label": label, "icon": validate_icon(b.get("icon")),
                                   "color": validate_color(b.get("color")),
                                   "image": validate_image_id(b.get("image")),
                                   "action": validate_action(b.get("action") or "")})
            except ActionError as e:
                raise ActionError(f"button {label or len(clean_btns) + 1!s} on {name}: {e}") from None
        out.append({"id": pid, "name": name, "color": validate_color(p.get("color")) or "#D97757",
                    "layout": layout, "buttons": clean_btns})
    return out
