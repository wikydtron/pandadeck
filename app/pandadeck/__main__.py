"""PandaDeck app entry point.

    pandadeck                run in the background (tray icon when pystray is installed)
    pandadeck --settings     open the settings page (starts the app if needed)
    pandadeck --headless     no tray icon (servers, autostart without a desktop)
    pandadeck --port N       preferred port (default 47321; the next free one is used if busy)
    pandadeck --version
Developer flags: --no-panel (never touch the panel), --demo (pretend panel),
--no-browser (don't open the page on first run).
"""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import signal
import sys
import threading
import webbrowser

from . import __version__, paths


def setup_logging(verbose: bool = False) -> None:
    paths.log_dir().mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [logging.handlers.RotatingFileHandler(
        paths.log_dir() / "pandadeck.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")]
    if sys.stderr and getattr(sys.stderr, "isatty", lambda: False)():
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def tray_image():
    """The tray icon, drawn at runtime: clay rounded square, ivory 'P'-ish deck grid."""
    from PIL import Image, ImageDraw
    s = 64
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((2, 6, s - 3, s - 7), radius=12, fill=(217, 119, 87, 255))
    for i in range(3):
        for j in range(2):
            x0, y0 = 13 + i * 14, 17 + j * 15
            d.rounded_rectangle((x0, y0, x0 + 10, y0 + 10), radius=3, fill=(240, 238, 230, 255))
    return img


def run_tray(svc, open_page, holder: dict | None = None) -> bool:
    """Blocks in the tray loop; False if pystray isn't available. The icon is
    put in holder["icon"] so a signal handler can stop the loop."""
    try:
        import pystray
    except Exception:  # noqa: BLE001 - not installed / no display
        return False

    def quit_app(icon, _item):
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem("Open settings", lambda *_: open_page(), default=True),
        pystray.MenuItem(lambda _i: _panel_line(svc), None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit PandaDeck", quit_app),
    )
    icon = pystray.Icon("pandadeck", tray_image(), "PandaDeck", menu)
    if holder is not None:
        holder["icon"] = icon
    try:
        icon.run()
    except Exception as e:  # noqa: BLE001 - e.g. no system tray on this desktop
        logging.getLogger("tray").info("tray unavailable: %s", e)
        return False
    return True


def _panel_line(svc) -> str:
    st = svc.links.status()
    if st["state"] == "connected":
        return f"Panel connected ({st['kind'].upper()})"
    return "Looking for the panel..."


def import_meme_sources(path: str) -> int:
    """`pandadeck --import-meme-sources PATH` (works while the app runs: the catalog is just a file)."""
    from pathlib import Path

    from .deck import userimg
    from .sounds.artwork import ArtError, Catalog, import_sources

    def has(image_id: str) -> bool:
        p = userimg.path_for(image_id)
        return p is not None and p.is_file()

    try:
        r = import_sources(Catalog(), Path(path), has, userimg.save_upload)
    except ArtError as e:
        print(f"Couldn't import: {e}", file=sys.stderr)
        return 2
    print(f"Meme pictures: {r['added']} added, {r['updated']} updated ({r['entries']} in the catalog).")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="pandadeck", description="PandaDeck desktop app")
    ap.add_argument("--settings", action="store_true", help="open the settings page")
    ap.add_argument("--headless", action="store_true", help="no tray icon")
    ap.add_argument("--port", type=int, default=None, help="preferred local port (default 47321)")
    ap.add_argument("--version", action="store_true")
    ap.add_argument("--no-browser", action="store_true", help="don't open the settings page on first run")
    ap.add_argument("--no-panel", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--demo", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--import-meme-sources", metavar="PATH",
                    help="add a sources.json of meme sound pictures to the picture catalog, then exit")
    args = ap.parse_args(argv)

    if args.version:
        print(f"PandaDeck {__version__}")
        return 0
    if args.import_meme_sources:
        return import_meme_sources(args.import_meme_sources)

    from .service import Service, settings_url

    url = settings_url()
    if url:  # already running: just show it
        if args.settings or not args.headless:
            webbrowser.open(url)
        print(f"PandaDeck is already running: {url}")
        return 0

    setup_logging(args.verbose)
    svc = Service(port=args.port, demo=args.demo, panel=not args.no_panel)
    try:
        svc.start()
    except OSError as e:
        logging.getLogger("main").error("can't start the settings server: %s", e)
        print(f"PandaDeck can't start: {e}", file=sys.stderr)
        return 3
    print(f"PandaDeck {__version__} running. Settings: {svc.url()}")

    def open_page():
        webbrowser.open(svc.url())

    if args.settings or (svc.settings.first_run and not args.no_browser):
        threading.Timer(0.8, open_page).start()

    done = threading.Event()
    tray: dict = {}

    def on_signal(*_):
        done.set()
        icon = tray.get("icon")
        if icon is not None:
            try:
                icon.stop()
            except Exception:  # noqa: BLE001
                pass

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, on_signal)
        except (ValueError, OSError):
            pass
    try:
        if args.headless or not run_tray(svc, open_page, tray):
            while not done.wait(1.0):
                pass
    finally:
        svc.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
