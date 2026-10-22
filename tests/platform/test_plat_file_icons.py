"""Platform.file_icon(path): icons for "open this file / folder" deck buttons."""
import os
import plistlib
import sys
from pathlib import Path

import pytest
from PIL import Image

from pandadeck.platform import _desktop as desk
from pandadeck.platform import _icons as ic
from pandadeck.platform import _util
from pandadeck.platform import linux as lx
from pandadeck.platform import macos as mac
from pandadeck.platform import windows as w


# ---- shared path handling ------------------------------------------------------------------------

def test_normalize_path(tmp_path):
    f = tmp_path / "a b.txt"
    f.write_text("x", encoding="utf-8")
    assert _util.normalize_path(str(f)) == os.path.normpath(str(f))
    assert _util.normalize_path(f'  "{f}"  ') == os.path.normpath(str(f))     # Explorer's "Copy as path"
    assert _util.normalize_path(str(tmp_path)) == os.path.normpath(str(tmp_path))
    for bad in ("", "   ", "relative.txt", str(tmp_path / "missing.txt"), f"{f}\x00", None, 42):
        assert _util.normalize_path(bad) is None


# ---- Windows (logic; the live shell render is in test_plat_windows_live.py) -------------------------

class FakeShellIcons:
    def __init__(self):
        self.paths = []

    def path_icon(self, path, px=256):
        self.paths.append(path)
        return Image.new("RGBA", (px, px), (1, 2, 3, 255))


def test_windows_file_icon_uses_the_shell_for_existing_paths_only(tmp_path):
    plat = w.WindowsPlatform(registry=object())
    fake = FakeShellIcons()
    plat._icons = fake
    f = tmp_path / "notes.txt"
    f.write_text("hi", encoding="utf-8")
    assert plat.file_icon(str(f)).size == (256, 256)
    assert plat.file_icon(str(tmp_path)) is not None
    assert plat.file_icon(str(tmp_path / "nope.txt")) is None and plat.file_icon("relative") is None
    assert fake.paths == [os.path.normpath(str(f)), os.path.normpath(str(tmp_path))]


@pytest.mark.skipif(sys.platform != "win32" or bool(os.environ.get("CI")), reason="needs the Windows shell")
def test_windows_file_icon_live(tmp_path):
    from pandadeck.platform import current
    plat = current()
    f = tmp_path / "notes.txt"
    f.write_text("hi", encoding="utf-8")
    for path in (sys.executable, str(f), str(tmp_path)):
        img = plat.file_icon(path)
        assert img is not None and img.mode == "RGBA" and min(img.size) >= 128, path
    assert plat.file_icon(str(tmp_path / "missing.txt")) is None


# ---- macOS -------------------------------------------------------------------------------------------

def test_mac_file_icon_app_bundle_uses_its_icns(tmp_path):
    bundle = tmp_path / "Paint.app" / "Contents"
    (bundle / "Resources").mkdir(parents=True)
    (bundle / "Info.plist").write_bytes(plistlib.dumps({"CFBundleIconFile": "PaintIcon"}))
    try:
        Image.new("RGBA", (128, 128), (30, 120, 220, 255)).save(bundle / "Resources" / "PaintIcon.icns",
                                                                format="ICNS")
    except Exception:  # noqa: BLE001
        pytest.skip("this Pillow can't write .icns")
    plat = mac.MacPlatform(home=tmp_path, roots=[], which=lambda _n: None)
    img = plat.file_icon(str(tmp_path / "Paint.app"))
    assert img is not None and img.mode == "RGBA"


def test_mac_file_icon_other_paths_use_quicklook(tmp_path, monkeypatch):
    doc = tmp_path / "report.pdf"
    doc.write_bytes(b"%PDF-1.4")
    calls = []

    def fake_run(args, timeout=5.0, **kw):
        calls.append(args)
        out_dir = Path(args[args.index("-o") + 1])
        Image.new("RGBA", (256, 200), (9, 9, 9, 255)).save(out_dir / f"{Path(args[-1]).name}.png")
        return ""
    monkeypatch.setattr(mac, "run", fake_run)
    plat = mac.MacPlatform(home=tmp_path, roots=[], which=lambda n: f"/usr/bin/{n}")
    img = plat.file_icon(str(doc))
    assert img is not None and img.size == (256, 200)
    assert calls[0][:5] == ["qlmanage", "-t", "-s", "256", "-o"] and calls[0][-1] == os.path.normpath(str(doc))
    assert plat.file_icon(str(tmp_path / "missing.pdf")) is None and len(calls) == 1
    no_ql = mac.MacPlatform(home=tmp_path, roots=[], which=lambda _n: None)
    assert no_ql.file_icon(str(doc)) is None


# ---- Linux: pure helpers --------------------------------------------------------------------------------

