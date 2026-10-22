# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec: one-folder app per OS.

    cd app && pyinstaller packaging/pandadeck.spec --noconfirm

Windows -> dist/PandaDeck/PandaDeck.exe (windowed, tray icon)
macOS   -> dist/PandaDeck.app (LSUIElement: menu-bar only, no Dock icon)
Linux   -> dist/PandaDeck/PandaDeck

Per-OS notes:
  * Windows: the winrt-* and pycaw packages must be installed in the build
    environment (pip install .[tray]); they are collected below.
  * macOS: first run asks for Automation permission (Spotify / Music). Sign
    and notarize the .app for distribution (not done here).
  * Linux: the user must be in the `dialout` (or `uucp`) group to open the
    panel's serial port; MPRIS needs a D-Bus session (jeepney).
"""
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

APP = Path(SPECPATH).resolve().parent  # noqa: F821 - provided by PyInstaller
PKG = APP / "pandadeck"

hidden = collect_submodules("pandadeck")
for optional in ("pystray", "zeroconf", "serial.tools.list_ports", "resvg_py"):
    try:
        hidden += collect_submodules(optional)
    except Exception:  # noqa: BLE001 - optional dependency not installed
        pass
if sys.platform == "win32":
    for mod in ("winrt", "pycaw", "comtypes"):
        try:
            hidden += collect_submodules(mod)
        except Exception:  # noqa: BLE001
            pass
elif sys.platform.startswith("linux"):
    hidden += collect_submodules("jeepney")

# Every package's static/ folder (the settings page and any optional extension UI) + deck defaults.
datas = [(str(d), str(d.relative_to(APP)).replace("\\", "/")) for d in PKG.rglob("static") if d.is_dir()]
datas.append((str(PKG / "deck" / "defaults.json"), "pandadeck/deck"))

a = Analysis(  # noqa: F821
    [str(APP / "packaging" / "run_pandadeck.py")],
    pathex=[str(APP)],
    datas=datas,
    hiddenimports=hidden,
    excludes=["pytest"],  # tkinter stays: the Browse... buttons use its file dialog on Windows/Linux
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="PandaDeck",
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="PandaDeck", upx=False)  # noqa: F821

if sys.platform == "darwin":
    app = BUNDLE(  # noqa: F821
        coll,
        name="PandaDeck.app",
        bundle_identifier="com.pandadeck.app",
        info_plist={
            "LSUIElement": True,
            "NSAppleEventsUsageDescription": "PandaDeck shows and controls what Spotify or Music is playing.",
            "CFBundleShortVersionString": "1.0.0",
        },
    )
