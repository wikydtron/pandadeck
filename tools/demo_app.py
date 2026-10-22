"""Run the settings page against a pretend computer and panel (screenshots, UI work).

    python tools/demo_app.py --home <empty folder> [--port 47399]

Everything it shows is made up here: the apps (drawn icons), the music, the PC's
name and model. Nothing is read from this computer's app list, media, hardware
names, ~/.claude or ~/.codex, and the pretend panel never touches real hardware.
Settings live in --home only.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEMO_PC = "Studio PC"
DEMO_CPU = "AMD Ryzen 7 7700X 8-Core Processor"


def demo_platform():
    sys.path.insert(0, str(ROOT / "tools"))
    import demo
    from pandadeck.platform.base import AppInfo, NowPlaying

    apps = {f"demo.{i}": a for i, a in enumerate(demo.APPS)}
    cover = demo.synthwave_cover()
    t0 = time.time()

    class Audio:
        muted = False

        def volume(self):
            return 42

        def mic_muted(self):
            return self.muted

        def toggle_mic(self):
            self.muted = not self.muted

    class Media:
        def now_playing(self):
            return NowPlaying("Spotify", "Midnight Static", "Neon Harbor", "City Lights", playing=True,
                              pos=(74 + time.time() - t0) % 236, dur=236, shuffle=1, repeat=0, art=cover,
                              art_key="demo-cover")

        def command(self, cmd):
            pass

    class Platform:
        name = {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux")

        def __init__(self):
            self.audio_backend, self.media_backend, self.auto = Audio(), Media(), False

        def list_apps(self):  # most-used first
            return [AppInfo(i, a[0], float(100 - n * 5)) for n, (i, a) in enumerate(apps.items())]

        def app_icon(self, app_id):
            a = apps.get(app_id)
            return demo.app_icon(*a) if a else None

        def launch_app(self, app_id):
            pass

        def media(self):
            return self.media_backend

        def audio(self):
            return self.audio_backend

        def set_autostart(self, enabled, argv):
            self.auto = enabled

        def autostart_enabled(self):
            return self.auto

    return Platform()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", required=True, help="an empty folder for the demo's settings")
    ap.add_argument("--port", type=int, default=47399)
    args = ap.parse_args()
    home = Path(args.home).resolve()
    home.mkdir(parents=True, exist_ok=True)
    os.environ["PANDADECK_HOME"] = str(home)                 # settings, cache, logs
    os.environ["PANDADECK_USER_HOME"] = str(home / "user")   # never the real ~/.claude, ~/.codex
    sys.path.insert(0, str(ROOT / "app"))

    from pandadeck.hud import hw, pc                          # no real hardware names
    pc.cpu_brand = lambda: DEMO_CPU
    pc.host_name = hw.host_name = lambda: DEMO_PC
    import shutil
    real_which = shutil.which
    shutil.which = lambda name, *a, **k: None if "nvidia" in name else real_which(name, *a, **k)
    from pandadeck.ai import icons
    icons.find_engines = lambda: {}                           # no real Claude / Codex runs

    from pandadeck.service import Service
    svc = Service(port=args.port, demo=True, panel=True, platform=demo_platform())
    svc.settings.update({"hud": {"pc_name": DEMO_PC}, "pages": ["ai", "deck", "music", "hud"]})
    svc.start()
    print(f"demo settings page: {svc.url()}  (Ctrl+C to stop)", flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
