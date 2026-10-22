#!/usr/bin/env python3
"""Reproducible LVGL font pipeline for PandaDeck (openly licensed fonts).

  python tools/make_fonts.py [--set open] [--out DIR] [--preview sheet.png]

Downloads pinned, openly licensed font files into .fonts-cache/ (every file is
sha256-verified), then generates the LVGL C fonts (via `npx lv_font_conv`) and
icon_map.h (Material Icons Round codepoints) into firmware/PandaDeck/, and the
upstream license texts plus LICENSES.md into firmware/fonts/. With --out DIR,
everything is written into DIR.

The icon name table lives in ICONS below.
Needs Python 3.9+ and Node.js (npx). --preview additionally needs Pillow.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).absolute().parent.parent  # keep subst drives (short paths)
CACHE_DIR = ROOT / ".fonts-cache"
FONT_OUT_DIR = ROOT / "firmware" / "PandaDeck"   # font_*.c + icon_map.h
LICENSE_OUT_DIR = ROOT / "firmware" / "fonts"    # LICENSES.md + LICENSE-* texts
SETS = ["open"]
LV_FONT_CONV = "lv_font_conv@1.5.3"
LV_FONT_CONV_ARGS = ("--format", "lvgl", "--bpp", "4", "--no-compress")
DOWNLOAD_TIMEOUT_S = 120

# --------------------------------------------------------------------------
# Icons: name -> Material Icons Round glyph name.
# Order and names are the firmware's ICONS[] table (ui_theme.cpp -> icon_map.h).
# --------------------------------------------------------------------------
ICONS = (
    ("prev", "skip_previous"),            ("play", "play_arrow"),
    ("pause", "pause"),                   ("next", "skip_next"),
    ("mute", "volume_off"),               ("vol_down", "volume_down"),
    ("vol_up", "volume_up"),              ("mic", "mic"),
    ("mic_off", "mic_off"),               ("audio", "audiotrack"),
    ("camera", "photo_camera"),           ("home", "home"),
    ("list", "list"),                     ("lock", "lock"),
    ("left", "align_horizontal_left"),    ("right", "align_horizontal_right"),
    ("up", "open_in_full"),               ("down", "close_fullscreen"),
    ("taskview", "filter_none"),          ("folder", "folder"),
    ("paste", "content_paste"),           ("smile", "sentiment_satisfied"),
    ("settings", "settings"),             ("go", "north_east"),
    ("loop", "swap_horiz"),               ("close", "close"),
    ("terminal", "terminal"),             ("edit", "data_object"),
    ("web", "language"),                  ("copy", "content_copy"),
    ("undo", "undo"),                     ("redo", "redo"),
    ("save", "save"),                     ("search", "search"),
    ("enter", "keyboard_return"),         ("bolt", "bolt"),
    ("refresh", "refresh"),               ("power", "power_settings_new"),
    ("warning", "warning"),               ("check", "check"),
    ("robot", "smart_toy"),               ("server", "dns"),
    ("brightness", "light_mode"),         ("wifi", "wifi"),
    ("keyboard", "keyboard"),             ("apps", "apps"),
    ("cut", "content_cut"),               ("chip", "memory"),
    ("thermo", "device_thermostat"),      ("speed", "speed"),
    ("cloud", "cloud"),                   ("game", "sports_esports"),
    ("image", "screenshot_monitor"),      ("run", "open_in_new"),
    ("crop", "crop"),                     ("spark", "auto_awesome"),
    ("play_s", "play_arrow"),             ("pause_s", "pause"),
    ("prev_s", "skip_previous"),          ("next_s", "skip_next"),
    ("shuffle", "shuffle"),               ("repeat", "repeat"),
    ("repeat_one", "repeat_one"),         ("music", "music_note"),
    ("unlock", "lock_open"),              ("car", "directions_car"),
    ("sync", "sync"),                     ("pin", "place"),
    ("heart", "favorite_border"),         ("heart_s", "favorite"),
    ("rew", "fast_rewind"),               ("ff", "fast_forward"),
)
ICON_SECTIONS = {  # comment emitted in icon_map.h before the named entry
    "play_s": "media: solid transport set for the MUSIC page (also in font_icons_52)",
    "unlock": "lock / unlock (both also in font_icons_52), then misc",
    "heart": "MUSIC page like button: outline / filled",
}
ICONS_52 = ("play_s", "pause_s", "prev_s", "next_s", "lock", "unlock")  # font_icons_52 subset
# (symbol size, rendered px, names or None = all). Material's solid transport glyphs sit
# small in their em box, so font_icons_52 is rendered larger.
ICON_SIZES = ((20, 20, None), (36, 36, None), (52, 72, ICONS_52))

# --------------------------------------------------------------------------
# Pinned downloads. Raw files are pinned to a commit; release archives are
# pinned by tag and sha256. `sha256` is always the hash of the cached file.
# --------------------------------------------------------------------------
GH_RAW = "https://raw.githubusercontent.com"
OUTFIT_SHA = "902773808eb372f70fb34e8946dd1ffe604efc79"
SOURCE_SERIF_SHA = "80d3f8894c09c937bebfa9011247d2e1c79fd6f4"
GOOGLE_FONTS_SHA = "47d54ca1ebb14fd864695f1d39dc734f39017c33"
MATERIAL_SHA = "49d4db35df873165d6bd6ba09b063c7dafbac2f4"
CASCADIA_SHA = "911dc421f333e3b72b97381d16fee5b71eb48f04"  # tag v2102.25
CASCADIA_ZIP = ("https://github.com/microsoft/cascadia-code/releases/download/"
                "v2102.25/CascadiaCode-2102.25.zip")
DEJAVU_ZIP = ("https://github.com/dejavu-fonts/dejavu-fonts/releases/download/"
              "version_2_37/dejavu-fonts-ttf-2.37.zip")


@dataclass(frozen=True)
class Asset:
    file: str                 # name inside .fonts-cache/
    url: str
    sha256: str               # sha256 of `file`
    member: str = ""          # path inside the zip at `url` (empty: url is the file)
    archive_sha256: str = ""  # sha256 of the zip when `member` is set


ASSETS = {
    "outfit": Asset(
        "Outfit-Regular.ttf",
        f"{GH_RAW}/Outfitio/Outfit-Fonts/{OUTFIT_SHA}/fonts/ttf/Outfit-Regular.ttf",
        "3b64ac4f6ab6a8eebddd4b0bc03c811c43602e11e176382ab0ee6be615ab861b"),
    "outfit_license": Asset(
        "Outfit-OFL.txt", f"{GH_RAW}/Outfitio/Outfit-Fonts/{OUTFIT_SHA}/OFL.txt",
        "c676351bf8576b9aba743cd5eaa8c0e7ee0d51f805d720447b4df4ddb6a2e416"),
    "source_serif": Asset(
        "SourceSerif4-Regular.ttf",
        f"{GH_RAW}/adobe-fonts/source-serif/{SOURCE_SERIF_SHA}/TTF/SourceSerif4-Regular.ttf",
        "e5a4ee6a3d87bb9024796be390c6771e2a0eb1883dae25effaf57ca01668e24b"),
    "source_serif_license": Asset(
        "SourceSerif4-LICENSE.md",
        f"{GH_RAW}/adobe-fonts/source-serif/{SOURCE_SERIF_SHA}/LICENSE.md",
        "c21d7293d87b6d7ab1d0229a2f55b77f33a7613a6a4e66f6693d68d7d8d09464"),
    "cascadia_mono": Asset(
        "CascadiaMono-Regular.ttf", CASCADIA_ZIP,
        "024ed8cbdfd77351f305853f6eaf46da39485225b27dc97a7775b3d1f967aac9",
        member="ttf/static/CascadiaMono-Regular.ttf",
        archive_sha256="a64e8834f06ec9acca4a5307cb6cf35922021d4fd0ca19096f8bf6a3e2515ad3"),
    "cascadia_license": Asset(
        "CascadiaCode-LICENSE.txt", f"{GH_RAW}/microsoft/cascadia-code/{CASCADIA_SHA}/LICENSE",
        "51882cd3cdba4e16f220f44ddb08a635c38c44ea6e0975db2574f4be6f958238"),
    "noto_symbols2": Asset(
        "NotoSansSymbols2-Regular.ttf",
        f"{GH_RAW}/google/fonts/{GOOGLE_FONTS_SHA}/ofl/notosanssymbols2/NotoSansSymbols2-Regular.ttf",
        "7d5fb73b7ca67a6798101741f5d280a3d016a56a197afcd4199dbb57b4b82a21"),
    "noto_symbols2_license": Asset(
        "NotoSansSymbols2-OFL.txt",
        f"{GH_RAW}/google/fonts/{GOOGLE_FONTS_SHA}/ofl/notosanssymbols2/OFL.txt",
        "b118dd41337806a5d4797052c77caf3bd096aed783e5eb21b4d11154351e1ac0"),
    "dejavu_mono": Asset(
        "DejaVuSansMono.ttf", DEJAVU_ZIP,
        "b4a6c3e4faab8773f4ff761d56451646409f29abedd68f05d38c2df667d3c582",
        member="dejavu-fonts-ttf-2.37/ttf/DejaVuSansMono.ttf",
        archive_sha256="7576310b219e04159d35ff61dd4a4ec4cdba4f35c00e002a136f00e96a908b0a"),
    "dejavu_license": Asset(
        "DejaVu-LICENSE.txt", DEJAVU_ZIP,
        "7a083b136e64d064794c3419751e5c7dd10d2f64c108fe5ba161eae5e5958a93",
        member="dejavu-fonts-ttf-2.37/LICENSE",
        archive_sha256="7576310b219e04159d35ff61dd4a4ec4cdba4f35c00e002a136f00e96a908b0a"),
    "material_round": Asset(
        "MaterialIconsRound-Regular.otf",
        f"{GH_RAW}/google/material-design-icons/{MATERIAL_SHA}/font/MaterialIconsRound-Regular.otf",
        "bad85e5454b6288104ce03806c37323bcd8f145e3094e727860173ac8c91062e"),
    "material_round_codepoints": Asset(
        "MaterialIconsRound-Regular.codepoints",
        f"{GH_RAW}/google/material-design-icons/{MATERIAL_SHA}/font/MaterialIconsRound-Regular.codepoints",
        "534fc87caafe6c7529a390fbe5b669c158d560caa5e9358c8e49054b817603d8"),
    "material_license": Asset(
        "MaterialIcons-LICENSE.txt", f"{GH_RAW}/google/material-design-icons/{MATERIAL_SHA}/LICENSE",
        "58d1e17ffe5109a7ae296caafcadfdbe6a7d176f0bc4ab01e12a689b0499d8bd"),
}


@dataclass(frozen=True)
class Family:
    name: str
    font: str           # ASSETS key of the font file
    license: str        # ASSETS key of the license text
    license_id: str
    license_out: str    # file name written next to the generated fonts
    home: str
    used_for: str


FAMILIES = (
    Family("Outfit Regular", "outfit", "outfit_license", "OFL-1.1", "LICENSE-Outfit.txt",
           "https://github.com/Outfitio/Outfit-Fonts", "font_head_28, font_head_48 (headings)"),
    Family("Source Serif 4 Regular", "source_serif", "source_serif_license", "OFL-1.1",
           "LICENSE-SourceSerif4.md", "https://github.com/adobe-fonts/source-serif",
           "font_serif_56 (big numerals)"),
    Family("Cascadia Mono Regular 2102.25", "cascadia_mono", "cascadia_license", "OFL-1.1",
           "LICENSE-CascadiaCode.txt", "https://github.com/microsoft/cascadia-code",
           "font_mono_16, font_mono_20"),
    Family("Noto Sans Symbols 2 Regular", "noto_symbols2", "noto_symbols2_license", "OFL-1.1",
           "LICENSE-NotoSansSymbols2.txt", "https://github.com/notofonts/symbols",
           "spinner glyphs U+2722 U+2733 U+2736 U+273B U+273D in font_mono_16/20"),
    Family("DejaVu Sans Mono 2.37", "dejavu_mono", "dejavu_license",
           "Bitstream Vera + Arev font licenses (DejaVu changes: public domain)",
           "LICENSE-DejaVu.txt", "https://github.com/dejavu-fonts/dejavu-fonts",
           "font_mono_32 (incl. its spinner glyphs)"),
    Family("Material Icons Round", "material_round", "material_license", "Apache-2.0",
           "LICENSE-MaterialIcons.txt", "https://github.com/google/material-design-icons",
           "font_icons_20, font_icons_36, font_icons_52, icon_map.h"),
)

# --------------------------------------------------------------------------
# Text fonts: the ranges / sizes / bpp the firmware expects.
# --------------------------------------------------------------------------
ASCII = "0x20-0x7E"
MONO_RANGE = "0x20-0x7E,0xB0,0xB7,0x2026,0x2191,0x2193,0x2192"
SPINNER = "0x2722,0x2733,0x2736,0x273B,0x273D"
TEXT_FONTS = (  # (symbol, px, ((asset key, -r range), ...))
    ("font_head_28", 28, (("outfit", ASCII),)),
    ("font_head_48", 48, (("outfit", ASCII),)),
    ("font_serif_56", 56, (("source_serif", ASCII),)),
    ("font_mono_16", 16, (("cascadia_mono", MONO_RANGE), ("noto_symbols2", SPINNER))),
    ("font_mono_20", 20, (("cascadia_mono", MONO_RANGE), ("noto_symbols2", SPINNER))),
    ("font_mono_32", 32, (("dejavu_mono", "0x20-0x7E,0xB7,0x2026," + SPINNER),)),
)


class PipelineError(RuntimeError):
    pass


# ------------------------------- downloads --------------------------------
def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def http_get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "PandaDeck-make_fonts"})
    try:
        with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT_S) as resp:
            return resp.read()
    except OSError as exc:
        raise PipelineError(f"download failed: {url}: {exc}") from exc


def write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".part")
    tmp.write_bytes(data)
    tmp.replace(path)


def cached_or_fetched(path: Path, url: str, sha: str) -> bytes:
    """Bytes of `url`, from `path` if it already holds the pinned content."""
    if path.exists():
        data = path.read_bytes()
        if sha256_of(data) == sha:
            return data
    print(f"  downloading {url}")
    data = http_get(url)
    got = sha256_of(data)
    if got != sha:
        raise PipelineError(f"sha256 mismatch for {url}\n  expected {sha}\n  got      {got}")
    write_atomic(path, data)
    return data


def fetch(key: str) -> Path:
    """Return the verified cached file for ASSETS[key], downloading it if needed."""
    asset = ASSETS[key]
    CACHE_DIR.mkdir(exist_ok=True)
    dest = CACHE_DIR / asset.file
    if not asset.member:
        cached_or_fetched(dest, asset.url, asset.sha256)
        return dest
    if dest.exists() and sha256_of(dest.read_bytes()) == asset.sha256:
        return dest
    archive = CACHE_DIR / Path(urlparse(asset.url).path).name
    cached_or_fetched(archive, asset.url, asset.archive_sha256)
    with zipfile.ZipFile(archive) as zf:
        data = zf.read(asset.member)
    if sha256_of(data) != asset.sha256:
        raise PipelineError(f"sha256 mismatch for {asset.member} in {archive.name}")
    write_atomic(dest, data)
    return dest


# ------------------------------- icon maps --------------------------------
def check_icon_table() -> None:
    names = [n for n, _ in ICONS]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise PipelineError(f"duplicate icon names: {dupes}")
    unknown = [n for n in (*ICONS_52, *ICON_SECTIONS) if n not in names]
    if unknown:
        raise PipelineError(f"unknown icon names referenced: {unknown}")


def check_bmp_pua(label: str, cp: int) -> None:
    # ui_theme.cpp encode_utf8() assumes 3-byte UTF-8 private-use codepoints.
    if not 0xE000 <= cp <= 0xF8FF:
        raise PipelineError(f"{label}: U+{cp:04X} is outside the BMP private-use area")


def load_material_codepoints(path: Path) -> dict[str, int]:
    table: dict[str, int] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) == 2:
            table.setdefault(parts[0], int(parts[1], 16))
    return table


def material_entries(codepoints: dict[str, int]) -> list[tuple[str, int, str]]:
    missing = sorted({glyph for _, glyph in ICONS if glyph not in codepoints})
    if missing:
        raise PipelineError(f"Material glyphs not in the codepoints file: {missing}")
    return [(name, codepoints[glyph], glyph) for name, glyph in ICONS]


def render_icon_map(entries: list[tuple[str, int, str]], header: str) -> str:
    lines = [header, "#pragma once", "", "static IconDef ICONS[] = {"]
    for name, cp, note in entries:
        check_bmp_pua(f"icon '{name}'", cp)
        if name in ICON_SECTIONS:
            lines.append(f"    /* {ICON_SECTIONS[name]} */")
        entry = f'    {{"{name}", 0x{cp:04X}, {{}}}},'
        lines.append(f"{entry:<34}/* {note} */" if note else entry)
    lines += ["};", ""]
    return "\n".join(lines)


OPEN_HEADER = """\
/* Generated by tools/make_fonts.py --set open -- edit ICONS there, not here.
 * Icon name -> Material Icons Round codepoint (Apache-2.0, google/material-
 * design-icons); comments name the Material glyph. Glyphs: font_icons_20/36
 * (all), font_icons_52 (subset). Included by ui_theme.cpp after IconDef. */"""


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    print(f"  wrote {path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}")


# ------------------------------- lv_font_conv -----------------------------
def parse_ranges(spec: str) -> set[int]:
    cps: set[int] = set()
    for part in spec.split(","):
        lo, _, hi = part.partition("-")
        cps.update(range(int(lo, 16), int(hi or lo, 16) + 1))
    return cps


def find_npx() -> str:
    npx = shutil.which("npx")
    if not npx:
        raise PipelineError("npx not found: install Node.js (https://nodejs.org)")
    return npx


def scrub_paths(text: str, fonts: list[Path]) -> str:
    """Show only file names in the generated `Opts:` header line."""
    for font in fonts:
        text = text.replace(font.as_posix(), font.name)
    header = text.split("*/", 1)[0]
    leaks = [str(ROOT), ROOT.as_posix(), str(Path.home()), Path.home().as_posix()]
    if any(s in text for s in leaks) or re.search(r"[A-Za-z]:[\\/]|/home/|/Users/", header):
        raise PipelineError("generated font still contains an absolute local path")
    return text


def check_glyphs(symbol: str, text: str, expected: set[int]) -> None:
    present = {int(h, 16) for h in re.findall(r"/\* U\+([0-9A-F]+) ", text)}
    missing = sorted(expected - present)
    if missing:
        raise PipelineError(f"{symbol}: glyphs missing from the source fonts: "
                            + ", ".join(f"U+{cp:04X}" for cp in missing))


def lv_font_conv(npx: str, out_dir: Path, symbol: str, size: int,
                 parts: list[tuple[Path, str]]) -> None:
    args = [npx, "--yes", LV_FONT_CONV]
    for font, rng in parts:
        args += ["--font", font.as_posix(), "-r", rng]
    args += ["--size", str(size), *LV_FONT_CONV_ARGS, "-o", f"{symbol}.c", "--lv-include", "lvgl.h"]
    print(f"  lv_font_conv {symbol} ({size}px)")
    result = subprocess.run(args, cwd=out_dir, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise PipelineError(f"lv_font_conv failed for {symbol}:\n{result.stderr or result.stdout}")
    out = out_dir / f"{symbol}.c"
    text = scrub_paths(out.read_text(encoding="utf-8"), [f for f, _ in parts])
    expected = set().union(*(parse_ranges(rng) for _, rng in parts))
    check_glyphs(symbol, text, expected)
    out.write_text(text, encoding="utf-8", newline="\n")


def icon_ranges(entries: list[tuple[str, int, str]], subset) -> str:
    """lv_font_conv -r list: unique codepoints in table order (or `subset` order)."""
    by_name = {name: cp for name, cp, _ in entries}
    cps: list[int] = []
    for name in (subset if subset is not None else by_name):
        if by_name[name] not in cps:
            cps.append(by_name[name])
    return ",".join(f"0x{cp:04X}" for cp in cps)


# ------------------------------- outputs ----------------------------------
def licenses_md() -> str:
    rows = ["| Font | Used for | License | License text | Source file (pinned) | sha256 |",
            "|---|---|---|---|---|---|"]
    for fam in FAMILIES:
        asset = ASSETS[fam.font]
        src = f"[{asset.member or asset.file}]({asset.url})"
        rows.append(f"| [{fam.name}]({fam.home}) | {fam.used_for} | {fam.license_id} | "
                    f"[{fam.license_out}]({fam.license_out}) | {src} | `{asset.sha256}` |")
    return "\n".join([
        "# Open font set: licenses", "",
        "The LVGL C fonts (`firmware/PandaDeck/font_*.c`) and `icon_map.h`, this file and",
        "the `LICENSE-*` texts are generated by `python tools/make_fonts.py` from the pinned,",
        f"sha256-verified upstream files below (`npx {LV_FONT_CONV}`, 4 bpp, uncompressed).",
        "Each `.c` file is a rasterized subset of the font(s) it was built from and is",
        "distributed under that font's license; the license texts are reproduced verbatim",
        "in the `LICENSE-*` files beside this one. The generated fonts use generic symbol",
        "names (`font_head_28`, ...) and no Reserved Font Name.", "",
        *rows, "",
        "Zip downloads are verified twice (the archive and the extracted file).",
        "The Material glyph used for each firmware icon name is noted in `icon_map.h`.", "",
    ])


def build_open(out_dir: Path, license_dir: Path, preview: Path | None) -> None:
    check_icon_table()
    print("fetching pinned sources")
    paths = {key: fetch(key) for key in ASSETS}
    out_dir.mkdir(parents=True, exist_ok=True)
    license_dir.mkdir(parents=True, exist_ok=True)
    entries = material_entries(load_material_codepoints(paths["material_round_codepoints"]))
    npx = find_npx()
    print(f"generating into {out_dir}")
    for symbol, size, parts in TEXT_FONTS:
        lv_font_conv(npx, out_dir, symbol, size, [(paths[k], rng) for k, rng in parts])
    for name_px, px, subset in ICON_SIZES:
        ranges = icon_ranges(entries, subset)
        lv_font_conv(npx, out_dir, f"font_icons_{name_px}", px, [(paths["material_round"], ranges)])
    write_text(out_dir / "icon_map.h", render_icon_map(entries, OPEN_HEADER))
    for fam in FAMILIES:
        (license_dir / fam.license_out).write_bytes(paths[fam.license].read_bytes())
    write_text(license_dir / "LICENSES.md", licenses_md())
    if preview:
        render_preview(preview, paths, entries)




# ------------------------------- preview ----------------------------------
def render_preview(dest: Path, paths: dict[str, Path], entries) -> None:
    """Contact sheet: every icon name -> Material glyph, plus text samples."""
    from PIL import Image, ImageDraw, ImageFont  # optional dependency

    label = ImageFont.truetype(str(paths["outfit"]), 15)
    icon_font = ImageFont.truetype(str(paths["material_round"]), 36)
    cols, cell_w, cell_h = 4, 280, 56
    rows = -(-len(entries) // cols)
    samples = [(paths[parts[0][0]], px, f"{sym}: All quiet - HOMELAB 42% 0123456789")
               for sym, px, parts in TEXT_FONTS]
    samples.append((paths["noto_symbols2"], 32, "spinner: \u2722 \u2733 \u2736 \u273B \u273D"))
    fonts = [ImageFont.truetype(str(path), px) for path, px, _ in samples]
    width = max(cols * cell_w, *(int(f.getlength(t)) + 16 for f, (_, _, t) in zip(fonts, samples)))
    text_h = sum(px + 24 for _, px, _ in samples)
    img = Image.new("RGB", (width, rows * cell_h + text_h + 20), (24, 22, 20))
    draw = ImageDraw.Draw(img)
    for i, (name, cp, glyph) in enumerate(entries):
        x, y = (i % cols) * cell_w + 8, (i // cols) * cell_h + 8
        draw.text((x, y), chr(cp), font=icon_font, fill=(232, 150, 110))
        draw.text((x + 48, y + 2), name, font=label, fill=(240, 236, 228))
        draw.text((x + 48, y + 21), f"{glyph} U+{cp:04X}", font=label, fill=(150, 140, 130))
    y = rows * cell_h + 16
    for font, (_, px, text) in zip(fonts, samples):
        draw.text((8, y), text, font=font, fill=(240, 236, 228))
        y += px + 24
    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest)
    print(f"  preview {dest}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--set", choices=SETS, default="open")
    ap.add_argument("--out", type=Path, metavar="DIR",
                    help="write everything into DIR (default: fonts + icon_map.h into "
                         f"{FONT_OUT_DIR.relative_to(ROOT).as_posix()}/, licenses into "
                         f"{LICENSE_OUT_DIR.relative_to(ROOT).as_posix()}/)")
    ap.add_argument("--preview", type=Path, help="also render a contact-sheet PNG (Pillow)")
    args = ap.parse_args(argv)
    try:
        out = args.out.absolute() if args.out else None
        build_open(out or FONT_OUT_DIR, out or LICENSE_OUT_DIR, args.preview)
    except PipelineError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
