"""Builds the deck the panel shows and keeps its icons.

    APPS     the apps the user picked (settings deck.apps), else suggestions
             (most-used first, padded to 18), with their real icons; each tile's
             label / picture can be overridden (deck.app_overrides)
    built-in MEDIA / WINDOWS / DEV / MACROS from defaults.json, per OS;
             SOUNDS from the sound library + deck.sounds (sounds/__init__.py)
    custom   pages the user made; app:<id> buttons become agent:launch:app:<key>

The deck JSON: {"rev", "style": "classic"|"icons", "labels": bool, "pages":[
{"name","color","cols","buttons":[{"label","icon","img","action","color"}]}]}.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .. import osutil
from ..music import images as im
from . import userimg
from .grammar import ascii_label

log = logging.getLogger("board")

HERE = Path(__file__).resolve().parent
APP_PAGE_COLOR = "#F2B14B"
MAX_APPS = 18
MAX_PAGES = 6
ICON_SLOTS = 47          # panel icon store holds 48; one is the cover art
PNG_PX = 64


def load_defaults() -> dict:
    return json.loads((HERE / "defaults.json").read_text(encoding="utf-8"))


def builtin_page(defaults: dict, page_id: str, os_name: str) -> dict | None:
    p = (defaults.get("pages") or {}).get(page_id)
    if p is None:
        return None
    if "name" not in p:
        p = p.get(os_name) or p.get("windows")
    return json.loads(json.dumps(p))


def short_name(name: str) -> str:
    for drop in (" for Windows", " (Beta)", " - Microsoft Store", "Microsoft "):
        name = name.replace(drop, "")
    return ascii_label(name) or "App"


def deck_rev(pages: list[dict], style: str = "classic", labels: bool = True) -> int:
    body = pages if (style, labels) == ("classic", True) else {"pages": pages, "style": style, "labels": labels}
    return int(hashlib.sha1(json.dumps(body, sort_keys=True).encode()).hexdigest()[:7], 16) | 0x10000000


def deck_style(deck_cfg: dict) -> tuple[str, bool, int]:
    """Pure: (style, labels, icon px) from settings deck. "icons" draws the picture as
    the whole button, so pictures are encoded larger: 80 px with the name under
    them, 96 px without (the tile is 113 px tall)."""
    style = "icons" if deck_cfg.get("style") == "icons" else "classic"
    labels = True if style == "classic" else bool(deck_cfg.get("labels", True))
    if style == "classic":
        return style, labels, im.ICON_PX
    return style, labels, (im.ICON_PX_NAMED if labels else im.ICON_PX_LARGE)


def apps_page(app_list: list[dict]) -> dict:
    """APPS tiles: {"name", "key"} (+ optional "action", "icon", "color", "img")."""
    buttons = []
    for a in app_list[:MAX_APPS]:
        b = {"label": a["name"], "icon": a.get("icon") or "apps", "img": a.get("img", a["key"]),
             "action": a.get("action") or f"agent:launch:app:{a['key']}"}
        if not b["img"]:
            del b["img"]
        if a.get("color"):
            b["color"] = a["color"]
        buttons.append(b)
    return {"name": "APPS", "color": APP_PAGE_COLOR, "cols": 6, "buttons": buttons}


def target_label(t: dict) -> str:
    """Default label for a file / folder / website button."""
    import os
    import urllib.parse
    if t.get("label"):
        return t["label"]
    if t.get("kind") == "url":
        host = urllib.parse.urlsplit(t.get("url", "")).hostname or "Website"
        return ascii_label(host[4:] if host.startswith("www.") else host) or "Website"
    path = str(t.get("path", "")).rstrip("/\\")
    base = os.path.basename(path) or path
    stem, ext = os.path.splitext(base)
    return ascii_label(stem if ext.lower() in (".lnk", ".url", ".desktop", ".app") else base) or "File"


def target_glyph(t: dict) -> str:
    from ..osutil import runs_code
    kind = t.get("kind")
    if kind == "folder":
        return "folder"
    if kind == "url":
        return "web"
    return "run" if runs_code(t.get("path", "")) else "edit"


def build_deck(app_list: list[dict], extra_pages: list[dict]) -> dict:
    """Pure: deck dict with a stable content-hash rev."""
    pages = [apps_page(app_list)] if app_list else []
    pages.extend(extra_pages)
    pages = pages[:MAX_PAGES]
    return {"rev": deck_rev(pages), "pages": pages}


# Apps most people use, in order, to fill the suggestions when the OS reports
# usage for only a few (Windows' UserAssist often knows just 5-10 apps).
WELL_KNOWN = (
    r"google chrome|^chrome$", r"microsoft edge|^edge$", r"firefox", r"brave", r"^safari$", r"^arc$", r"opera",
    r"file explorer|^finder$|^files$|nautilus|dolphin", r"terminal|konsole|iterm", r"visual studio code|^code$",
    r"spotify", r"discord", r"^steam$", r"slack", r"teams", r"zoom", r"outlook|^mail$|thunderbird",
    r"\bword\b", r"\bexcel\b", r"powerpoint", r"obs studio", r"^notepad", r"calculator",
    r"^settings$|system settings|system preferences", r"^photos$", r"task manager|activity monitor|system monitor",
    r"claude", r"chatgpt", r"notion", r"obsidian", r"vlc", r"whatsapp", r"telegram", r"1password|bitwarden",
)
JUNK = re.compile(r"uninstall|readme|read me|documentation|\bhelp\b|manual|release notes|license|website|"
                  r"support|feedback|\(x86\)|\(x64\)|\(wow\)|debug|diagnostic|recovery|\bupdate|\breset\b|safe mode|"
                  r"skinned|verifier|crash report", re.IGNORECASE)


def rank_suggestions(catalog: list, limit: int = MAX_APPS) -> list:
    """Pure: the most-used apps (usage score: focus time, then launches),
    padded with well-known apps, then the rest alphabetically, so there are
    always `limit` suggestions when the computer has that many apps."""
    out, names, ids = [], set(), set()

    def take(a) -> bool:
        key = a.name.lower()
        if a.id in ids or key in names or JUNK.search(a.name):
            return False
        names.add(key)
        ids.add(a.id)
        out.append(a)
        return len(out) >= limit

    by_name = sorted(catalog, key=lambda a: a.name.lower())
    for a in sorted((a for a in catalog if a.score > 0), key=lambda a: (-a.score, a.name.lower())):
        if take(a):
            return out
    for pat in WELL_KNOWN:
        rx = re.compile(pat, re.IGNORECASE)
        for a in by_name:
            if rx.search(a.name) and take(a):
                return out
    for a in by_name:
        if take(a):
            return out
    return out


def pick_apps(chosen: list[str], catalog: list, limit: int = MAX_APPS) -> list:
    """Pure: the user's apps in their order (unknown ids dropped), else the
    suggestions (most-used first)."""
    by_id = {a.id: a for a in catalog}
    if chosen:
        return [by_id[i] for i in chosen if i in by_id][:limit]
    return rank_suggestions(catalog, limit)


def deck_custom(deck_cfg: dict) -> dict:
    """Custom pages by id, plus the SOUNDS page (generated from the sound library) when it's on."""
    custom = {p["id"]: p for p in deck_cfg.get("custom") or []}
    if "sounds" in (deck_cfg.get("pages") or []):
        from ..sounds import sounds_page
        custom["sounds"] = sounds_page(deck_cfg)
    return custom


