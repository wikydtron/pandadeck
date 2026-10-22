"""Windows backend (see docs/app.md).

* Apps: every Start-menu entry (Get-StartApps), scored by Windows' own
  UserAssist counters (focus time, then launches; ROT13-encoded value names
  in HKCU).
* Icons: the shell's own rendering of shell:AppsFolder\\<AppID> (_win_shell).
* Launch: explorer.exe shell:AppsFolder\\<AppID>, only for discovered apps.
* Media: GSMTC via winrt (_win_media). Audio: Core Audio via pycaw (_win_audio).
* Autostart: HKCU\\...\\CurrentVersion\\Run value "PandaDeck".

winreg/ctypes/winrt/comtypes are only imported inside functions, so this
module imports on every OS.
"""
from __future__ import annotations

import codecs
import json
import logging
import os
import subprocess
import threading
from typing import Iterable

from ._util import NO_WINDOW, TTLCache, clean, normalize_path, spawn, validate_argv
from .base import AppInfo, AudioBackend, MediaBackend

log = logging.getLogger("pandadeck.platform.win")

USERASSIST = (r"Software\Microsoft\Windows\CurrentVersion\Explorer\UserAssist"
              r"\{CEBFF5CD-ACE2-4F4F-9178-9926F41749EA}\Count")
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
RUN_VALUE = "PandaDeck"
# Shell plumbing / installers that show up in UserAssist or the Start menu but aren't apps.
SKIP_SUBSTR = ("ShellExperienceHost", "StartMenuExperienceHost", "CortanaUI", "UEME_", "\\Temp\\",
               "Setup.exe", "Uninstall", "unins000")
SKIP_NAME_SUBSTR = ("uninstall",)
# Start-menu entries that are documents or web links, not programs (games via steam:// stay).
DOC_EXTS = (".chm", ".txt", ".rtf", ".htm", ".html", ".url", ".pdf", ".hlp", ".md", ".log")
WEB_PREFIXES = ("http://", "https://")
APPS_TTL_S = 300.0
START_APPS_TIMEOUT_S = 40
START_APPS_PS = ("[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
                 "Get-StartApps | Select-Object Name,AppID | ConvertTo-Json -Compress")


# ---- pure helpers (unit-tested on every OS) ------------------------------------------------

def rot13(s: str) -> str:
    return codecs.decode(s, "rot13")


def parse_userassist_value(val: object) -> tuple[int, int] | None:
    """UserAssist v5 (Win7+) value -> (focus_ms, runs); None if not that format."""
    if not isinstance(val, (bytes, bytearray)) or len(val) < 16:
        return None
    runs = int.from_bytes(val[4:8], "little", signed=True)
    focus = int.from_bytes(val[12:16], "little", signed=True)
    return max(focus, 0), max(runs, 0)


def decode_userassist(values: Iterable[tuple[str, object]]) -> dict[str, tuple[int, int]]:
    """(ROT13 name, raw value) pairs -> {AppID or path: (focus_ms, runs)}."""
    out: dict[str, tuple[int, int]] = {}
    for name, val in values:
        parsed = parse_userassist_value(val)
        if parsed is None:
            continue
        key = rot13(name)
        prev = out.get(key, (0, 0))
        out[key] = (prev[0] + parsed[0], prev[1] + parsed[1])
    return out


def usage_score(focus_ms: int, runs: int) -> float:
    """Focus seconds dominate; launches only break ties."""
    return round(focus_ms / 1000.0 + min(runs, 999) / 1000.0, 3)


def parse_start_apps(text: str) -> dict[str, str]:
    """Get-StartApps JSON -> {AppID: name}."""
    rows = json.loads(text or "[]")
    if isinstance(rows, dict):
        rows = [rows]
    out: dict[str, str] = {}
    for r in rows if isinstance(rows, list) else []:
        if isinstance(r, dict) and r.get("AppID") and r.get("Name"):
            out[str(r["AppID"])] = clean(r["Name"])
    return out


def is_app_entry(app_id: str, name: str) -> bool:
    """Pure: False for installers, docs, web links and network paths in the Start menu."""
    low = app_id.lower()
    if any(s in app_id for s in SKIP_SUBSTR) or any(s in name.lower() for s in SKIP_NAME_SUBSTR):
        return False
    if low.endswith(DOC_EXTS) or low.startswith(WEB_PREFIXES) or app_id.startswith("\\\\"):
        return False
    return True


def build_app_list(catalog: dict[str, str], usage: dict[str, tuple[int, int]]) -> list[AppInfo]:
    """Start-menu catalog + UserAssist usage -> AppInfo list, most used first.
    Non-apps are dropped; duplicate names keep the most used entry."""
    best: dict[str, AppInfo] = {}
    for app_id, name in catalog.items():
        if not is_app_entry(app_id, name):
            continue
        focus, runs = usage.get(app_id, (0, 0))
        info = AppInfo(id=app_id, name=name, score=usage_score(focus, runs))
        cur = best.get(name.lower())
        if cur is None or info.score > cur.score:
            best[name.lower()] = info
    return sorted(best.values(), key=lambda a: (-a.score, a.name.lower()))


def media_app_name(aumid: str, apps: list[AppInfo]) -> str | None:
    """Pure: Start-menu name for a media session's AppUserModelID. Exact id
    first; else a unique match on the part after the vendor prefix (Chromium
    forks report "Chromium.<hash>" for a Start entry "<BrowserName>.<hash>")."""
    if not aumid:
        return None
    for a in apps:
        if a.id == aumid:
            return a.name
    _, dot, tail = aumid.partition(".")
    if not dot or len(tail) < 12:
        return None
    hits = {a.name for a in apps if a.id.partition(".")[2] == tail}
    return hits.pop() if len(hits) == 1 else None