def test_file_icon_names_follow_the_icon_naming_spec():
    generic = {"text/x-python": "text-x-script"}
    assert ic.file_icon_names("text/x-python", generic) == \
        ["text-x-python", "text-x-script", "text-x-generic", "application-x-generic", "unknown"]
    assert ic.file_icon_names("image/png", {})[:2] == ["image-png", "image-x-generic"]
    assert ic.file_icon_names(None, {}, executable=True)[0] == "application-x-executable"
    assert ic.file_icon_names(None, {}) == ic.FILE_FALLBACKS


def test_folder_icon_names_and_user_dirs():
    user_dirs = ic.parse_user_dirs('# comment\nXDG_DOWNLOAD_DIR="$HOME/Downloads"\n'
                                   'XDG_MUSIC_DIR="/data/Music"\nXDG_WEIRD_DIR="$HOME/x"\n', "/home/u")
    assert user_dirs == {os.path.normpath("/home/u/Downloads"): "folder-download",
                         os.path.normpath("/data/Music"): "folder-music"}
    assert ic.folder_icon_names("/home/u", "/home/u", user_dirs)[0] == "user-home"
    assert ic.folder_icon_names("/home/u/Downloads", "/home/u", user_dirs)[0] == "folder-download"
    assert ic.folder_icon_names("/home/u/src", "/home/u", user_dirs) == ["folder", "inode-directory"]


def test_parse_generic_icons():
    assert ic.parse_generic_icons("application/x-shellscript:text-x-script\nbad line\n") == \
        {"application/x-shellscript": "text-x-script"}


# ---- Linux: end to end on a fake icon theme -------------------------------------------------------------

def linux_plat(tmp_path, monkeypatch, which=lambda _n: None):
    share = tmp_path / "share"
    hicolor = share / "icons" / "hicolor"
    (hicolor / "48x48").mkdir(parents=True)
    (hicolor / "index.theme").write_text("[Icon Theme]\nDirectories=48x48/places,48x48/mimetypes,48x48/apps\n"
                                         "[48x48/places]\nSize=48\n[48x48/mimetypes]\nSize=48\n"
                                         "[48x48/apps]\nSize=48\n", encoding="utf-8")
    colors = {"places/folder": (200, 160, 40), "places/user-home": (40, 160, 200),
              "mimetypes/image-png": (10, 200, 10), "mimetypes/text-x-script": (90, 90, 200),
              "mimetypes/text-x-generic": (220, 220, 220), "apps/my-editor": (250, 0, 250)}
    for rel, rgb in colors.items():
        p = hicolor / "48x48" / f"{rel}.png"
        p.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGBA", (48, 48), (*rgb, 255)).save(p)
    (share / "mime").mkdir()
    (share / "mime" / "generic-icons").write_text("text/x-made-up:text-x-script\n", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(desk, "data_dirs", lambda env, h: [share])
    return lx.LinuxPlatform(home=home, env={}, which=which), home


def colour(img):
    return img.getpixel((24, 24))[:3]


def test_linux_file_icons(tmp_path, monkeypatch):
    plat, home = linux_plat(tmp_path, monkeypatch)
    proj = home / "proj"
    proj.mkdir()
    png = proj / "shot.png"
    png.write_bytes(b"\x89PNG")
    odd = proj / "data.zzz-unknown"
    odd.write_text("?", encoding="utf-8")
    launcher = proj / "editor.desktop"
    launcher.write_text("[Desktop Entry]\nType=Application\nName=Ed\nExec=ed\nIcon=my-editor\n", encoding="utf-8")
    assert colour(plat.file_icon(str(proj))) == (200, 160, 40)          # folder
    assert colour(plat.file_icon(str(home))) == (40, 160, 200)          # user-home
    assert colour(plat.file_icon(str(png))) == (10, 200, 10)            # image/png by extension
    assert colour(plat.file_icon(str(odd))) == (220, 220, 220)          # unknown -> text-x-generic
    assert colour(plat.file_icon(str(launcher))) == (250, 0, 250)       # the launcher's own Icon=
    assert plat.file_icon(str(proj / "missing")) is None and plat.file_icon("rel/path") is None


def test_linux_file_icon_prefers_xdg_mime_and_generic_icons(tmp_path, monkeypatch):
    plat, home = linux_plat(tmp_path, monkeypatch, which=lambda n: "/usr/bin/xdg-mime" if n == "xdg-mime" else None)
    f = home / "script"
    f.write_text("#!/bin/sh\n", encoding="utf-8")
    calls = []

    def fake_run(args, **kw):
        calls.append(args)
        return "text/x-made-up; charset=us-ascii\n"
    monkeypatch.setattr(lx, "run", fake_run)
    assert colour(plat.file_icon(str(f))) == (90, 90, 200)              # via mime/generic-icons
    assert calls == [["/usr/bin/xdg-mime", "query", "filetype", os.path.normpath(str(f))]]
