"""Linux icon-theme lookup on a fake directory tree (runs on every OS)."""
import os
from pathlib import Path

from PIL import Image

from pandadeck.platform import _icons as ic

HICOLOR_INDEX = """[Icon Theme]
Name=Hicolor
Directories=48x48/apps,256x256/apps,scalable/apps

[48x48/apps]
Size=48
Type=Threshold

[256x256/apps]
Size=256
MinSize=64
MaxSize=256
Type=Scalable

[scalable/apps]
Size=16
MinSize=16
MaxSize=256
Type=Scalable
"""

THEME_INDEX = """[Icon Theme]
Name=Mine
Inherits=Parent,hicolor
Directories=apps/48,16x16@2x/apps
ScaledDirectories=16x16@2x/apps

[apps/48]
Size=48
Type=Fixed

[16x16@2x/apps]
Size=16
Scale=2
Type=Fixed
"""

PARENT_INDEX = """[Icon Theme]
Name=Parent
Inherits=hicolor
Directories=128x128/apps

[128x128/apps]
Size=128
Type=Fixed
"""


def png(path: Path, px: int, color=(200, 30, 30, 255)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGBA", (px, px), color).save(path)
    return path


def svg(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16"/>', encoding="utf-8")
    return path


def make_tree(root: Path) -> tuple[Path, Path]:
    share = root / "share"
    icons = share / "icons"
    (icons / "hicolor").mkdir(parents=True)
    (icons / "hicolor" / "index.theme").write_text(HICOLOR_INDEX, encoding="utf-8")
    (icons / "Mine").mkdir()
    (icons / "Mine" / "index.theme").write_text(THEME_INDEX, encoding="utf-8")
    (icons / "Parent").mkdir()
    (icons / "Parent" / "index.theme").write_text(PARENT_INDEX, encoding="utf-8")
    png(icons / "hicolor" / "48x48" / "apps" / "chrome.png", 48)
    png(icons / "hicolor" / "256x256" / "apps" / "chrome.png", 256)
    svg(icons / "hicolor" / "scalable" / "apps" / "vector.svg")
    png(icons / "hicolor" / "48x48" / "apps" / "vector.png", 48)
    png(icons / "Mine" / "apps" / "48" / "themed.png", 48)
    png(icons / "hicolor" / "256x256" / "apps" / "themed.png", 256)
    png(icons / "Parent" / "128x128" / "apps" / "parented.png", 128)
    png(share / "pixmaps" / "legacy.png", 32)
    home = root / "home"
    home.mkdir()
    return share, home


def finder(tmp_path, themes=("Mine",), can_svg=False):
    share, home = make_tree(tmp_path)
    f = ic.IconFinder([share], home, list(themes), which=lambda _n: None)
    f._can_svg = can_svg
    return f, share


def test_parse_index_theme_reads_sizes_scales_and_inherits():
    inherits, dirs = ic.parse_index_theme(THEME_INDEX)
    assert inherits == ["Parent", "hicolor"]
    assert [(t.path, t.size, t.scalable) for t in dirs] == [("apps/48", 48, False), ("16x16@2x/apps", 32, False)]
    _, hdirs = ic.parse_index_theme(HICOLOR_INDEX)
    assert [(t.path, t.size, t.scalable) for t in hdirs][-1] == ("scalable/apps", 256, True)


def test_theme_chain_follows_inherits_once_and_ends_with_hicolor(tmp_path):
    f, _ = finder(tmp_path)
    assert f.chain == ["Mine", "Parent", "hicolor"]
    f2, _ = finder(tmp_path / "b", themes=("Missing",))
    assert f2.chain == ["Missing", "hicolor"]


def test_prefers_big_png_within_a_theme(tmp_path):
    f, share = finder(tmp_path)
    assert f.find("chrome") == share / "icons" / "hicolor" / "256x256" / "apps" / "chrome.png"
    assert f.find("chrome.png") == f.find("chrome")              # stray extension tolerated


def test_current_theme_wins_over_hicolor_even_if_smaller(tmp_path):
    f, share = finder(tmp_path)
    assert f.find("themed") == share / "icons" / "Mine" / "apps" / "48" / "themed.png"
    assert f.find("parented") == share / "icons" / "Parent" / "128x128" / "apps" / "parented.png"


def test_svg_only_when_it_can_be_rasterised(tmp_path):
    f, share = finder(tmp_path, can_svg=False)
    assert f.find("vector") == share / "icons" / "hicolor" / "48x48" / "apps" / "vector.png"
    f2, share2 = finder(tmp_path / "b", can_svg=True)
    assert f2.find("vector") == share2 / "icons" / "hicolor" / "scalable" / "apps" / "vector.svg"


def test_pixmaps_absolute_paths_and_misses(tmp_path):
    f, share = finder(tmp_path)
    assert f.find("legacy") == share / "pixmaps" / "legacy.png"
    if os.name == "posix":  # Icon=/abs/path.png (snaps do this)
        absolute = png(tmp_path / "abs" / "icon.png", 64)
        assert f.find(str(absolute)) == absolute
    assert f.find("/definitely/not/here.png") is None
    assert f.find("nope") is None and f.find("") is None


def test_load_returns_rgba_and_none_for_unrasterisable_svg(tmp_path):
    f, _ = finder(tmp_path)
    img = f.load("chrome")
    assert img.mode == "RGBA" and img.size == (256, 256)
    f2, _ = finder(tmp_path / "b", can_svg=True)
    # The lookup is told a rasteriser exists; drawing needs cairosvg/rsvg-convert for real.
    vec = f2.load("vector", px=64)
    assert vec is None or (vec.mode == "RGBA" and vec.size == (64, 64))


def test_pick_candidate_rules():
    p = Path("x")
    big = ic.Candidate(p / "256", 256, False)
    huge = ic.Candidate(p / "512", 512, False)
    mid = ic.Candidate(p / "128", 128, False)
    small = ic.Candidate(p / "48", 48, False)
    vec = ic.Candidate(p / "svg", 0, True)
    assert ic.pick_candidate([small, mid, huge, big, vec], True) is big
    assert ic.pick_candidate([small, huge], True) is huge
    assert ic.pick_candidate([small, vec], True) is vec
    assert ic.pick_candidate([small, ic.Candidate(p / "32", 32, False), vec], False) is small
    assert ic.pick_candidate([vec], False) is vec
    assert ic.pick_candidate([], True) is None


def test_current_icon_themes_reads_kdeglobals(tmp_path):
    cfg = tmp_path / ".config"
    cfg.mkdir()
    (cfg / "kdeglobals").write_text("[General]\nX=1\n[Icons]\nTheme=breeze-dark\n", encoding="utf-8")
    assert ic.current_icon_themes(tmp_path, {"XDG_CURRENT_DESKTOP": "KDE"}, lambda _n: None) == ["breeze-dark"]
    assert ic.current_icon_themes(tmp_path / "nobody", {}, lambda _n: None) == []
