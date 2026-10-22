"""freedesktop icon-theme lookup (Icon= of a .desktop entry -> an image file).

A small subset of the Icon Theme spec: the current theme, its Inherits chain
and hicolor, each theme's index.theme directories (Size/Scale/Type), base dirs
~/.icons + $XDG_DATA_DIRS/icons, then the pixmaps dirs. Within the first theme
that has the icon we pick: a raster >= 96 px closest to 256, else a scalable
SVG (if we can rasterise it), else the biggest raster. Pure Python + Pillow;
SVGs go through cairosvg or rsvg-convert when either is installed.
"""
from __future__ import annotations

import io
import logging
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from ._util import ICON_PX, icon_from_file, run

log = logging.getLogger("pandadeck.platform.linux")

RASTER_EXTS = (".png", ".xpm")
SVG_EXTS = (".svg", ".svgz")
ICON_EXTS = RASTER_EXTS + SVG_EXTS
GOOD_RASTER_PX = 96
TARGET_PX = 256
MAX_INHERIT_DEPTH = 8
KNOWN_EXT_SUFFIXES = (".png", ".svg", ".svgz", ".xpm")


@dataclass(frozen=True)
class ThemeDir:
    path: str      # relative to the theme root, e.g. "256x256/apps"
    size: int      # nominal pixel size (Size * Scale)
    scalable: bool


@dataclass(frozen=True)
class Candidate:
    path: Path
    px: int        # 0 = scalable
    svg: bool


def parse_groups(text: str) -> dict[str, dict[str, str]]:
    """Every [group] of an ini-style file (first occurrence of a key wins)."""
    groups: dict[str, dict[str, str]] = {}
    cur: dict[str, str] | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            cur = groups.setdefault(line[1:-1], {})
            continue
        if cur is not None and "=" in line:
            key, value = line.split("=", 1)
            cur.setdefault(key.strip(), value.strip())
    return groups


def parse_index_theme(text: str) -> tuple[list[str], list[ThemeDir]]:
    """index.theme -> (Inherits, directories)."""
    groups = parse_groups(text)
    head = groups.get("Icon Theme", {})
    inherits = [t.strip() for t in head.get("Inherits", "").split(",") if t.strip()]
    names = [d.strip() for d in (head.get("Directories", "") + "," + head.get("ScaledDirectories", "")).split(",")]
    dirs: list[ThemeDir] = []
    seen: set[str] = set()
    for name in names:
        if not name or name in seen:
            continue
        seen.add(name)
        sec = groups.get(name, {})
        try:
            size = int(sec.get("Size", "0")) * max(1, int(sec.get("Scale", "1")))
        except ValueError:
            continue
        scalable = sec.get("Type", "Threshold") == "Scalable"
        if scalable:
            try:
                size = max(size, int(sec.get("MaxSize", "0")))
            except ValueError:
                pass
        dirs.append(ThemeDir(name, size, scalable))
    return inherits, dirs


def pick_candidate(cands: Iterable[Candidate], can_svg: bool) -> Candidate | None:
    """Pure: best file for a ~256 px tile (see module docstring)."""
    cands = list(cands)
    rasters = [c for c in cands if not c.svg]
    svgs = [c for c in cands if c.svg]
    good = [c for c in rasters if c.px >= GOOD_RASTER_PX]
    if good:
        return min(good, key=lambda c: (abs(c.px - TARGET_PX), -c.px))
    if svgs and can_svg:
        return svgs[0]
    if rasters:
        return max(rasters, key=lambda c: c.px)
    return svgs[0] if svgs else None


def strip_icon_ext(name: str) -> str:
    low = name.lower()
    for ext in KNOWN_EXT_SUFFIXES:
        if low.endswith(ext):
            return name[: -len(ext)]
    return name


