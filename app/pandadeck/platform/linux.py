"""Linux backend (see docs/app.md).

* Apps: visible .desktop entries from the XDG data dirs (+ flatpak/snap
  exports), id = desktop-file ID (_desktop). Score from GNOME Shell's
  application_state, KDE's activity database and recently-used.xbel.
* Icons: Icon= through the icon theme (_icons); absolute paths as-is.
* Launch: gtk-launch <id> if present, else the parsed Exec (never a shell).
* Media: MPRIS via jeepney, else playerctl (_mpris).
* Audio: pactl (PulseAudio / PipeWire-pulse), else wpctl (WirePlumber).
* Autostart: $XDG_CONFIG_HOME/autostart/pandadeck.desktop.
"""
from __future__ import annotations

import logging
import mimetypes
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Callable, Mapping

from . import _desktop as desk
from . import _icons as ic
from ._util import TTLCache, normalize_path, run, spawn, validate_argv, write_atomic
from .base import AppInfo, AudioBackend, MediaBackend

log = logging.getLogger("pandadeck.platform.linux")

APPS_TTL_S = 300.0
AUDIO_TTL_S = 0.8
GTK_LAUNCH_WAIT_S = 3.0
MAX_STATE_BYTES = 8 * 1024 * 1024
AUTOSTART_NAME = "pandadeck.desktop"
XBEL_NS = "{http://www.freedesktop.org/standards/desktop-bookmarks}"
KDE_DB = ".local/share/kactivitymanagerd/resources/database"


# ---- usage scores (pure parsers + small readers) ----------------------------------------------------

def parse_gnome_app_state(text: str) -> dict[str, float]:
    """~/.local/share/gnome-shell/application_state -> {desktop id: score}."""
    out: dict[str, float] = {}
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return out
    for app in root.iter("application"):
        app_id = app.get("id") or ""
        try:
            score = float(app.get("score") or 0)
        except ValueError:
            continue
        if app_id:
            out[app_id] = out.get(app_id, 0.0) + max(score, 0.0)
    return out


def parse_xbel_apps(text: str) -> dict[str, int]:
    """recently-used.xbel -> {program basename: total count} from the Exec of each app."""
    out: dict[str, int] = {}
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return out
    for el in root.iter(f"{XBEL_NS}application"):
        prog = exec_basename(el.get("exec") or "")
        try:
            count = int(el.get("count") or 1)
        except ValueError:
            count = 1
        if prog:
            out[prog] = out.get(prog, 0) + max(count, 0)
    return out


def exec_basename(exec_value: str) -> str:
    """'\\'firefox %u\\'' / '/usr/bin/gedit %U' -> 'firefox' / 'gedit'."""
    value = exec_value.strip().strip("'\"")
    try:
        args = desk.split_exec(value)
    except ValueError:
        args = value.split()
    for a in args:
        if a in ("env", "flatpak", "run") or "=" in a or a.startswith("-"):
            continue
        return os.path.basename(a)
    return ""


def kde_scores(db_path: Path) -> dict[str, float]:
    """KDE activity manager's per-app scores (read-only; best effort)."""
    out: dict[str, float] = {}
    if not db_path.is_file():
        return out
    try:
        conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True, timeout=1)
        try:
            rows = conn.execute("SELECT targettedResource, cachedScore FROM ResourceScoreCache "
                                "WHERE targettedResource LIKE 'applications:%'").fetchall()
        finally:
            conn.close()
    except sqlite3.Error as e:
        log.debug("KDE activity db unreadable: %s", e)
        return out
    for res, score in rows:
        app_id = str(res).split(":", 1)[1]
        if not app_id.endswith(".desktop"):
            app_id += ".desktop"
        out[app_id] = out.get(app_id, 0.0) + float(score or 0)
    return out


