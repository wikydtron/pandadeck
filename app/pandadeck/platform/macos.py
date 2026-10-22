"""macOS backend (see docs/app.md).

* Apps: .app bundles in /Applications, /System/Applications, ~/Applications
  (and one level of sub-folders such as Utilities). id = bundle path; name =
  the bundle's file name (what Finder/Launchpad show), Info.plist names as a
  fallback. Score from Spotlight's kMDItemUseCount / kMDItemLastUsedDate
  (one batched `mdls` call).
* Icons: CFBundleIconFile .icns via Pillow, else `sips` -> PNG, else a
  Quick Look thumbnail of the bundle (`qlmanage -t`) for asset-catalog icons.
* Launch: `open -a <bundle>`, only for discovered bundles.
* Media: Spotify / Apple Music via osascript (_mac_media).
* Audio: `get volume settings`; mic "mute" = input volume 0 (restored on unmute).
* Autostart: ~/Library/LaunchAgents/com.pandadeck.app.plist (RunAtLoad).
  We only write/remove the file: `launchctl bootstrap` would start a second
  copy right now and `bootout` would kill the running one if launchd started it.
"""
from __future__ import annotations

import datetime as dt
import logging
import plistlib
import re
import xml.parsers.expat
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable

from ._util import TTLCache, icon_from_file, normalize_path, run, spawn, validate_argv, write_atomic
from .base import AppInfo, AudioBackend, MediaBackend

log = logging.getLogger("pandadeck.platform.mac")

APP_ROOTS = ("/Applications", "/System/Applications", "/System/Applications/Utilities",
             "/Applications/Utilities")
APPS_TTL_S = 300.0
MDLS_TIMEOUT_S = 15
RECENCY_DAYS = 90.0
AUDIO_TTL_S = 0.8
DEFAULT_MIC_LEVEL = 70
LAUNCH_AGENT_LABEL = "com.pandadeck.app"
# plistlib raises expat's error (not a ValueError) for malformed XML plists.
PLIST_ERRORS = (plistlib.InvalidFileException, xml.parsers.expat.ExpatError)
VOLUME_SCRIPT = ('set s to get volume settings\n'
                 'return ((output volume of s) as text) & "," & ((input volume of s) as text) & "," & '
                 '((output muted of s) as text)')


# ---- bundles (pure-ish: only read files) -----------------------------------------------------

def read_info_plist(bundle: Path) -> dict:
    try:
        with open(bundle / "Contents" / "Info.plist", "rb") as f:
            data = plistlib.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, *PLIST_ERRORS) as e:
        log.debug("no Info.plist in %s: %s", bundle, e)
        return {}


def bundle_name(bundle: Path, info: dict) -> str:
    """Finder's name for the app: the bundle's file name, else the plist names."""
    stem = bundle.name[:-4] if bundle.name.endswith(".app") else bundle.name
    return stem.strip() or str(info.get("CFBundleDisplayName") or info.get("CFBundleName") or "App")


def is_launchable(info: dict) -> bool:
    if info.get("LSBackgroundOnly") in (True, 1, "1", "YES", "true"):
        return False
    kind = info.get("CFBundlePackageType")
    return kind in (None, "", "APPL")


def find_bundles(roots: list[Path]) -> list[Path]:
    """.app bundles directly in each root, or one folder down (e.g. /Applications/Adobe X/)."""
    out: list[Path] = []
    seen: set[str] = set()

    def add(p: Path) -> None:
        try:
            key = str(p.resolve())
        except OSError:
            key = str(p)
        if key not in seen:
            seen.add(key)
            out.append(p)

    for root in roots:
        try:
            children = sorted(root.iterdir())
        except OSError:
            continue
        for child in children:
            if child.name.startswith("."):
                continue
            if child.suffix == ".app" and child.is_dir():
                add(child)
            elif child.is_dir() and child.suffix == "":
                try:
                    for sub in sorted(child.iterdir()):
                        if sub.suffix == ".app" and sub.is_dir() and not sub.name.startswith("."):
                            add(sub)
                except OSError:
                    continue
    return out


def icns_path(bundle: Path, info: dict) -> Path | None:
    res = bundle / "Contents" / "Resources"
    name = str(info.get("CFBundleIconFile") or "").strip()
    if name:
        cand = res / (name if name.lower().endswith(".icns") else name + ".icns")
        if cand.is_file():
            return cand
    for guess in ("AppIcon.icns", f"{bundle.stem}.icns"):
        if (res / guess).is_file():
            return res / guess
    try:
        icns = sorted(res.glob("*.icns"))
    except OSError:
        return None
    return icns[0] if len(icns) == 1 else None