def icon_demand(deck_cfg: dict, catalog_ids: set[str] | None = None, auto_tiles: int = MAX_APPS) -> int:
    """Pure: how many panel icon slots a deck needs (APPS tiles, app buttons on
    custom pages, uploaded pictures)."""
    order = deck_cfg.get("pages") or []
    keys: set[str] = set()
    overrides = deck_cfg.get("app_overrides") or {}
    targets = deck_cfg.get("targets") or {}
    if "apps" in order:
        chosen = [a for a in deck_cfg.get("apps") or []
                  if (a[7:] in targets if a.startswith("target:") else catalog_ids is None or a in catalog_ids)]
        for a in chosen[:MAX_APPS]:
            img = (overrides.get(a) or {}).get("image")
            keys.add(f"img:{img}" if img else a if a.startswith("target:") else f"app:{a}")
        if not deck_cfg.get("apps"):
            keys.update(f"auto:{i}" for i in range(auto_tiles))
    custom = deck_custom(deck_cfg)
    for pid in order:
        for b in (custom.get(pid) or {}).get("buttons") or []:
            act = str(b.get("action", ""))
            if b.get("image"):
                keys.add(f"img:{b['image']}")
            elif act.startswith("app:"):
                keys.add(f"app:{act[4:]}")
            elif act.startswith("target:"):
                keys.add(act)
    return len(keys)


