"""Fetch the `arduino_tinyusb` ESP-IDF component the PlatformIO build needs.

pioarduino's "hybrid" build (custom sdkconfig) recompiles the Arduino core
from ESP-IDF components, but its template ships without `arduino_tinyusb`,
which USB HID/CDC (the stream-deck keys) depend on. This recreates it exactly
as Espressif's lib-builder does for the Arduino-ESP32 version the build uses:
the component files from esp32-arduino-lib-builder + TinyUSB at the pinned
commit + lib-builder's TinyUSB patches.

usage:  python tools/fetch_tinyusb.py            # -> pio/components/arduino_tinyusb
        python tools/fetch_tinyusb.py <dest-dir>
Needs git on PATH. Safe to re-run (replaces the folder).
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# From framework-arduinoespressif32-libs/esp32s3/versions.txt (Arduino-ESP32 3.3.x)
LIB_BUILDER = ("https://github.com/espressif/esp32-arduino-lib-builder.git",
               "6671d0bd65cdb9d4cc1001b759e8610de945a8d5")
TINYUSB = ("https://github.com/hathach/tinyusb.git", "88c7f0b812ad516adc670bf67627911021f0951e")

ROOT = Path(__file__).resolve().parents[1]


def git(*args: str, cwd: Path | None = None) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True)


def main() -> None:
    dest = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else ROOT / "pio" / "components" / "arduino_tinyusb"
    with tempfile.TemporaryDirectory(prefix="pdtusb") as tmp:
        lb = Path(tmp) / "lb"
        print(f"- esp32-arduino-lib-builder @ {LIB_BUILDER[1][:7]}")
        git("clone", "-q", "--filter=blob:none", "--no-checkout", LIB_BUILDER[0], str(lb))
        git("checkout", "-q", LIB_BUILDER[1], cwd=lb)
        if dest.exists():
            shutil.rmtree(dest, onerror=lambda f, p, _: (Path(p).chmod(0o700), f(p)))
        shutil.copytree(lb / "components" / "arduino_tinyusb", dest)

        tusb = dest / "tinyusb"
        print(f"- tinyusb @ {TINYUSB[1][:9]}")
        tusb.mkdir()
        git("init", "-q", cwd=tusb)
        git("remote", "add", "origin", TINYUSB[0], cwd=tusb)
        git("fetch", "-q", "--depth", "1", "origin", TINYUSB[1], cwd=tusb)
        git("checkout", "-q", "FETCH_HEAD", cwd=tusb)
        for patch in sorted((lb / "patches" / "tinyusb").glob("*.diff")):
            print(f"- patch {patch.name}")
            git("apply", str(patch), cwd=tusb)
    print(f"done: {dest}")


if __name__ == "__main__":
    main()