def _read_small(path: Path) -> str | None:
    try:
        if path.stat().st_size > MAX_STATE_BYTES:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def score_entries(entries: Mapping[str, desk.DesktopEntry], gnome: Mapping[str, float],
                  kde: Mapping[str, float], xbel: Mapping[str, int]) -> list[AppInfo]:
    """Pure: entries + usage sources -> AppInfo list, most used first."""
    out = []
    for app_id, e in entries.items():
        score = gnome.get(app_id, 0.0) + kde.get(app_id, 0.0)
        prog = exec_basename(e.exec) if e.exec else ""
        if prog:
            score += xbel.get(prog, 0)
        out.append(AppInfo(id=app_id, name=e.name, score=round(score, 3)))
    return sorted(out, key=lambda a: (-a.score, a.name.lower()))


# ---- audio -------------------------------------------------------------------------------------------

def parse_pactl_volume(text: str | None) -> int:
    """`pactl get-sink-volume` -> average channel percent, -1 if unknown."""
    pcts = [int(p) for p in re.findall(r"(\d+)%", text or "")]
    return min(100, int(round(sum(pcts) / len(pcts)))) if pcts else -1   # can be boosted past 100


def parse_pactl_mute(text: str | None) -> bool | None:
    m = re.search(r"Mute:\s*(yes|no)", text or "")
    return None if m is None else m.group(1) == "yes"


def parse_wpctl_volume(text: str | None) -> tuple[int, bool] | None:
    """`wpctl get-volume` -> (percent, muted)."""
    m = re.search(r"Volume:\s*([0-9.]+)", text or "")
    if m is None:
        return None
    return min(100, int(round(float(m.group(1)) * 100))), "[MUTED]" in (text or "")


class _Cached:
    def __init__(self, ttl: float):
        self._ttl, self._at, self._value = ttl, 0.0, None
        self._lock = threading.Lock()

    def get(self, load: Callable[[], object]):
        with self._lock:
            if time.monotonic() - self._at >= self._ttl:
                self._value, self._at = load(), time.monotonic()
            return self._value

    def clear(self) -> None:
        with self._lock:
            self._at = 0.0


class PactlAudio:
    def __init__(self, pactl: str):
        self._pactl = pactl
        self._vol, self._mic = _Cached(AUDIO_TTL_S), _Cached(AUDIO_TTL_S)

    def volume(self) -> int:
        return self._vol.get(lambda: parse_pactl_volume(
            run([self._pactl, "get-sink-volume", "@DEFAULT_SINK@"], timeout=2, c_locale=True)))

    def mic_muted(self) -> bool | None:
        return self._mic.get(lambda: parse_pactl_mute(
            run([self._pactl, "get-source-mute", "@DEFAULT_SOURCE@"], timeout=2, c_locale=True)))

    def toggle_mic(self) -> None:
        run([self._pactl, "set-source-mute", "@DEFAULT_SOURCE@", "toggle"], timeout=2)
        self._mic.clear()

    def set_volume(self, level: int) -> None:
        run([self._pactl, "set-sink-volume", "@DEFAULT_SINK@", f"{int(level)}%"], timeout=2)
        self._vol.clear()


class WpctlAudio:
    def __init__(self, wpctl: str):
        self._wpctl = wpctl
        self._vol, self._mic = _Cached(AUDIO_TTL_S), _Cached(AUDIO_TTL_S)

    def volume(self) -> int:
        r = self._vol.get(lambda: parse_wpctl_volume(
            run([self._wpctl, "get-volume", "@DEFAULT_AUDIO_SINK@"], timeout=2, c_locale=True)))
        return r[0] if r else -1

    def mic_muted(self) -> bool | None:
        r = self._mic.get(lambda: parse_wpctl_volume(
            run([self._wpctl, "get-volume", "@DEFAULT_AUDIO_SOURCE@"], timeout=2, c_locale=True)))
        return r[1] if r else None

    def toggle_mic(self) -> None:
        run([self._wpctl, "set-mute", "@DEFAULT_AUDIO_SOURCE@", "toggle"], timeout=2)
        self._mic.clear()

    def set_volume(self, level: int) -> None:
        run([self._wpctl, "set-volume", "@DEFAULT_AUDIO_SINK@", f"{int(level) / 100:.2f}"], timeout=2)
        self._vol.clear()


# ---- autostart ----------------------------------------------------------------------------------------