def custom_page(page: dict, app_key: dict[str, str], with_icon: set[str],
                image_key: dict[str, str] | None = None, targets: dict | None = None,
                target_key: dict[str, str] | None = None) -> dict:
    """Pure: a settings custom page -> firmware page."""
    buttons = []
    image_key, targets, target_key = image_key or {}, targets or {}, target_key or {}
    for b in page.get("buttons") or []:
        action = b.get("action", "")
        out = {"label": b.get("label", ""), "icon": b.get("icon") or "bolt", "action": action}
        picture = image_key.get(b.get("image") or "")
        if picture and picture in with_icon:
            out["img"] = picture
        if action.startswith("target:"):
            tid = action[7:]
            if tid in targets:
                out["action"] = f"agent:launch:target:{tid}"
                out["icon"] = b.get("icon") or target_glyph(targets[tid])
                out["label"] = out["label"] or target_label(targets[tid])
                key = target_key.get(tid)
                if key and key in with_icon and "img" not in out:
                    out["img"] = key
            else:
                out["action"] = "agent:noop"  # target removed
        elif action.startswith("app:"):
            key = app_key.get(action[4:])
            if key:
                out["action"] = f"agent:launch:app:{key}"
                out["icon"] = b.get("icon") or "apps"
                if key in with_icon and "img" not in out:
                    out["img"] = key
            else:
                out["action"] = "agent:noop"  # app no longer installed
        if b.get("color"):
            out["color"] = b["color"]
        buttons.append(out)
    layout = page.get("layout") or ("apps" if len(buttons) > 12 else "keys")
    return {"name": page.get("name") or "CUSTOM", "color": page.get("color") or "#D97757",
            "cols": 6 if layout == "apps" else 4, "buttons": buttons}