class IconFinder:
    """Resolves icon names against themes found under the given data dirs."""

    def __init__(self, data_dirs: Iterable[Path], home: Path, themes: Iterable[str] = (),
                 which: Callable[[str], str | None] = shutil.which):
        data_dirs = list(data_dirs)
        self._bases = [home / ".icons"] + [Path(d) / "icons" for d in data_dirs]
        self._pixmaps = [Path(d) / "pixmaps" for d in data_dirs] + [Path("/usr/share/pixmaps")]
        self._which = which
        self._index: dict[str, tuple[list[str], list[ThemeDir], list[Path]]] = {}
        self._chain = self._theme_chain(list(themes))
        self._found: dict[str, Path | None] = {}
        self._can_svg: bool | None = None

    # themes --------------------------------------------------------------------------------------
    def _theme(self, name: str) -> tuple[list[str], list[ThemeDir], list[Path]]:
        """(inherits, dirs, roots) of a theme; roots = every base dir that has it."""
        if name not in self._index:
            roots = [b / name for b in self._bases if (b / name).is_dir()]
            inherits: list[str] = []
            dirs: list[ThemeDir] = []
            for root in roots:
                index = root / "index.theme"
                if index.is_file():
                    try:
                        inherits, dirs = parse_index_theme(index.read_text(encoding="utf-8", errors="replace"))
                    except OSError:
                        continue
                    break
            self._index[name] = (inherits, dirs, roots)
        return self._index[name]

    def _theme_chain(self, themes: list[str]) -> list[str]:
        chain: list[str] = []
        todo = [(t, 0) for t in themes if t]
        while todo:
            name, depth = todo.pop(0)
            if name in chain or depth > MAX_INHERIT_DEPTH:
                continue
            chain.append(name)
            todo += [(t, depth + 1) for t in self._theme(name)[0]]
        if "hicolor" not in chain:
            chain.append("hicolor")
        return chain

    @property
    def chain(self) -> list[str]:
        return list(self._chain)

    # lookup -------------------------------------------------------------------------------------------
    def _candidates(self, theme: str, name: str) -> list[Candidate]:
        _, dirs, roots = self._theme(theme)
        out: list[Candidate] = []
        for root in roots:
            for d in dirs:
                for ext in ICON_EXTS:
                    p = root / d.path / f"{name}{ext}"
                    if p.is_file():
                        svg = ext in SVG_EXTS
                        out.append(Candidate(p, 0 if svg else d.size, svg))
        return out

    def can_svg(self) -> bool:
        if self._can_svg is None:
            self._can_svg = svg_rasterizer_available(self._which)
        return self._can_svg

    def find(self, icon: str) -> Path | None:
        """Icon= value -> a file path (absolute values are used as-is)."""
        if not icon:
            return None
        if icon.startswith("/"):
            p = Path(icon)
            return p if p.is_file() else None
        if icon in self._found:
            return self._found[icon]
        name = strip_icon_ext(icon)
        result = None
        can_svg = self.can_svg()
        for theme in self._chain:
            best = pick_candidate(self._candidates(theme, name), can_svg)
            if best is not None:
                result = best.path
                break
        if result is None:
            for d in self._pixmaps:
                for ext in ICON_EXTS:
                    p = d / f"{name}{ext}"
                    if p.is_file():
                        result = p
                        break
                if result is not None:
                    break
        self._found[icon] = result
        return result

    def load(self, icon: str, px: int = ICON_PX):
        path = self.find(icon)
        if path is None:
            return None
        if path.suffix.lower() in SVG_EXTS:
            return rasterize_svg(path, px, self._which)
        return icon_from_file(path, px)


def svg_rasterizer_available(which: Callable[[str], str | None] = shutil.which) -> bool:
    try:
        import cairosvg  # noqa: F401
        return True
    except Exception:  # noqa: BLE001 - missing, or its cairo libs are missing
        return bool(which("rsvg-convert"))


