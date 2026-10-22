"""Show made-up sample data on a PandaDeck panel (for screenshots and demos).

    python tools/demo.py --url http://pandadeck.local --token <panel token> [--page music] [--shots out/]

Everything shown is fictional and generated here (cover art, app icons, projects),
so screenshots never leak anyone's real apps, music or sessions. Stop the PandaDeck
app first, otherwise it overwrites the demo within a second.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import sys
import threading
import time
import urllib.request
import uuid
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parents[1]
PAGES = ("ai", "deck", "music", "hud")

# --------------------------------------------------------------------------------------------
# artwork


def synthwave_cover(px: int = 640) -> Image.Image:
    """A fictional album cover: sunset gradient, striped sun, perspective grid."""
    img = Image.new("RGB", (px, px))
    d = ImageDraw.Draw(img)
    top, mid, bot = (35, 12, 74), (196, 54, 120), (255, 160, 70)
    horizon = int(px * 0.62)
    for y in range(horizon):
        t = y / horizon
        a, b, u = (top, mid, t * 2) if t < 0.5 else (mid, bot, (t - 0.5) * 2)
        d.line([(0, y), (px, y)], fill=tuple(int(a[i] + (b[i] - a[i]) * u) for i in range(3)))
    # sun with stripes
    r = int(px * 0.22)
    cx, cy = px // 2, horizon - int(r * 0.35)
    sun = Image.new("L", (px, px), 0)
    ImageDraw.Draw(sun).ellipse((cx - r, cy - r, cx + r, cy + r), fill=255)
    sd = ImageDraw.Draw(sun)
    for i in range(5):  # classic retro-sun slits, thicker towards the horizon
        y = cy - int(r * 0.25) + i * int(r * 0.13)
        sd.rectangle((0, y, px, y + 3 + i * 3), fill=0)
    sun_col = Image.new("RGB", (px, px), (255, 214, 102))
    img.paste(sun_col, (0, 0), sun)
    # ground + grid
    d.rectangle((0, horizon, px, px), fill=(22, 8, 40))
    grid = (255, 72, 196)
    for i in range(1, 12):
        y = horizon + int((px - horizon) * (i / 11) ** 1.8)
        d.line([(0, y), (px, y)], fill=grid, width=2)
    for i in range(-10, 11):
        d.line([(px // 2 + i * 14, horizon), (px // 2 + i * 90, px)], fill=grid, width=2)
    glow = img.filter(ImageFilter.GaussianBlur(6))
    return Image.blend(img, glow, 0.25)


APPS = [  # (label, material glyph name, colour A, colour B)
    ("Browser", "public", (66, 133, 244), (52, 168, 83)), ("Mail", "mail", (56, 132, 255), (30, 80, 200)),
    ("Calendar", "event", (255, 94, 91), (220, 50, 70)), ("Notes", "sticky_note_2", (255, 204, 0), (255, 150, 0)),
    ("Terminal", "terminal", (60, 60, 64), (20, 20, 22)), ("Code", "code", (0, 122, 204), (0, 90, 160)),
    ("Music", "music_note", (255, 70, 130), (190, 40, 220)), ("Photos", "photo_library", (255, 149, 0), (255, 59, 48)),
    ("Files", "folder", (90, 160, 255), (40, 110, 230)), ("Chat", "forum", (88, 101, 242), (70, 80, 200)),
    ("Calculator", "calculate", (255, 159, 10), (230, 110, 0)), ("Settings", "settings", (142, 142, 147), (90, 90, 96)),
    ("Camera", "photo_camera", (40, 40, 44), (80, 80, 86)), ("Maps", "map", (52, 199, 89), (40, 150, 70)),
    ("Weather", "wb_sunny", (90, 200, 250), (0, 122, 255)), ("Games", "sports_esports", (175, 82, 222), (120, 50, 180)),
    ("Store", "storefront", (0, 199, 190), (0, 140, 140)), ("Design", "palette", (255, 55, 95), (255, 120, 70)),
]


def _material_font(size: int) -> tuple[ImageFont.FreeTypeFont | None, dict[str, int]]:
    cache = ROOT / ".fonts-cache"
    fonts = sorted(cache.glob("MaterialIcons*Round*.*tf")) or sorted(cache.glob("MaterialIcons*.*tf"))
    cps = sorted(cache.glob("MaterialIcons*Round*.codepoints")) or sorted(cache.glob("*.codepoints"))
    if not fonts or not cps:
        return None, {}
    table = {}
    for line in cps[0].read_text().splitlines():
        if " " in line:
            name, cp = line.split()
            table[name] = int(cp, 16)
    return ImageFont.truetype(str(fonts[0]), size), table


def app_icon(label: str, glyph: str, a, b, px: int = 256) -> Image.Image:
    img = Image.new("RGBA", (px, px), (0, 0, 0, 0))
    grad = Image.new("RGB", (px, px))
    gd = ImageDraw.Draw(grad)
    for y in range(px):
        t = y / px
        gd.line([(0, y), (px, y)], fill=tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3)))
    mask = Image.new("L", (px, px), 0)
    ImageDraw.Draw(mask).rounded_rectangle((8, 8, px - 8, px - 8), radius=px // 4.5, fill=255)
    img.paste(grad, (0, 0), mask)
    font, table = _material_font(int(px * 0.55))
    d = ImageDraw.Draw(img)
    if font and glyph in table:
        ch = chr(table[glyph])
        bb = d.textbbox((0, 0), ch, font=font)
        d.text(((px - bb[2] - bb[0]) / 2, (px - bb[3] - bb[1]) / 2), ch, font=font, fill=(255, 255, 255))
    else:
        f = ImageFont.load_default()
        d.text((px / 2, px / 2), label[0], font=f, fill="white", anchor="mm")
    return img


# --------------------------------------------------------------------------------------------
# snapshots


def demo_snapshot(t0: float, playing: bool = True) -> dict:
    now = time.time()
    return {
        "t": int(now), "ack": 0, "boot": 0,
        "sessions": [
            {"id": "s1", "proj": "weather-app", "title": "Hourly forecast chart", "act": "Bash: npm test",
             "host": "desktop", "model": "claude", "st": "work", "age": 2, "dur": 1260, "src": "claude"},
            {"id": "s2", "proj": "api-server", "title": "Rate limiting", "act": "Done: added token bucket + tests",
             "host": "desktop", "model": "claude", "st": "wait", "age": 35, "dur": 3480, "src": "claude"},
            {"id": "s3", "proj": "landing-page", "title": "Hero section", "act": "Edit: src/components/Hero.tsx",
             "host": "laptop", "model": "codex", "st": "work", "age": 4, "dur": 610, "src": "codex"},
        ],
        "perms": [],
        "usage": {"ok": True, "s5h": 34, "s5h_reset": 7920, "week": 58, "week_reset": 288000,
                  "codex_week": 21, "codex_reset": 410000},
        "pc": {"name": "Studio PC", "cpu_model": "Ryzen 7 7700X", "gpu_model": "RTX 4070",
               "cpu": 23.5 + 6 * math.sin(now / 3), "ram": 46.8,
               "disk": 62.0, "down": 18.4, "up": 2.1, "cpu_t": 58, "gpu": 31, "gpu_t": 54, "gpu_w": 96.5,
               "vram": 38.2, "mic_muted": False, "vol": 42},
        "lab": {"ok": True, "name": "homelab", "cpu": 12.4, "ram": 61.0, "temp": 47, "gpu": -1, "gpu_t": -1,
                "disk": 71, "run": 23, "total": 24, "healthy": 23, "backup_h": 5.2, "bad": [],
                "top": [{"id": "a1a1a1a1a1a1", "name": "media-server", "state": "running", "health": "healthy",
                         "cpu": 8.1, "mem": 6.3},
                        {"id": "b2b2b2b2b2b2", "name": "photos", "state": "running", "health": "healthy",
                         "cpu": 3.4, "mem": 4.8},
                        {"id": "c3c3c3c3c3c3", "name": "smart-home", "state": "running", "health": "healthy",
                         "cpu": 2.2, "mem": 3.9}]},
        "media": {"ok": True, "app": "Spotify", "title": "Midnight Static", "artist": "Neon Harbor",
                  "album": "City Lights", "playing": playing, "pos": 74 + (int(now - t0) if playing else 0),
                  "dur": 236, "shuffle": 1, "repeat": 0, "art": DEMO_TAG, "tint": "#FF6FB0",
                  "app_color": "#1DB954", "liked": 1},
        "deck": DEMO_DECK,
    }


DEMO_TAG = ""
DEMO_DECK = {
    "rev": 0x1D3E0002, "style": "classic", "labels": True,
    "pages": [
        {"name": "APPS", "color": "#D97757", "cols": 6,
         "buttons": [{"label": a[0], "img": f"demo{i}", "action": "keys:F24"} for i, a in enumerate(APPS)]},
        {"name": "MEDIA", "color": "#C084FC", "buttons": [
            {"label": "Prev", "icon": "prev", "action": "media:prev"},
            {"label": "Play", "icon": "play", "action": "media:playpause"},
            {"label": "Next", "icon": "next", "action": "media:next"},
            {"label": "Mute", "icon": "mute", "action": "media:mute"},
            {"label": "Vol -", "icon": "vol_down", "action": "media:voldown"},
            {"label": "Vol +", "icon": "vol_up", "action": "media:volup"},
            {"label": "Mic", "icon": "mic", "action": "agent:mic"},
            {"label": "Screenshot", "icon": "crop", "action": "keys:GUI+SHIFT+S"},
            {"label": "Desktop", "icon": "home", "action": "keys:GUI+D"},
            {"label": "Task view", "icon": "taskview", "action": "keys:GUI+TAB"},
            {"label": "Settings", "icon": "settings", "action": "keys:GUI+I"},
            {"label": "Lock", "icon": "lock", "action": "keys:GUI+L"}]},
        {"name": "WINDOWS", "color": "#6FA8DC", "buttons": []},
        {"name": "MACROS", "color": "#5FB477", "buttons": []},
    ],
}

# --------------------------------------------------------------------------------------------
# transport (HTTP + token; the panel must be on Wi-Fi)


class Panel:
    def __init__(self, url: str, token: str):
        self.url, self.token = url.rstrip("/"), token

    def _req(self, path, data=None, ctype="application/json", timeout=20):
        req = urllib.request.Request(self.url + path, data=data, method="POST" if data is not None else "GET",
                                     headers={"X-Panda-Token": self.token, "Content-Type": ctype})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()

    def post_json(self, path, obj):
        return self._req(path, json.dumps(obj).encode())

    def upload(self, path, payload: bytes):
        b = uuid.uuid4().hex
        body = (f"--{b}\r\nContent-Disposition: form-data; name=\"img\"; filename=\"img\"\r\n"
                f"Content-Type: application/octet-stream\r\n\r\n").encode() + payload + f"\r\n--{b}--\r\n".encode()
        return self._req(path, body, f"multipart/form-data; boundary={b}")

    def tap(self, x, y):
        return self._req(f"/api/tap?x={x}&y={y}", b"")

    def info(self):
        return json.loads(self._req("/api/info"))


def grab(panel: Panel) -> Image.Image:
    """What the panel shows right now (GET /api/screen, RGB565)."""
    sys.path.insert(0, str(ROOT / "tools"))
    import array
    req = urllib.request.Request(panel.url + "/api/screen", headers={"X-Panda-Token": panel.token})
    with urllib.request.urlopen(req, timeout=30) as r:
        w, h = int(r.headers.get("X-Width", 800)), int(r.headers.get("X-Height", 480))
        rotate = r.headers.get("X-Rotate", "0") == "180"
        raw = r.read()
    px = array.array("H")
    px.frombytes(raw)
    rgb = bytearray(w * h * 3)
    for i, v in enumerate(px):
        r5, g6, b5 = (v >> 11) & 31, (v >> 5) & 63, v & 31
        rgb[3 * i:3 * i + 3] = bytes(((r5 << 3) | (r5 >> 2), (g6 << 2) | (g6 >> 4), (b5 << 3) | (b5 >> 2)))
    img = Image.frombytes("RGB", (w, h), bytes(rgb))
    return img.rotate(180) if rotate else img


def screenshot(panel: Panel, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    grab(panel).save(out)
    print("saved", out)


def tab_centers(img: Image.Image, n: int, bar_h: int = 48, max_x: int = 520) -> list[int]:
    """x centres of the first n tab labels in the top bar: runs of columns with bright text pixels."""
    g = img.convert("L")
    ink = g.getpixel((max_x, 4)) + 30          # labels: clearly brighter than the bar's background
    cols = [any(g.getpixel((x, y)) > ink for y in range(10, bar_h - 10)) for x in range(max_x)]
    runs, start, gap = [], None, 0
    for x, on in enumerate(cols + [False] * 20):
        if on:
            start, gap = (x if start is None else start), 0
        elif start is not None:
            gap += 1
            if gap > 12:                       # letters are spaced ~3 px; tabs ~30 px apart
                runs.append((start, x - gap))
                start, gap = None, 0
    return [(a + b) // 2 for a, b in runs[:n]]


def main() -> None:
    global DEMO_TAG
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://pandadeck.local")
    ap.add_argument("--token", required=True)
    ap.add_argument("--shots", help="folder: capture every page into it")
    ap.add_argument("--seconds", type=float, default=0, help="keep the demo running (0 = until Ctrl+C)")
    args = ap.parse_args()
    panel = Panel(args.url, args.token)

    sys.path.insert(0, str(ROOT / "app"))
    from pandadeck.music.images import (art_image, backdrop_image, encode_rgb565, encode_rgb565a8,  # noqa: E402
                                        to_rgb565a8)

    cover = synthwave_cover()
    art = encode_rgb565a8(art_image(cover))
    DEMO_TAG = hashlib.sha1(art).hexdigest()[:8]
    panel.upload(f"/api/icon?key=art&w=248&h=248&tag={DEMO_TAG}", art)
    panel.upload(f"/api/backdrop?w=200&h=108&tag={DEMO_TAG}", encode_rgb565(backdrop_image(cover)))
    for i, (label, glyph, a, b) in enumerate(APPS):
        panel.upload(f"/api/icon?key=demo{i}&w=56&h=56", to_rgb565a8(app_icon(label, glyph, a, b), 56))
    panel.post_json("/api/config", {"pages": ["ai", "deck", "music", "hud"]})

    t0 = time.time()
    stop = threading.Event()

    def feeder():
        while not stop.is_set():
            try:
                panel.post_json("/api/sync", demo_snapshot(t0))
            except OSError:
                pass
            stop.wait(0.4)

    threading.Thread(target=feeder, daemon=True).start()
    try:
        if args.shots:
            shots = Path(args.shots)
            time.sleep(3)
            xs = tab_centers(grab(panel), len(PAGES))
            if len(xs) < len(PAGES):
                raise SystemExit(f"found {len(xs)} tabs in the top bar, expected {len(PAGES)}")
            for page, x in zip(PAGES, xs):
                panel.tap(x, 24)
                time.sleep(2.5)
                screenshot(panel, shots / f"{page}.png")
        else:
            deadline = time.time() + args.seconds if args.seconds else float("inf")
            while time.time() < deadline:
                time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()


if __name__ == "__main__":
    main()