# ---- Spotlight usage ------------------------------------------------------------------------

def parse_mdls(text: str, n: int) -> list[tuple[int | None, dt.datetime | None]]:
    """Pure: `mdls -name kMDItemUseCount -name kMDItemLastUsedDate f1 f2 ...` output ->
    per-file (use count, last used). The i-th value of each attribute is file i."""
    counts: list[int | None] = []
    dates: list[dt.datetime | None] = []
    for line in (text or "").splitlines():
        m = re.match(r"\s*(kMDItem\w+)\s*=\s*(.*?)\s*$", line)
        if not m:
            continue
        attr, value = m.groups()
        if attr == "kMDItemUseCount":
            counts.append(int(value) if value.isdigit() else None)
        elif attr == "kMDItemLastUsedDate":
            try:
                dates.append(dt.datetime.strptime(value, "%Y-%m-%d %H:%M:%S %z"))
            except ValueError:
                dates.append(None)
    counts += [None] * (n - len(counts))
    dates += [None] * (n - len(dates))
    return list(zip(counts[:n], dates[:n]))


def usage_score(count: int | None, last_used: dt.datetime | None, now: dt.datetime) -> float:
    """Launch count plus a 0..1 recency bonus (fading over RECENCY_DAYS)."""
    score = float(count or 0)
    if last_used is not None:
        age_days = max(0.0, (now - last_used).total_seconds() / 86400)
        score += max(0.0, 1.0 - age_days / RECENCY_DAYS)
    return round(score, 3)


# ---- audio ---------------------------------------------------------------------------------------

def parse_volume_settings(text: str | None) -> tuple[int, int | None, bool | None]:
    """'50,75,false' -> (output %, input %, output muted); unknown parts -> -1 / None."""
    parts = [p.strip() for p in (text or "").strip().split(",")]
    parts += [""] * (3 - len(parts))

    def num(s: str) -> int | None:
        return int(s) if re.fullmatch(r"\d+", s) else None

    out = num(parts[0])
    muted = {"true": True, "false": False}.get(parts[2].lower())
    return (out if out is not None else -1), num(parts[1]), muted


class MacAudio:
    def __init__(self):
        self._lock = threading.Lock()
        self._at = 0.0
        self._state: tuple[int, int | None, bool | None] = (-1, None, None)
        self._restore = DEFAULT_MIC_LEVEL

    def _read(self) -> tuple[int, int | None, bool | None]:
        with self._lock:
            if time.monotonic() - self._at >= AUDIO_TTL_S:
                self._state = parse_volume_settings(run(["osascript", "-e", VOLUME_SCRIPT], timeout=3))
                self._at = time.monotonic()
            return self._state

    def volume(self) -> int:
        return self._read()[0]

    def mic_muted(self) -> bool | None:
        level = self._read()[1]
        return None if level is None else level == 0

    def toggle_mic(self) -> None:
        level = self._read()[1]
        if level is None:
            return
        if level > 0:
            self._restore = level
            target = 0
        else:
            target = self._restore or DEFAULT_MIC_LEVEL
        run(["osascript", "-e", f"set volume input volume {int(target)}"], timeout=3)
        with self._lock:
            self._at = 0.0


# ---- autostart -------------------------------------------------------------------------------------

def launch_agent_plist(argv: list[str]) -> bytes:
    argv = validate_argv(argv)
    return plistlib.dumps({
        "Label": LAUNCH_AGENT_LABEL,
        "ProgramArguments": list(argv),
        "RunAtLoad": True,
        "KeepAlive": False,
        "ProcessType": "Interactive",
        "LimitLoadToSessionType": "Aqua",
    })


# ---- the platform ------------------------------------------------------------------------------------

