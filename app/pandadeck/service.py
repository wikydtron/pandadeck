"""Wires everything together: settings, secrets, the panel link, collectors,
the sync loop and the local web server; reacts to settings changes."""
from __future__ import annotations

import http.client
import importlib
import importlib.util
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

from . import APP_ID, __version__, osutil, paths
from .actions import Actions
from .ai import hooks as ai_hooks
from .ai.bridge import Bridge
from .ai.connect import claude_cli
from .ai.usage import UsageCollector, read_codex_auth
from .config import DEFAULT_PORT, PAGE_IDS, PORT_SPAN, Settings, SettingsError
from .deck.board import ICON_SLOTS, DeckBoard, builtin_page
from .deck.grammar import ICON_NAMES
from .firmware import RELEASES_API, FirmwareJob
from .hud.lab import LabCollector
from .hud.hw import host_name
from .hud.pc import PcCollector
from .link.base import LinkError, Unsupported
from .link.discover import LinkManager
from .music.artist import ArtistImages
from .music.collector import MediaCollector
from .music.likes import Likes
from .music.spotify import SpotifyAuth, SpotifyBackend, redirect_uri_for
from .sounds import DEFAULT_VOLUME as SOUND_VOLUME
from .sounds import PAGE_COLOR as SOUND_PAGE_COLOR
from .sounds import Sounds
from .sounds.artwork import Artwork, Catalog
from .sounds.fetch import Fetcher, SoundImport
from .secrets import SecretStore
from .state import StateStore
from .sync import Syncer
from .tz import posix_tz
from .util import read_json, write_json_atomic
from .web import api
from .web.server import bind_first_free


log = logging.getLogger("service")

OS_NAMES = {"win32": "windows", "darwin": "macos"}


def os_name() -> str:
    return OS_NAMES.get(sys.platform, "linux")


class NullPlatform:
    """Used when the OS layer can't load: the panel link, HUD, AI and Spotify
    still work; apps / system media / audio / autostart are unavailable."""

    def __init__(self, name: str, error: str = ""):
        self.name, self.error = name, error

    def list_apps(self):
        return []

    def app_icon(self, app_id):
        return None

    def launch_app(self, app_id):
        raise NotImplementedError("app launching isn't available on this system yet")

    def media(self):
        return None

    def audio(self):
        return None

    def set_autostart(self, enabled, argv):
        raise NotImplementedError("launch at login isn't available on this system yet")

    def autostart_enabled(self):
        return False


def load_extensions(package: str = "extras") -> list:
    """Optional extensions package (pandadeck/extras): each extension adds its
    own settings, snapshot sources, panel actions and a settings-page section
    (/ext/<name>.js). Without the package there are none (nothing to report)."""
    name = f"{__package__}.{package}"
    try:
        found = importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        found = False
    if not found:
        log.debug("no optional extensions installed")
        return []
    try:
        extras = importlib.import_module(name)
    except ImportError as e:  # present but broken: say so
        log.warning("extensions failed to load: %s", e)
        return []
    exts = [e() if isinstance(e, type) else e for e in getattr(extras, "EXTENSIONS", [])]
    for e in exts:
        e.register()
    return exts


def load_platform():
    try:
        from .platform import current
        return current(), ""
    except Exception as e:  # noqa: BLE001 - missing module / OS libraries
        log.warning("platform layer unavailable (%s): apps, system media and audio are off", e)
        return NullPlatform(os_name(), str(e)), str(e)


def autostart_argv() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--no-browser"]
    exe = sys.executable
    if sys.platform == "win32":
        pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.exists(pyw):
            exe = pyw
    return [exe, "-m", "pandadeck", "--no-browser"]


def current_ssids() -> list[str]:
    """Best effort: the Wi-Fi network this computer is on (to prefill the panel's)."""
    try:
        if sys.platform == "win32":
            out = subprocess.run(["netsh", "wlan", "show", "interfaces"], capture_output=True, text=True,
                                 timeout=5, creationflags=0x08000000, check=False).stdout
            return [m.strip() for m in re.findall(r"^\s*SSID\s*:\s*(.+)$", out, re.MULTILINE)][:3]
        if sys.platform == "darwin":
            out = subprocess.run(["networksetup", "-getairportnetwork", "en0"], capture_output=True, text=True,
                                 timeout=5, check=False).stdout
            m = re.search(r"Network:\s*(.+)$", out.strip())
            return [m.group(1).strip()] if m else []
        if shutil.which("nmcli"):
            out = subprocess.run(["nmcli", "-t", "-f", "active,ssid", "dev", "wifi"], capture_output=True,
                                 text=True, timeout=5, check=False).stdout
            return [line.split(":", 1)[1] for line in out.splitlines() if line.startswith("yes:")][:3]
    except (OSError, subprocess.TimeoutExpired):
        pass
    return []


