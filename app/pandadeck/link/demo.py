"""A pretend panel for `--demo` (UI work and screenshots without hardware)."""
from __future__ import annotations

import time

from .base import Progress


class DemoLink:
    kind = "usb"
    where = "demo"

    def __init__(self):
        self.boot = int(time.time()) & 0xFFFF
        self.settings = {"pages": ["deck", "music", "hud"], "brightness": 80, "flip": False, "tflip": False,
                         "name": "PandaDeck"}
        self.icons: set[str] = set()

    def hello(self) -> dict:
        return {"app": "pandadeck", "fw": "1.0.0", "boot": self.boot, "board": "panda-touch",
                "wifi": {"configured": True, "connected": True, "ip": "10.0.0.50", "ssid": "Home"},
                "settings": dict(self.settings), "features": ["usb", "wifi"], "demo": True}

    def sync(self, snap: dict) -> dict:
        return {"ok": True, "fw": "1.0.0", "boot": self.boot, "icons": len(self.icons), "events": []}

    def image(self, hdr: dict, data: bytes) -> bool:
        self.icons.add(hdr.get("key") or hdr.get("kind", ""))
        return True

    def config(self, d: dict) -> dict:
        for k in ("pages", "brightness", "flip", "tflip", "name"):
            if k in d:
                self.settings[k] = d[k]
        return {"ok": True, "settings": dict(self.settings), "reboot": False}

    def update(self, firmware: bytes, progress: Progress | None = None) -> None:
        for i in range(0, len(firmware) + 1, max(1, len(firmware) // 20)):
            if progress:
                progress(min(i, len(firmware)), len(firmware))
            time.sleep(0.05)

    def close(self) -> None:
        pass