def approved_disabled(data: object) -> bool:
    """StartupApproved\\Run value: first byte odd (0x03/0x07) = disabled in Task Manager."""
    return isinstance(data, (bytes, bytearray)) and len(data) > 0 and bool(data[0] & 1)


def run_command(argv: list[str]) -> str:
    return subprocess.list2cmdline(validate_argv(argv))


# ---- OS access -----------------------------------------------------------------------------

class Registry:
    """Thin HKCU wrapper (tests substitute a fake with the same methods)."""

    def get(self, key: str, name: str) -> object | None:
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
                return winreg.QueryValueEx(k, name)[0]
        except OSError:
            return None

    def set_str(self, key: str, name: str, value: str) -> None:
        import winreg

        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, name, 0, winreg.REG_SZ, value)

    def delete(self, key: str, name: str) -> None:
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_SET_VALUE) as k:
                winreg.DeleteValue(k, name)
        except FileNotFoundError:
            pass

    def values(self, key: str) -> list[tuple[str, object]]:
        import winreg

        out: list[tuple[str, object]] = []
        try:
            k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key)
        except OSError:
            return out
        with k:
            i = 0
            while True:
                try:
                    name, val, _ = winreg.EnumValue(k, i)
                except OSError:
                    break
                out.append((name, val))
                i += 1
        return out


def start_apps() -> dict[str, str]:
    """AppID -> display name for everything in the Start menu (raises on failure)."""
    proc = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", START_APPS_PS],
        capture_output=True, timeout=START_APPS_TIMEOUT_S, creationflags=NO_WINDOW, stdin=subprocess.DEVNULL,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"Get-StartApps failed ({proc.returncode})")
    catalog = parse_start_apps(proc.stdout.decode("utf-8-sig", errors="replace"))
    if not catalog:
        raise RuntimeError("Get-StartApps returned nothing")
    return catalog


# ---- the platform --------------------------------------------------------------------------

class WindowsPlatform:
    name = "windows"

    def __init__(self, registry: Registry | None = None):
        self._reg = registry or Registry()
        self._apps: TTLCache[list[AppInfo]] = TTLCache(APPS_TTL_S, [])
        self._icons_lock = threading.Lock()
        self._backend_lock = threading.Lock()
        self._icons = None
        self._media: MediaBackend | None = None
        self._media_tried = False
        self._audio: AudioBackend | None = None
        self._audio_tried = False

    # apps -------------------------------------------------------------------------------------
    def _load_apps(self) -> list[AppInfo]:
        usage = decode_userassist(self._reg.values(USERASSIST))
        return build_app_list(start_apps(), usage)

    def list_apps(self) -> list[AppInfo]:
        return list(self._apps.get(self._load_apps))

    def _known(self, app_id: str) -> bool:
        if any(a.id == app_id for a in self._apps.get(self._load_apps)):
            return True
        return any(a.id == app_id for a in self._apps.get(self._load_apps, force=True))

    def _shell_icons(self):
        with self._icons_lock:
            if self._icons is None:
                from ._win_shell import ShellIcons

                self._icons = ShellIcons()
            return self._icons

    def app_icon(self, app_id: str):
        return self._shell_icons().icon(app_id)

    def file_icon(self, path: str):
        """Shell icon of any file-system item (.exe/.lnk/.url/.bat, documents,
        folders), on the same STA icon thread as app icons."""
        p = normalize_path(path)
        return self._shell_icons().path_icon(p) if p else None

    def launch_app(self, app_id: str) -> None:
        if not isinstance(app_id, str) or not self._known(app_id):
            raise ValueError(f"unknown app id {app_id!r}")
        explorer = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "explorer.exe")
        spawn([explorer, f"shell:AppsFolder\\{app_id}"])
        log.info("launched %s", app_id)

    # media / audio ------------------------------------------------------------------------------
    def _media_name(self, aumid: str) -> str | None:
        """Player name from the already-cached app list (never blocks on PowerShell)."""
        return media_app_name(aumid, self._apps.peek() or [])

    def media(self) -> MediaBackend | None:
        with self._backend_lock:
            if not self._media_tried:
                self._media_tried = True
                from ._win_media import WindowsMedia

                self._media = WindowsMedia.create(name_for=self._media_name)
            return self._media

    def audio(self) -> AudioBackend | None:
        with self._backend_lock:
            if not self._audio_tried:
                self._audio_tried = True
                from ._win_audio import WindowsAudio

                self._audio = WindowsAudio.create()
            return self._audio

    # autostart ------------------------------------------------------------------------------------
    def set_autostart(self, enabled: bool, argv: list[str]) -> None:
        if enabled:
            self._reg.set_str(RUN_KEY, RUN_VALUE, run_command(argv))
            # Re-enabling from our settings page also clears a Task-Manager "disabled" mark.
            self._reg.delete(APPROVED_KEY, RUN_VALUE)
        else:
            self._reg.delete(RUN_KEY, RUN_VALUE)
            self._reg.delete(APPROVED_KEY, RUN_VALUE)

    def autostart_enabled(self) -> bool:
        if not self._reg.get(RUN_KEY, RUN_VALUE):
            return False
        return not approved_disabled(self._reg.get(APPROVED_KEY, RUN_VALUE))