class Service:
    upload_paths = api.UPLOAD_PATHS

    def __init__(self, *, port: int | None = None, demo: bool = False, panel: bool = True, platform=None):
        paths.ensure_dirs()
        self.extensions = load_extensions()  # before Settings: they add settings keys
        self.settings = Settings(paths.settings_file())
        self.secrets = SecretStore(paths.secrets_file())
        self.state = StateStore(paths.cache_dir() / "state.json")
        self.hook_secret = self.secrets.hook_secret()
        self.port_wanted = port or (self.settings.section("server") or {}).get("port", DEFAULT_PORT)
        self.port = 0
        self.os = os_name()
        if platform is not None:
            self.platform, self.platform_error = platform, ""
        else:
            self.platform, self.platform_error = load_platform()
        self.demo = demo
        self.job = FirmwareJob()
        self.staged: dict = {}
        self.release_cache: tuple | None = None
        self.hook_errors: dict[str, str] = {}
        self.threads: list = []
        self._server = None
        self._stopping = threading.Event()

        label = (self.settings.section("ai") or {}).get("host_label") or host_name()[:19]
        self.bridge = Bridge(host=label)
        self.audio = _safe(self.platform.audio)
        self.system_media = _safe(self.platform.media)
        self.links = LinkManager(self.settings, self.secrets, self.state, enabled=panel or demo, demo=demo)
        self.board = DeckBoard(self.settings, self.platform, self.os, cache_dir=paths.cache_dir())
        self.spotify_auth = SpotifyAuth(self.secrets, lambda: (self.settings.section("music") or {}).get(
            "spotify_client_id", ""))
        self.spotify = SpotifyBackend(self.spotify_auth, want_artist=lambda: (self.settings.section(
            "music") or {}).get("backdrop") == "artist")
        self.likes = Likes(self.spotify_auth, self.spotify.api, on_change=lambda: self.media.poke())
        self.artists = ArtistImages(paths.cache_dir(), on_ready=lambda: self.media.poke())
        self.media = MediaCollector(self.settings, lambda: self.system_media,
                                    lambda: self.spotify if self.spotify_auth.signed_in() else None,
                                    artists=self.artists,
                                    likes=self.likes)
        self.usage = UsageCollector(self.settings, self.secrets)
        self.lab = LabCollector(self.settings, self.secrets)
        self.pc = PcCollector(audio=self.audio, settings=self.settings)
        self.sounds = Sounds(self.settings)
        self.sound_import = SoundImport(self.sounds.store)  # "Replace from link..."
        self.artwork = make_artwork()                        # pictures for imported sounds
        from .ai import icons as ai_icons
        self._ai_engines = lambda: ai_icons.find_engines()
        self._ai_renderer = lambda: ai_icons.renderer_available()
        self.ai_icons = ai_icons.IconJob(paths.cache_dir(), engines=lambda: self._ai_engines(),  # ✨ Generate with AI
                                         on_workdir=self.bridge.ignore_dirs.append)  # never on the AI page
        self.actions = Actions(bridge=self.bridge, audio=self.audio, media=self.media, board=self.board,
                               lab=self.lab, open_settings=self.open_settings_page, sounds=self.sounds)
        self.sources = {"pc": self.pc.snapshot, "lab": self.lab.snapshot, "usage": self.usage.snapshot,
                        "media": self.media.snapshot}
        for ext in self.extensions:
            ext.setup(self)
        self.ext_static = {f"/ext/{e.name}.{kind}": e.static / f"{e.name}.{kind}" for e in self.extensions
                           for kind in ("js", "css") if (e.static / f"{e.name}.{kind}").exists()}
        self.syncer = Syncer(self.links, self.bridge, self.actions, sources=self.sources,
                             deck=self.board.current, images=self._images)
        self.links.on_connect.append(self._on_connect)
        self.settings.on_change(self._on_settings)

    # ---- lifecycle ---------------------------------------------------------------------
    def start(self) -> int:
        self._server = bind_first_free(self, self.port_wanted, PORT_SPAN, api.ROUTES)
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, name="web", daemon=True).start()
        write_json_atomic(paths.runtime_file(), {"app": APP_ID, "port": self.port, "pid": os.getpid(),
                                                 "version": __version__, "started": int(time.time())}, private=False)
        self.board.start()
        self.sounds.warm_up()  # writes the built-in sounds' files the first time
        for t in [self.links, self.pc, self.lab, self.usage, self.media, *self.threads]:
            t.start()
        self.syncer.start()
        self._ensure_hooks()
        if self.spotify_auth.signed_in() and not self.state.get("spotify_name"):
            self.spotify_signed_in()
        # A fresh install of the packaged app starts with the computer (the Startup
        # section turns it off). Never for dev runs or PANDADECK_HOME test/portable runs.
        if (self.settings.first_run and getattr(sys, "frozen", False) and self.settings.section("startup")
                and not os.environ.get("PANDADECK_HOME")):
            try:
                self.set_autostart(True)
            except Exception as e:  # noqa: BLE001
                log.info("couldn't enable launch at login: %s", e)
        log.info("PandaDeck %s up: settings at %s", __version__, self.url())
        return self.port

    def stop(self) -> None:
        self._stopping.set()
        self.sounds.stop_all()
        for t in [self.syncer, self.links, self.pc, self.lab, self.usage, self.media, *self.threads]:
            stop = getattr(t, "stop", None)
            if stop:
                try:
                    stop()
                except Exception:  # noqa: BLE001
                    pass
        if self._server:
            self._server.shutdown()
            self._server.server_close()
        rt = read_json(paths.runtime_file(), {})
        if isinstance(rt, dict) and rt.get("pid") == os.getpid():
            try:
                paths.runtime_file().unlink()
            except OSError:
                pass

    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    SETTINGS_ANCHORS = {"music": "music-spotify"}  # settings:<section> from the panel -> page anchor

    def open_settings_page(self, section: str = "") -> None:
        """The panel's gear / an agent:settings button: this app's settings in the default browser
        (settings:music: at the Spotify sign-in, when ❤ Like isn't available yet)."""
        if not self.port:
            log.info("settings page not served (yet): nothing to open")
            return
        anchor = self.SETTINGS_ANCHORS.get(section, "")
        try:
            webbrowser.open(self.url() + (f"#{anchor}" if anchor else ""))
        except Exception as e:  # noqa: BLE001 - no browser on this system
            log.warning("couldn't open the settings page: %s", e)

    def redirect_uri(self) -> str:
        return redirect_uri_for(self.port or self.port_wanted)

    # ---- glue ---------------------------------------------------------------------------
    def _images(self) -> dict:
        out = self.board.images()
        out.update(self.media.images())
        return out

    def handle_hook(self, payload, src: str, alive) -> bytes:
        wait = float((self.settings.section("ai") or {}).get("perm_wait_s", 600))
        return self.bridge.handle_hook(payload, src, alive, wait, self.syncer.approvals_possible)

    def _on_connect(self, link, info: dict) -> None:
        self.syncer.new_link()
        panel = info.get("settings") or {}
        want = self.settings.section("pages")
        if panel.get("pages") is not None and panel.get("pages") != want:
            threading.Thread(target=self._push_config, args=({"pages": want},), daemon=True).start()
        # The panel's clock follows this computer's time zone (firmware that knows "tz" reports it;
        # older firmware would reject the whole update, so it only goes to panels that do).
        if "tz" in panel:
            tz = posix_tz()
            if tz and panel.get("tz") != tz:
                threading.Thread(target=self._push_config, args=({"tz": tz},), daemon=True).start()

    def _push_pages(self, pages: list[str]) -> None:
        self._push_config({"pages": pages})

    def _push_config(self, payload: dict) -> None:
        link = self.links.current()
        if link is None:
            return
        try:
            out = link.config(payload)
            if isinstance(out.get("settings"), dict):
                self.links.update_info(settings=out["settings"])
        except Unsupported:
            log.info("panel firmware doesn't know %s yet (update it)", ", ".join(payload))
        except LinkError as e:
            log.info("couldn't send %s to the panel: %s", ", ".join(payload), e)

    def _on_settings(self, old: dict, new: dict) -> None:
        if old["panel"] != new["panel"]:
            self.links.reconnect()
        if old["pages"] != new["pages"]:
            threading.Thread(target=self._push_pages, args=(new["pages"],), daemon=True).start()
        if old["deck"] != new["deck"]:
            self.board.rebuild()
            self._learn_pictures(old["deck"], new["deck"])
        if old["music"] != new["music"]:
            self.media.poke()
        if old["hud"] != new["hud"]:
            if _origin(old["hud"]["lab_url"]) and _origin(old["hud"]["lab_url"]) != _origin(new["hud"]["lab_url"]):
                self.secrets.clear("lab_token")  # a token never follows the URL to another server
            self.lab.poke()
        if old["startup"] != new["startup"] and self.autostart_enabled() != new["startup"]:
            try:
                self.platform.set_autostart(new["startup"], autostart_argv())
            except Exception as e:  # noqa: BLE001
                log.info("couldn't change launch at login: %s", e)
        oa, na = old["ai"], new["ai"]
        if (oa["claude_hooks"], oa["codex_hooks"], oa["perm_wait_s"]) != (na["claude_hooks"], na["codex_hooks"],
                                                                         na["perm_wait_s"]):
            self._apply_hooks(oa, na)
        if any(oa[k] != na[k] for k in ("claude_usage", "codex_usage", "claude_usage_url", "codex_usage_url")):
            self.usage.poke()
        if na["host_label"] != oa["host_label"]:
            self.bridge.host = na["host_label"] or host_name()[:19]

    # ---- hooks --------------------------------------------------------------------------
    def _apply_hooks(self, old_ai: dict | None, new_ai: dict) -> None:
        wait = int(new_ai["perm_wait_s"])
        for name, fn in (("claude", ai_hooks.install_claude), ("codex", ai_hooks.install_codex)):
            key = f"{name}_hooks"
            was = bool(old_ai and old_ai[key])
            try:
                if new_ai[key]:
                    fn(self.port or self.port_wanted, self.hook_secret, wait)
                elif was:  # only on an explicit switch-off: never touch hooks we didn't add this run
                    fn(self.port or self.port_wanted, self.hook_secret, wait, uninstall=True)
                self.hook_errors.pop(name, None)
            except (OSError, ValueError) as e:
                log.warning("%s hooks: %s", name, e)
                self.hook_errors[name] = f"Couldn't update the {name.title()} settings file: {e}"

    def _ensure_hooks(self) -> None:
        """Enabled hooks must point at this run's port/secret (the port can move)."""
        ai = self.settings.section("ai") or {}
        if not (ai.get("claude_hooks") or ai.get("codex_hooks")):
            return
        st = ai_hooks.status(self.port, self.hook_secret)
        stale = (ai.get("claude_hooks") and not st["claude"]["current"]) or \
                (ai.get("codex_hooks") and not st["codex"]["current"])
        if stale:
            self._apply_hooks(None, ai)

    # ---- secrets / spotify -----------------------------------------------------------------
    def check_settings(self, new: dict) -> None:
        """Rules that need live data; raises SettingsError to refuse an update."""
        old_targets = (self.settings.section("deck") or {}).get("targets") or {}
        for tid, t in (new["deck"].get("targets") or {}).items():
            if t.get("kind") in ("file", "folder") and old_targets.get(tid) != t:
                p = Path(t["path"]).expanduser()
                if not (p.is_dir() if t["kind"] == "folder" else p.is_file()):
                    raise SettingsError(f"{t['path']} doesn't exist (or isn't a {t['kind']}) on this computer")
        need = self.board.icon_demand(new["deck"])
        if need > ICON_SLOTS and need > self.board.icon_demand(self.settings.section("deck") or {}):
            raise SettingsError(
                f"The panel holds {ICON_SLOTS} button pictures (app icons and your own pictures) and this "
                f"would need {need}. Remove a few app buttons or pictures first.")

    def ai_icons_meta(self) -> dict:
        """Which CLIs can make button pictures here: Codex (its own image generation) whenever it's
        installed; Claude (vector icons) only with an SVG renderer."""
        from .ai.icons import ENGINES, KINDS
        renderer = self._ai_renderer()
        installed = self._ai_engines()
        engines = [{"id": e, "name": ENGINES[e], "kind": KINDS[e]} for e in ENGINES
                   if e in installed and (KINDS[e] == "picture" or renderer)]
        return {"engines": engines, "renderer": renderer}

    def ai_reference(self, info: dict) -> bytes | None:
        """A real picture of the subject for Codex to draw from: the button's own picture (never one the AI
        made: it would only copy itself), else a confident meme-catalog match for a sound."""
        import re as _re

        from .deck import userimg
        from .sounds.artwork import CONFIDENT
        image = str(info.get("image") or "")
        if _re.fullmatch(r"[0-9a-f]{40}", image) and info.get("image_origin") != "ai":
            data = userimg.png_bytes(image)
            if data:
                return data
        if info.get("kind") == "sound":
            for score, entry in self.artwork.catalog.match([str(info.get("label") or ""), str(info.get("sound") or "")]):
                if score >= CONFIDENT and entry.get("image_id"):
                    data = userimg.png_bytes(entry["image_id"])
                    if data:
                        return data
        return None

    def import_meme_sources(self, path) -> dict:
        """sources.json picture mappings -> the meme catalog (idempotent)."""
        from .deck import userimg
        from .sounds.artwork import import_sources

        def has(image_id: str) -> bool:
            p = userimg.path_for(image_id)
            return p is not None and p.is_file()

        return import_sources(self.artwork.catalog, Path(path), has, userimg.save_upload)

    def _learn_pictures(self, old: dict, new: dict) -> None:
        """A picture the user picked for a sound teaches the meme catalog that name."""
        lib = self.sounds.library()
        for sid, e in (new.get("sounds") or {}).items():
            before = (old.get("sounds") or {}).get(sid) or {}
            if e.get("image") and e["image"] != before.get("image") and not e.get("image_auto"):
                label = e.get("label") or (lib.get(sid) or ("",))[0]
                try:
                    self.artwork.catalog.learn(label, e["image"])
                except OSError as err:
                    log.info("meme catalog not saved: %s", err)

    def secret_changed(self, name: str) -> None:
        if name == "claude_token":
            self.usage.poke()
        elif name == "lab_token":
            self.lab.poke()
        elif name == "panel_token":
            self.links.reconnect()

    def spotify_signed_in(self) -> None:
        def work():
            name = self.spotify.profile()
            if name:
                self.state.set("spotify_name", name)
        threading.Thread(target=work, daemon=True).start()
        self.media.poke()

    # ---- startup -------------------------------------------------------------------------------
    def set_autostart(self, enabled: bool) -> None:
        self.platform.set_autostart(enabled, autostart_argv())
        if self.settings.section("startup") != enabled:
            self.settings.update({"startup": enabled})

    def autostart_enabled(self) -> bool:
        try:
            return bool(self.platform.autostart_enabled())
        except Exception:  # noqa: BLE001
            return False

    def open_folder(self, what: str) -> None:
        osutil.open_uri(str(paths.config_dir() if what == "config" else paths.log_dir()))

    def wifi_suggestions(self) -> list[str]:
        out = []
        panel_ssid = ((self.links.status().get("info") or {}).get("wifi") or {}).get("ssid")
        for s in [panel_ssid, *current_ssids()]:
            if s and s not in out:
                out.append(s)
        return out

    # ---- status for the settings page --------------------------------------------------------
    def meta(self) -> dict:
        defaults = self.board.defaults
        builtins = {pid: builtin_page(defaults, pid, self.os) for pid in ("media", "windows", "dev", "macros")}
        return {
            "version": __version__, "os": self.os, "port": self.port, "redirect_uri": self.redirect_uri(),
            "hostname": self.pc.hostname,
            "config_dir": paths.display(paths.config_dir()), "log_dir": paths.display(paths.log_dir()),
            "page_ids": list(PAGE_IDS), "icons": list(ICON_NAMES), "builtin_pages": builtins,
            "icon_slots": ICON_SLOTS, "runs_code": list(osutil.RUNS_CODE),
            "sound_library": {k: {"label": v[0], "icon": v[1]} for k, v in self.sounds.library().items()},
            "sound_page": {"color": SOUND_PAGE_COLOR, "volume": SOUND_VOLUME},
            "ai_icons": self.ai_icons_meta(),
            "file_icons": callable(getattr(self.platform, "file_icon", None)),
            "releases_api": RELEASES_API,
            "page_info": {k: v for e in self.extensions for k, v in getattr(e, "page_info", {}).items()},
            "codex_hook_cmd": "curl.exe" if sys.platform == "win32" else "curl",
        }

    def status(self) -> dict:
        link = self.links.status()
        info = link.get("info") or {}
        sessions, perms = self.bridge.snapshot()
        media_snap = self.media.snapshot()
        return {
            "version": __version__, "os": self.os, "port": self.port, "demo": self.demo,
            "platform": {"ok": not self.platform_error, "error": paths.scrub(self.platform_error)},
            "panel": {**link, "fw": info.get("fw", ""), "features": info.get("features") or [],
                      "settings": info.get("settings") or {}, "wifi": info.get("wifi") or {},
                      "alive": self.syncer.panel_alive(), "has_token": self.secrets.has("panel_token"),
                      "last_ip": self.state.get("last_ip", "")},
            "job": self.job.status(),
            "music": {
                "available": self.system_media is not None,
                "active": self.media.active_name,
                "backdrop_source": self.media.backdrop_source,
                "now": media_snap if media_snap and media_snap.get("ok") else None,
                "spotify": {"signed_in": self.spotify_auth.signed_in(), "name": self.state.get("spotify_name", ""),
                            "error": self.spotify.error, "last_result": self.spotify_auth.last_result,
                            "can_like": self.spotify_auth.can_like(), "like_error": self.likes.error},
            },
            "ai": {
                "sessions": len(sessions), "perms": len(perms),
                "session_list": [{k: s[k] for k in ("proj", "st", "src", "act")} for s in sessions[:5]],
                "hooks": _display_hooks(ai_hooks.status(self.port, self.hook_secret)),
                "hook_errors": {k: paths.scrub(v) for k, v in self.hook_errors.items()},
                "usage": {"claude": _usage_public(self.usage.status["claude"]),
                          "codex": _usage_public(self.usage.status["codex"])},
                "values": self.usage.snapshot(),
                "codex_auth": read_codex_auth() is not None, "claude_cli": bool(claude_cli()),
                "last_event": dict(self.bridge.last_event_at),
            },
            "hud": {"pc": self.pc.snapshot(), "lab": {**self.lab.status, "data": self.sources["lab"]()}},
            "deck": {"ready": self.board.catalog_ready(), "error": self.board.error,
                     "pages": [p["name"] for p in (self.board.current() or {}).get("pages", [])],
                     "picture_choices": sorted(self.artwork.pending)},  # sound files with pictures to choose from
            "startup": {"enabled": self.autostart_enabled(), "supported": not isinstance(self.platform, NullPlatform)},
            "ext": {e.name: _safe_status(e, self) for e in self.extensions},
        }

    def extensions_public(self) -> list[dict]:
        return [{"name": e.name, "title": e.title, "script": f"/ext/{e.name}.js",
                 "style": f"/ext/{e.name}.css" if f"/ext/{e.name}.css" in self.ext_static else ""}
                for e in self.extensions if f"/ext/{e.name}.js" in self.ext_static]