class DeckBoard:
    """All platform calls (app list, icons, launch) go through one worker
    thread, so platform code (COM on Windows) always runs on the same thread."""

    def __init__(self, settings, platform, os_name: str, cache_dir: Path | None = None):
        self.settings = settings
        self.platform = platform
        self.os_name = os_name
        self.cache_dir = cache_dir
        self._target_icons: dict[str, object] = {}  # path/url -> RGBA image or None
        self.defaults = load_defaults()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="apps")
        self._lock = threading.Lock()
        self._catalog: list = []
        self._catalog_ready = threading.Event()
        self._icons565: dict[tuple, bytes] = {}  # (app id, px) -> RGB565A8 icon
        self._png: dict[str, bytes | None] = {}  # app id -> PNG for the settings page
        self.keymap: dict[str, str] = {}        # deck key -> app id
        self.deck: dict | None = None
        self._images: dict = {}
        self.error = ""

    # ---- catalog ------------------------------------------------------------------
    def start(self) -> None:
        self._pool.submit(self._load_catalog)

    def refresh(self) -> None:
        self._pool.submit(self._load_catalog)

    def _load_catalog(self) -> None:
        try:
            apps = list(self.platform.list_apps()) if self.platform else []
            self.error = ""
        except Exception as e:  # noqa: BLE001 - keep the last good list
            log.warning("app discovery failed: %s", e)
            self.error = f"Couldn't list your apps: {e}"
            apps = self._catalog
        with self._lock:
            self._catalog = apps
        self._catalog_ready.set()
        log.info("app catalog: %d apps", len(apps))
        self._rebuild()

    def catalog(self) -> list[dict]:
        chosen = set((self.settings.section("deck") or {}).get("apps") or [])
        with self._lock:
            apps = list(self._catalog)
        apps.sort(key=lambda a: (-a.score, a.name.lower()))
        return [{"id": a.id, "name": a.name, "short": short_name(a.name), "score": round(a.score, 3),
                 "chosen": a.id in chosen} for a in apps]

    def known(self, app_id: str) -> bool:
        with self._lock:
            return any(a.id == app_id for a in self._catalog)

    def catalog_ready(self) -> bool:
        return self._catalog_ready.is_set()

    def suggested(self, limit: int = MAX_APPS) -> list[str]:
        with self._lock:
            apps = list(self._catalog)
        return [a.id for a in rank_suggestions(apps, limit)]

    def icon_demand(self, deck_cfg: dict) -> int:
        with self._lock:
            apps = list(self._catalog)
        ids = {a.id for a in apps}
        return icon_demand(deck_cfg, ids or None, len(rank_suggestions(apps)) if apps else MAX_APPS)

    # ---- icons -----------------------------------------------------------------------
    def _icon_image(self, app_id: str):
        try:
            return self.platform.app_icon(app_id) if self.platform else None
        except Exception as e:  # noqa: BLE001
            log.debug("icon for %s failed: %s", app_id, e)
            return None

    def icon_png(self, app_id: str, timeout: float = 15.0) -> bytes | None:
        with self._lock:
            if app_id in self._png:
                return self._png[app_id]

        def work():
            img = self._icon_image(app_id)
            return im.png_bytes(im.trim(img), PNG_PX) if img is not None else None

        try:
            data = self._pool.submit(work).result(timeout=timeout)
        except Exception:  # noqa: BLE001 - timeout or platform failure: no icon
            return None
        with self._lock:
            if len(self._png) > 400:
                self._png.clear()
            self._png[app_id] = data
        return data

    def _icon565(self, app_id: str, px: int = im.ICON_PX) -> bytes | None:
        """Worker thread only."""
        if (app_id, px) in self._icons565:
            return self._icons565[(app_id, px)]
        img = self._icon_image(app_id)
        data = im.to_rgb565a8(img, px) if img is not None else None
        if data:
            self._icons565[(app_id, px)] = data
        return data

    # ---- deck -------------------------------------------------------------------------
    def rebuild(self) -> None:
        """Settings changed: rebuild on the worker thread."""
        self._pool.submit(self._rebuild)

    def _rebuild(self) -> None:
        deck_cfg = self.settings.section("deck") or {}
        with self._lock:
            catalog = list(self._catalog)
        by_id = {a.id: a for a in catalog}
        order = deck_cfg.get("pages") or []
        overrides = deck_cfg.get("app_overrides") or {}
        targets = deck_cfg.get("targets") or {}
        custom = deck_custom(deck_cfg)
        keymap: dict[str, str] = {}       # deck key -> app id (launch:app:<key>)
        source: dict[str, tuple] = {}     # deck key -> ("app", id) | ("target", tid) | ("img", sha1)
        app_key: dict[str, str] = {}
        target_key: dict[str, str] = {}
        image_key: dict[str, str] = {}
        tiles = []
        if "apps" in order:
            chosen = deck_cfg.get("apps") or []
            entries = ([e for e in chosen if e.startswith("target:") and e[7:] in targets or e in by_id]
                       if chosen else [a.id for a in pick_apps([], catalog)])
            for i, entry in enumerate(entries[:MAX_APPS]):
                key, ov = f"a{i}", overrides.get(entry) or {}
                if entry.startswith("target:"):
                    tid = entry[7:]
                    t = targets[tid]
                    target_key.setdefault(tid, key)
                    source[key] = ("target", tid)
                    tile = {"name": ov.get("label") or target_label(t), "key": key, "icon": target_glyph(t),
                            "action": f"agent:launch:target:{tid}"}
                else:
                    app_key[entry] = key
                    keymap[key] = entry
                    source[key] = ("app", entry)
                    tile = {"name": ov.get("label") or short_name(by_id[entry].name), "key": key}
                if ov.get("image"):
                    source[key] = ("img", ov["image"])
                if ov.get("color"):
                    tile["color"] = ov["color"]
                tiles.append(tile)
        for pid in order:
            for b in (custom.get(pid) or {}).get("buttons") or []:
                act = b.get("action", "")
                if b.get("image") and b["image"] not in image_key:
                    image_key[b["image"]] = key = f"u{len(image_key)}"
                    source[key] = ("img", b["image"])
                if act.startswith("app:") and act[4:] not in app_key and (act[4:] in by_id or not catalog):
                    key = app_key[act[4:]] = f"b{len(app_key)}"
                    keymap[key] = act[4:]
                    source[key] = ("app", act[4:])
                elif act.startswith("target:") and act[7:] in targets and act[7:] not in target_key:
                    key = target_key[act[7:]] = f"t{len(target_key)}"
                    source[key] = ("target", act[7:])
        # Icons (the panel store holds ICON_SLOTS icons besides the cover art); the
        # "icons" style shows them as the whole button, so they're encoded larger.
        style, labels, px = deck_style(deck_cfg)
        icons, with_icon = {}, set()
        for key, (kind, ref) in list(source.items())[:ICON_SLOTS]:
            if kind == "img":
                data = userimg.panel_bytes(ref, px)
            elif kind == "target":
                img = self._target_image(targets[ref])
                data = im.to_rgb565a8(img, px) if img is not None else None
            else:
                data = self._icon565(ref, px)
            if data:
                icons[f"icon:{key}"] = ({"kind": "icon", "key": key, "w": px, "h": px, "tag": ""}, data)
                with_icon.add(key)
        for t in tiles:
            if t["key"] not in with_icon:
                t["img"] = ""  # no picture: the glyph shows
        pages = []
        for pid in order:
            if pid == "apps":
                if tiles:
                    pages.append(apps_page(tiles))
            elif pid in custom:
                pages.append(custom_page(custom[pid], app_key, with_icon, image_key, targets, target_key))
            else:
                p = builtin_page(self.defaults, pid, self.os_name)
                if p:
                    pages.append(p)
        pages = pages[:MAX_PAGES]
        deck = {"rev": deck_rev(pages, style, labels), "style": style, "labels": labels, "pages": pages}
        with self._lock:
            self.keymap, self.deck, self._images = keymap, deck, icons
        log.info("deck: %d pages, %d icons", len(deck["pages"]), len(icons))

    # ---- files, folders, websites ---------------------------------------------------------
    def _target_image(self, t: dict):
        """Worker thread only: the real file/folder icon, or the website's icon."""
        ref = t.get("url") or t.get("path") or ""
        if ref in self._target_icons:
            return self._target_icons[ref]
        img = None
        try:
            if t.get("kind") == "url":
                from . import favicon
                img = favicon.fetch(t["url"], self.cache_dir)
            else:
                getter = getattr(self.platform, "file_icon", None)  # optional platform feature
                img = getter(t["path"]) if getter else None
        except Exception as e:  # noqa: BLE001
            log.debug("icon for %s failed: %s", ref, e)
        if len(self._target_icons) > 200:
            self._target_icons.clear()
        self._target_icons[ref] = img
        return img

    def target_png(self, tid: str, timeout: float = 15.0) -> bytes | None:
        t = ((self.settings.section("deck") or {}).get("targets") or {}).get(tid)
        if not t:
            return None
        try:
            img = self._pool.submit(self._target_image, t).result(timeout=timeout)
        except Exception:  # noqa: BLE001
            return None
        return im.png_bytes(im.trim(img), PNG_PX) if img is not None else None

    def launch_target(self, tid: str) -> bool:
        t = ((self.settings.section("deck") or {}).get("targets") or {}).get(tid)
        if not t:
            log.warning("unknown target %r", tid)
            return False
        return osutil.open_target(t)

    def images(self) -> dict:
        with self._lock:
            return dict(self._images)

    def current(self) -> dict | None:
        with self._lock:
            return self.deck

    # ---- actions --------------------------------------------------------------------------
    def launch(self, key: str) -> bool:
        with self._lock:
            app_id = self.keymap.get(key)
        if not app_id:
            log.warning("unknown app key %r", key)
            return False
        self._pool.submit(self._launch_app, app_id)
        return True

    def _launch_app(self, app_id: str) -> None:
        try:
            self.platform.launch_app(app_id)
            log.info("launched %s", app_id)
        except Exception as e:  # noqa: BLE001
            log.warning("launch %s failed: %s", app_id, e)

    def launch_named(self, name: str) -> bool:
        target = ((self.defaults.get("launch") or {}).get(self.os_name) or {}).get(name)
        if not target:
            log.warning("no launch target %r on %s", name, self.os_name)
            return False
        if target.startswith("app:"):
            self._pool.submit(self._launch_app, target[4:])
            return True
        return osutil.launch_target(target, self._launch_app)