def rasterize_svg(path: Path, px: int = ICON_PX, which: Callable[[str], str | None] = shutil.which):
    """SVG -> RGBA via cairosvg or rsvg-convert; None if neither is available."""
    from PIL import Image

    data = None
    try:
        import cairosvg

        data = cairosvg.svg2png(url=str(path), output_width=px, output_height=px)
    except Exception as e:  # noqa: BLE001 - not installed, or this SVG upset it
        log.debug("cairosvg unavailable/failed for %s: %s", path, e)
    if not data and which("rsvg-convert"):
        data = run_bytes(["rsvg-convert", "-w", str(px), "-h", str(px), "-f", "png", str(path)])
    if not data:
        return None
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
        return img.convert("RGBA")
    except Exception as e:  # noqa: BLE001
        log.debug("rasterised SVG unreadable: %s", e)
        return None


def run_bytes(args: list[str], timeout: float = 10.0) -> bytes | None:
    import subprocess

    try:
        proc = subprocess.run(args, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def current_icon_themes(home: Path, env: dict | None = None,
                        which: Callable[[str], str | None] = shutil.which) -> list[str]:
    """Best guess at the user's icon theme(s): KDE's kdeglobals, gsettings (GNOME & co)."""
    env = env or {}
    kde_first = "kde" in (env.get("XDG_CURRENT_DESKTOP") or "").lower()
    found: list[str] = []
    config_home = Path(env.get("XDG_CONFIG_HOME") or home / ".config")
    try:
        text = (config_home / "kdeglobals").read_text(encoding="utf-8", errors="replace")
        kde = parse_groups(text).get("Icons", {}).get("Theme", "")
    except OSError:
        kde = ""
    gnome = ""
    if which("gsettings"):
        out = run(["gsettings", "get", "org.gnome.desktop.interface", "icon-theme"], timeout=3)
        gnome = (out or "").strip().strip("'\"")
    for name in ([kde, gnome] if kde_first else [gnome, kde]):
        if name and name not in found:
            found.append(name)
    return found


# ---- file / folder icons (Icon Naming Spec names) -------------------------------------------

USER_DIR_ICONS = {"DESKTOP": "user-desktop", "DOWNLOAD": "folder-download", "DOCUMENTS": "folder-documents",
                  "MUSIC": "folder-music", "PICTURES": "folder-pictures", "VIDEOS": "folder-videos",
                  "TEMPLATES": "folder-templates", "PUBLICSHARE": "folder-publicshare"}
FOLDER_FALLBACKS = ["folder", "inode-directory"]
FILE_FALLBACKS = ["application-x-generic", "text-x-generic", "unknown"]


def parse_user_dirs(text: str, home: str) -> dict[str, str]:
    """~/.config/user-dirs.dirs -> {absolute dir: icon name} for the special folders."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        m = re.match(r'\s*XDG_([A-Z]+)_DIR\s*=\s*"(.*)"\s*$', line)
        if not m or m.group(1) not in USER_DIR_ICONS:
            continue
        path = m.group(2).replace("$HOME", home)
        if path.startswith("/"):
            out[os.path.normpath(path)] = USER_DIR_ICONS[m.group(1)]
    return out


def parse_generic_icons(text: str) -> dict[str, str]:
    """shared-mime-info's mime/generic-icons ("type/sub:icon-name" lines)."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        mime, sep, icon = line.strip().partition(":")
        if sep and mime and icon:
            out.setdefault(mime, icon)
    return out


def folder_icon_names(path: str, home: str, user_dirs: dict[str, str]) -> list[str]:
    path = os.path.normpath(path)
    if path == os.path.normpath(home):
        return ["user-home", *FOLDER_FALLBACKS]
    if path in user_dirs:
        return [user_dirs[path], *FOLDER_FALLBACKS]
    return list(FOLDER_FALLBACKS)


def file_icon_names(mime: str | None, generic: dict[str, str], executable: bool = False) -> list[str]:
    """Icon names to try for a file of this MIME type, most specific first:
    text/x-python -> text-x-python, its generic-icons entry, text-x-generic."""
    names: list[str] = []
    if mime and "/" in mime:
        names.append(mime.replace("/", "-"))
        if mime in generic:
            names.append(generic[mime])
        names.append(f"{mime.split('/')[0]}-x-generic")
    if executable:
        names.append("application-x-executable")
    out: list[str] = []
    for n in names + FILE_FALLBACKS:
        if n not in out:
            out.append(n)
    return out