def autostart_desktop(argv: list[str]) -> str:
    """Contents of ~/.config/autostart/pandadeck.desktop for this command line."""
    argv = validate_argv(argv)
    return ("[Desktop Entry]\n"
            "Type=Application\n"
            "Name=PandaDeck\n"
            "Comment=PandaDeck panel companion\n"
            f"Exec={desk.exec_value(argv)}\n"
            "Terminal=false\n"
            "NoDisplay=false\n"
            "Hidden=false\n"
            "X-GNOME-Autostart-enabled=true\n"
            "X-GNOME-Autostart-Delay=3\n")


def autostart_active(text: str) -> bool:
    raw = desk.parse_group(text)
    if not raw or desk.is_true(raw.get("Hidden")):
        return False
    return (raw.get("X-GNOME-Autostart-enabled", "true").strip().lower() != "false"
            and bool(raw.get("Exec")))


# ---- the platform -------------------------------------------------------------------------------------

class LinuxPlatform:
    name = "linux"

    def __init__(self, home: Path | None = None, env: Mapping[str, str] | None = None,
                 which: Callable[[str], str | None] = shutil.which):
        self._home = Path(home) if home is not None else Path.home()
        self._env = dict(os.environ if env is None else env)
        self._which = which
        self._apps: TTLCache[dict[str, desk.DesktopEntry]] = TTLCache(APPS_TTL_S, {})
        self._usage_cache: TTLCache[tuple] = TTLCache(APPS_TTL_S, ({}, {}, {}))
        self._lock = threading.Lock()
        self._icons = None
        self._generic_icons: dict[str, str] | None = None
        self._media: MediaBackend | None = None
        self._media_tried = False
        self._audio: AudioBackend | None = None
        self._audio_tried = False

    # apps --------------------------------------------------------------------------------------------
    def _data_dirs(self) -> list[Path]:
        return desk.data_dirs(self._env, self._home)

    def _scan(self) -> dict[str, desk.DesktopEntry]:
        return desk.scan(self._data_dirs(), desk.current_desktops(self._env),
                         desk.locale_candidates(self._env), self._which)

    def _usage(self) -> tuple[dict[str, float], dict[str, float], dict[str, int]]:
        data_home = Path(self._env.get("XDG_DATA_HOME") or self._home / ".local/share")
        gnome_text = _read_small(data_home / "gnome-shell" / "application_state")
        xbel_text = _read_small(data_home / "recently-used.xbel")
        return (parse_gnome_app_state(gnome_text) if gnome_text else {},
                kde_scores(self._home / KDE_DB),
                parse_xbel_apps(xbel_text) if xbel_text else {})

    def list_apps(self) -> list[AppInfo]:
        entries = self._apps.get(self._scan)
        return score_entries(entries, *self._usage_cache.get(self._usage))

    def _entry(self, app_id: str) -> desk.DesktopEntry:
        entry = self._apps.get(self._scan).get(app_id)
        if entry is None:
            entry = self._apps.get(self._scan, force=True).get(app_id)
        if entry is None:
            raise ValueError(f"unknown app id {app_id!r}")
        return entry

    def _icon_finder(self):
        with self._lock:
            if self._icons is None:
                themes = ic.current_icon_themes(self._home, self._env, self._which)
                self._icons = ic.IconFinder(self._data_dirs(), self._home, themes, self._which)
            return self._icons

    def app_icon(self, app_id: str):
        try:
            entry = self._entry(app_id)
        except ValueError:
            return None
        return self._icon_finder().load(entry.icon)

    # file / folder icons ---------------------------------------------------------------------------
    def _mime_type(self, path: str) -> str | None:
        """xdg-mime (content sniffing, the desktop's own answer), else by extension."""
        xdg = self._which("xdg-mime")
        if xdg:
            out = run([xdg, "query", "filetype", path], timeout=3, c_locale=True)
            mime = (out or "").strip().split(";")[0].strip()
            if "/" in mime:
                return mime
        return mimetypes.guess_type(path)[0]

    def _mime_generic_icons(self) -> dict[str, str]:
        if self._generic_icons is None:
            merged: dict[str, str] = {}
            for d in reversed(self._data_dirs()):  # earlier dirs win
                text = _read_small(d / "mime" / "generic-icons")
                if text:
                    merged.update(ic.parse_generic_icons(text))
            self._generic_icons = merged
        return self._generic_icons

    def _file_icon_names(self, path: str) -> list[str]:
        if os.path.isdir(path):
            config_home = Path(self._env.get("XDG_CONFIG_HOME") or self._home / ".config")
            text = _read_small(config_home / "user-dirs.dirs") or ""
            return ic.folder_icon_names(path, str(self._home), ic.parse_user_dirs(text, str(self._home)))
        names: list[str] = []
        if path.endswith(".desktop"):  # a launcher file: use the app's own icon
            entry = desk.load_entry(Path(path), os.path.basename(path))
            if entry is not None and entry.icon:
                names.append(entry.icon)
        executable = os.access(path, os.X_OK) and os.name == "posix"
        return names + ic.file_icon_names(self._mime_type(path), self._mime_generic_icons(), executable)

    def file_icon(self, path: str):
        """Theme icon for a file's MIME type (or a folder / a .desktop's own icon)."""
        p = normalize_path(path)
        if p is None:
            return None
        try:
            names = self._file_icon_names(p)
        except OSError as e:
            log.debug("file icon for %s: %s", p, e)
            return None
        finder = self._icon_finder()
        for name in names:
            img = finder.load(name)
            if img is not None:
                return img
        return None

    def launch_app(self, app_id: str) -> None:
        entry = self._entry(app_id)
        env = {**self._env, "XDG_DATA_DIRS": ":".join(str(d) for d in self._data_dirs()[1:])}
        gtk_launch = self._which("gtk-launch")
        if gtk_launch:
            try:
                proc = spawn([gtk_launch, app_id], env=env)
                if proc.wait(timeout=GTK_LAUNCH_WAIT_S) == 0:
                    log.info("launched %s via gtk-launch", app_id)
                    return
            except subprocess.TimeoutExpired:
                return  # still starting: treat as launched
            except OSError as e:
                log.debug("gtk-launch failed: %s", e)
        if entry.exec:
            argv = entry.command(self._which)
        elif self._which("gio"):
            argv = [self._which("gio") or "gio", "launch", entry.path]
        else:
            raise OSError(f"{app_id}: nothing to run (DBus-activatable only)")
        cwd = entry.workdir if entry.workdir and os.path.isdir(entry.workdir) else None
        spawn(argv, cwd=cwd)
        log.info("launched %s", app_id)

    # media / audio -----------------------------------------------------------------------------------------
    def media(self) -> MediaBackend | None:
        with self._lock:
            if not self._media_tried:
                self._media_tried = True
                from ._mpris import MprisMedia

                self._media = MprisMedia.create()
            return self._media

    def audio(self) -> AudioBackend | None:
        with self._lock:
            if not self._audio_tried:
                self._audio_tried = True
                self._audio = self._pick_audio()
            return self._audio

    def _pick_audio(self) -> AudioBackend | None:
        pactl = self._which("pactl")
        if pactl and run([pactl, "get-sink-volume", "@DEFAULT_SINK@"], timeout=2, c_locale=True) is not None:
            return PactlAudio(pactl)
        wpctl = self._which("wpctl")
        if wpctl and run([wpctl, "get-volume", "@DEFAULT_AUDIO_SINK@"], timeout=2, c_locale=True) is not None:
            return WpctlAudio(wpctl)
        return None

    # autostart -------------------------------------------------------------------------------------------------
    def _autostart_path(self) -> Path:
        config_home = Path(self._env.get("XDG_CONFIG_HOME") or self._home / ".config")
        return config_home / "autostart" / AUTOSTART_NAME

    def set_autostart(self, enabled: bool, argv: list[str]) -> None:
        path = self._autostart_path()
        if enabled:
            write_atomic(path, autostart_desktop(argv).encode("utf-8"))
        else:
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    def autostart_enabled(self) -> bool:
        try:
            return autostart_active(self._autostart_path().read_text(encoding="utf-8", errors="replace"))
        except OSError:
            return False
