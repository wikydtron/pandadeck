# Contributing to PandaDeck

Thanks for helping! Bug reports, ideas, docs fixes and pull requests are all welcome.

## Project layout

| Folder | What |
|---|---|
| `firmware/PandaDeck/` | The panel firmware (ESP32-S3, Arduino core, LVGL 9.5): pages, USB HID + CDC link, Wi-Fi API |
| `pio/` | PlatformIO project (custom sdkconfig for the 800×480 RGB panel) |
| `app/pandadeck/` | The desktop app (Python): tray, settings page, music, HUD, deck, sounds, AI |
| `app/lab_server/` | `pandadeck_lab.py`, the optional homelab companion |
| `docs/` | The user guide, the panel protocol (`protocol.md`) and the app notes (`app.md`) |
| `tests/` | Python tests (app, platforms) and C++ host tests for the firmware's pure modules |
| `tools/` | Font generator, demo/screenshot helpers, OTA flasher |

## Development setup

```bash
python -m pip install -e "./app[tray,mdns,ai-icons]" pytest
python -m pytest tests -q
```

Firmware host tests (g++ on Linux/macOS, MSYS2 MinGW on Windows):

```bash
git clone --depth 1 --branch v7.4.2 https://github.com/bblanchon/ArduinoJson.git firmware/libraries/ArduinoJson
bash tests/run_host_tests.sh
```

Building and flashing the firmware is described in
[docs/guide/10-build-from-source.md](../docs/guide/10-build-from-source.md).
To work on the settings page without a panel, run `python tools/demo_app.py --home <empty folder>`:
it serves the real UI against a pretend computer and panel.

## Pull requests

- One topic per pull request; describe what changed and how you tested it.
- Add or update tests with behaviour changes (the CI runs them on Windows, macOS and Linux).
- Keep the panel protocol backwards compatible, or document the change in `docs/protocol.md`.
- Commit messages: `type: summary` (`feat`, `fix`, `docs`, `refactor`, `test`, `chore`).

By contributing you agree that your work is released under the [MIT license](../LICENSE).