def _origin(url: str) -> str:
    import urllib.parse
    u = urllib.parse.urlsplit(url or "")
    return f"{u.scheme}://{u.netloc}".lower() if u.scheme and u.netloc else ""


def _usage_public(st: dict) -> dict:
    return {"state": st.get("state"), "error": paths.scrub(st.get("error") or ""), "at": st.get("at")}


def _display_hooks(st: dict) -> dict:
    out = dict(st)
    for name in ("claude", "codex"):
        if isinstance(out.get(name), dict):
            out[name] = {**out[name], "path": paths.display(out[name].get("path", ""))}
    return out


def _safe_status(ext, svc) -> dict:
    try:
        return ext.status(svc)
    except Exception as e:  # noqa: BLE001 - an extension must never break the page
        log.debug("extension %s status failed: %s", ext.name, e)
        return {}


def _safe(fn):
    try:
        return fn()
    except Exception as e:  # noqa: BLE001
        log.info("%s unavailable: %s", getattr(fn, "__name__", "backend"), e)
        return None


def running_instance() -> dict | None:
    """The runtime file of another live instance, if it answers /api/ping."""
    rt = read_json(paths.runtime_file(), None)
    if not isinstance(rt, dict) or not isinstance(rt.get("port"), int):
        return None
    from .util import http_json
    try:
        status, data = http_json(f"http://127.0.0.1:{rt['port']}/api/ping", timeout=1.5)
    except (OSError, ValueError, http.client.HTTPException):  # something else holds that port now
        return None
    if status == 200 and isinstance(data, dict) and data.get("app") == APP_ID:
        return {**rt, "port": data.get("port") or rt["port"]}
    return None


def settings_url() -> str | None:
    inst = running_instance()
    return f"http://127.0.0.1:{inst['port']}/" if inst else None


def dump_status(svc: Service) -> str:  # debugging aid for `--status`
    return json.dumps(svc.status(), indent=1, default=str)


def make_artwork() -> Artwork:
    from .deck import userimg

    def has_picture(image_id: str) -> bool:
        p = userimg.path_for(image_id)
        return p is not None and p.is_file()

    return Artwork(Catalog(), paths.cache_dir(), lambda: Fetcher(None), userimg.save_upload, has_picture)
