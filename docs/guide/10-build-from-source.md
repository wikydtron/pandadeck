# 10. Build from source

You only need this if you want to change PandaDeck. Ready-made firmware and app builds
are on the [releases page](../../../../releases/latest).

```
app/         the desktop app (Python, Windows / macOS / Linux)
firmware/    the panel firmware (Arduino-ESP32 + LVGL 9)
pio/         PlatformIO project that builds the firmware with a tuned ESP-IDF config
tools/       font generator, TinyUSB fetcher, OTA + screenshot helpers
docs/        this guide, the USB/Wi-Fi protocol and the app architecture
tests/       firmware model/protocol tests (host C++) and app tests (pytest)
```

## Firmware

The firmware is built with [PlatformIO](https://platformio.org) and
[pioarduino](https://github.com/pioarduino/platform-espressif32), which recompiles the
Arduino core with settings tuned for the panel's RGB display (64 KB data cache, IRAM-safe
display interrupt, code running from PSRAM). The stock prebuilt Arduino core works too,
but the picture can glitch under load.

1. Install Python 3.10+ and git, then PlatformIO:
   ```
   pip install -U platformio
   ```
2. Clone this repository **to a short path** (on Windows, e.g. `C:\pandadeck` — the
   ESP-IDF build hits Windows' path-length limit in deep folders).
3. Fetch the USB stack component (TinyUSB for the keyboard + serial link):
   ```
   python tools/fetch_tinyusb.py
   ```
4. Build:
   ```
   pio run -d pio
   ```
   The first build downloads the toolchain and compiles ESP-IDF — expect 10–20 minutes.
   Later builds take about a minute. Output: `pio/.pio/build/pandadeck/firmware.bin`.
5. Flash it from the app (**Panel → Firmware → Install from file**), from the browser
   (`http://<panel>/update`, see [Wi-Fi and updates](08-wifi-and-updates.md)), or over USB:
   ```
   pio run -d pio -t upload
   ```

**Windows tips:** run the build from PowerShell or cmd (not Git Bash / MSYS2, which the
ESP-IDF tools refuse). If you see "Filename too long", move the clone to a shorter path
or set `PLATFORMIO_BUILD_DIR` to something like `C:\pdb`.

### Optional compile-time settings

The firmware needs no configuration: Wi-Fi and everything else are set from the app and
stored on the panel. For your own builds you may still create
`firmware/PandaDeck/config.h` from `config.h.example` to bake in defaults.

### Fonts

The fonts are generated LVGL bitmaps. To change them:

```
python tools/make_fonts.py
```

This downloads the open-licensed source fonts (pinned by checksum), runs
[`lv_font_conv`](https://github.com/lvgl/lv_font_conv) through `npx` (Node.js needed),
and writes `firmware/PandaDeck/font_*.c` plus `icon_map.h` (icon names → codepoints) and the
font licenses (`firmware/fonts/LICENSES.md` + `LICENSE-*`).

### Tests

```
bash tests/run_host_tests.sh      # firmware data model + USB protocol (needs g++)
```

## App

```
cd app
pip install -e .[dev]
pandadeck --headless              # run without the tray icon
python -m pytest -q ../tests      # app tests
```

The settings page is at <http://127.0.0.1:47321>. See
[docs/app.md](../app.md) for the architecture and [docs/protocol.md](../protocol.md) for
the panel protocol (USB frames and HTTP API).

### Packaging

Each release builds standalone apps for Windows, macOS and Linux with PyInstaller in
GitHub Actions (`.github/workflows/app.yml`). To build locally:

```
pip install pyinstaller
pyinstaller app/packaging/pandadeck.spec
```

## Contributing

Issues and pull requests are welcome. Please run the tests before sending a PR, and keep
the firmware's UI responsive: every LVGL setter redraws, so the UI code only touches
what changed.