class MacPlatform:
    name = "macos"

    def __init__(self, home: Path | None = None, roots: list[Path] | None = None,
                 which: Callable[[str], str | None] = shutil.which):
        self._home = Path(home) if home is not None else Path.home()
        self._roots = roots if roots is not None else [Path(r) for r in APP_ROOTS] + [self._home / "Applications"]
        self._which = which
        self._apps: TTLCache[dict[str, tuple[AppInfo, dict]]] = TTLCache(APPS_TTL_S, {})
        self._lock = threading.Lock()
        self._media: MediaBackend | None = None
        self._media_tried = False
        self._audio: AudioBackend | None = None

    # apps -------------------------------------------------------------------------------------------
    def _scan(self) -> dict[str, tuple[AppInfo, dict]]:
        found: list[tuple[Path, dict]] = []
        for bundle in find_bundles(self._roots):
            info = read_info_plist(bundle)
            if is_launchable(info):
                found.append((bundle, info))
        usage = self._spotlight([b for b, _ in found])
        now = dt.datetime.now(dt.timezone.utc)
        out: dict[str, tuple[AppInfo, dict]] = {}
        for (bundle, info), (count, last) in zip(found, usage):
            app = AppInfo(id=str(bundle), name=bundle_name(bundle, info), score=usage_score(count, last, now))
            out[app.id] = (app, info)
        return out

    def _spotlight(self, bundles: list[Path]) -> list[tuple[int | None, dt.datetime | None]]:
        if not bundles or not self._which("mdls"):
            return [(None, None)] * len(bundles)
        text = run(["mdls", "-name", "kMDItemUseCount", "-name", "kMDItemLastUsedDate",
                    *[str(b) for b in bundles]], timeout=MDLS_TIMEOUT_S)
        return parse_mdls(text or "", len(bundles))

    def list_apps(self) -> list[AppInfo]:
        apps = [a for a, _ in self._apps.get(self._scan).values()]
        return sorted(apps, key=lambda a: (-a.score, a.name.lower()))

    def _entry(self, app_id: str) -> tuple[AppInfo, dict]:
        entry = self._apps.get(self._scan).get(app_id)
        if entry is None:
            entry = self._apps.get(self._scan, force=True).get(app_id)
        if entry is None:
            raise ValueError(f"unknown app id {app_id!r}")
        return entry

    def app_icon(self, app_id: str):
        try:
            _, info = self._entry(app_id)
        except ValueError:
            return None
        return self._bundle_icon(Path(app_id), info)

    def _bundle_icon(self, bundle: Path, info: dict):
        icns = icns_path(bundle, info)
        if icns is not None:
            img = icon_from_file(icns)
            if img is not None:
                return img
            img = self._sips_png(icns)
            if img is not None:
                return img
        return self._quicklook(bundle)

    def file_icon(self, path: str):
        """An .app bundle's own icon; anything else (file or folder): its Quick
        Look thumbnail, which is the Finder icon when there's no preview."""
        p = normalize_path(path)
        if p is None:
            return None
        target = Path(p)
        if target.suffix == ".app" and target.is_dir():
            return self._bundle_icon(target, read_info_plist(target))
        return self._quicklook(target)

    def _sips_png(self, icns: Path):
        if not self._which("sips"):
            return None
        with tempfile.TemporaryDirectory(prefix="pandadeck-icon-") as tmp:
            out = Path(tmp) / "icon.png"
            if run(["sips", "-s", "format", "png", str(icns), "--out", str(out)], timeout=10) is None:
                return None
            return icon_from_file(out)

    def _quicklook(self, bundle: Path):
        if not self._which("qlmanage"):
            return None
        with tempfile.TemporaryDirectory(prefix="pandadeck-icon-") as tmp:
            if run(["qlmanage", "-t", "-s", "256", "-o", tmp, str(bundle)], timeout=15) is None:
                return None
            pngs = sorted(Path(tmp).glob("*.png"))
            return icon_from_file(pngs[0]) if pngs else None

    def launch_app(self, app_id: str) -> None:
        self._entry(app_id)  # raises ValueError for anything we didn't discover
        spawn(["open", "-a", app_id])
        log.info("launched %s", app_id)

    # media / audio -----------------------------------------------------------------------------------
    def media(self) -> MediaBackend | None:
        with self._lock:
            if not self._media_tried:
                self._media_tried = True
                from ._mac_media import MacMedia

                self._media = MacMedia.create()
            return self._media

    def audio(self) -> AudioBackend | None:
        with self._lock:
            if self._audio is None and self._which("osascript"):
                self._audio = MacAudio()
            return self._audio

    # autostart -----------------------------------------------------------------------------------------
    def _agent_path(self) -> Path:
        return self._home / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"

    def set_autostart(self, enabled: bool, argv: list[str]) -> None:
        path = self._agent_path()
        if enabled:
            write_atomic(path, launch_agent_plist(argv))
        else:
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    def autostart_enabled(self) -> bool:
        try:
            with open(self._agent_path(), "rb") as f:
                data = plistlib.load(f)
        except (OSError, ValueError, *PLIST_ERRORS):
            return False
        return (isinstance(data, dict) and data.get("Label") == LAUNCH_AGENT_LABEL
                and bool(data.get("RunAtLoad")) and not data.get("Disabled", False)
                and bool(data.get("ProgramArguments")))
