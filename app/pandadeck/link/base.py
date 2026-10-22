"""What every panel link (USB or Wi-Fi) offers. Same JSON payloads both ways;
see docs/protocol.md."""
from __future__ import annotations

from typing import Callable, Protocol

Progress = Callable[[int, int], None]  # (bytes done, total)

MAX_FIRMWARE = 8 * 1024 * 1024
ESP_IMAGE_MAGIC = 0xE9


class LinkError(OSError):
    """The panel didn't answer (unplugged, rebooting, busy) or refused."""


class Unsupported(LinkError):
    """The panel's firmware doesn't know this request (older firmware)."""


class Link(Protocol):
    kind: str      # "usb" | "wifi"
    where: str     # "COM5" / "/dev/ttyACM0" / "10.0.0.50"

    def hello(self) -> dict: ...
    def sync(self, snap: dict) -> dict: ...
    def image(self, hdr: dict, data: bytes) -> bool: ...
    def config(self, d: dict) -> dict: ...
    def update(self, firmware: bytes, progress: Progress | None = None) -> None: ...
    def close(self) -> None: ...


def check_firmware(data: bytes) -> dict:
    """Validate an ESP32 app image before flashing. Returns what we learned:
    {"size", "project", "version", "idf", "pandadeck", "stock"}. Raises ValueError."""
    import struct

    if not data or data[0] != ESP_IMAGE_MAGIC:
        raise ValueError("this isn't an ESP32 firmware file (.bin with the 0xE9 header)")
    if len(data) > MAX_FIRMWARE:
        raise ValueError("this file is too big to be panel firmware")
    if len(data) < 256:
        raise ValueError("this file is too small to be panel firmware")
    info = {"size": len(data), "project": "", "version": "", "idf": ""}
    off = 24 + 8  # image header + first segment header -> esp_app_desc_t
    if len(data) >= off + 144 and struct.unpack_from("<I", data, off)[0] == 0xABCD5432:
        def s(a: int, n: int) -> str:
            return data[off + a:off + a + n].split(b"\0")[0].decode("ascii", "replace")
        info.update(version=s(16, 32), project=s(48, 32), idf=s(112, 32))
    info["pandadeck"] = b"PandaDeck" in data
    info["stock"] = info["project"].startswith("knomi")
    return info
