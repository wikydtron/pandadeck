"""Website icons for "Add website" buttons: https://<host>/favicon.ico, else
the page's <link rel="icon">. https only, size/time-limited, decoded under
a pixel budget, cached per host in the cache folder for a week."""
from __future__ import annotations

import hashlib
import io
import json
import logging
import re
import time
import urllib.parse
from html.parser import HTMLParser
from pathlib import Path

from ..util import http_request

log = logging.getLogger("favicon")

MAX_BYTES = 512 * 1024
MAX_HTML = 256 * 1024
MAX_PIXELS = 1024 * 1024
TIMEOUT_S = 5
TTL_S = 7 * 24 * 3600
CACHE_VERSION = 2          # bump when the stored icon changes shape (v2: dark logos get a plate)
PLATE = (240, 238, 230, 255)  # the page's light text colour
DARK_LUMA = 64             # mean brightness of a logo's visible pixels below which it needs the plate
OWN_BACKGROUND = 0.9       # an icon this opaque brings its own background


class _IconLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links: list[tuple[int, str]] = []

    def handle_starttag(self, tag, attrs):
        if tag != "link":
            return
        a = {k.lower(): (v or "") for k, v in attrs}
        rel = a.get("rel", "").lower().split()
        if "icon" in rel or "apple-touch-icon" in rel:
            size = 0
            m = re.search(r"(\d+)x\d+", a.get("sizes", ""))
            if m:
                size = int(m.group(1))
            if "apple-touch-icon" in rel:
                size = max(size, 180)
            self.links.append((size, a.get("href", "")))


def icon_links(html: str, base: str) -> list[str]:
    """Pure: <link rel=icon> hrefs from a page, biggest first, absolute https only."""
    p = _IconLinks()
    try:
        p.feed(html)
    except Exception:  # noqa: BLE001 - malformed HTML
        pass
    out = []
    for _, href in sorted(p.links, key=lambda x: -x[0]):
        url = urllib.parse.urljoin(base, href.strip())
        if url.startswith("https://") and url not in out:
            out.append(url)
    return out


def decode(data: bytes):
    from PIL import Image
    if not data or len(data) > MAX_BYTES:
        return None
    try:
        img = Image.open(io.BytesIO(data))
        if img.format not in ("ICO", "PNG", "JPEG", "WEBP", "GIF", "BMP"):
            return None
        if img.size[0] * img.size[1] > MAX_PIXELS:
            return None
        if img.format == "ICO" and hasattr(img, "ico"):
            img.size = max(img.ico.sizes())
        img.load()
        return img.convert("RGBA")
    except Exception:  # noqa: BLE001
        return None


def readable(img):
    """A dark logo on a transparent background (GitHub, X, Apple...) vanishes on the
    panel's dark tiles: put such an icon on a light rounded plate. Icons with their
    own background, or light ones, are returned as they are."""
    from PIL import Image, ImageDraw, ImageStat
    rgba = img.convert("RGBA")
    alpha = rgba.getchannel("A")
    opaque = sum(alpha.histogram()[128:])
    if not opaque or opaque > rgba.size[0] * rgba.size[1] * OWN_BACKGROUND:
        return rgba
    mask = alpha.point(lambda a: 255 if a >= 128 else 0)
    if ImageStat.Stat(rgba.convert("L"), mask).mean[0] >= DARK_LUMA:
        return rgba
    side = max(rgba.size)
    size = side + 2 * (side // 6)
    plate = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(plate).rounded_rectangle((0, 0, size - 1, size - 1), radius=size // 5, fill=PLATE)
    plate.alpha_composite(rgba, ((size - rgba.size[0]) // 2, (size - rgba.size[1]) // 2))
    return plate


def fetch(url: str, cache_dir: Path | None = None, request=http_request, clock=time.time):
    """Website URL -> RGBA icon or None."""
    u = urllib.parse.urlsplit(url)
    host = (u.hostname or "").lower()
    if not host:
        return None
    key = hashlib.sha1(f"{CACHE_VERSION}:{host}".encode()).hexdigest()[:20]
    cache = Path(cache_dir) / "favicons" if cache_dir else None
    if cache:
        png, meta = cache / f"{key}.png", cache / f"{key}.json"
        try:
            info = json.loads(meta.read_text(encoding="utf-8"))
            if clock() - float(info.get("t", 0)) < TTL_S:
                return decode(png.read_bytes()) if info.get("found") else None
        except (OSError, ValueError):
            pass
    img = None
    base = f"https://{host}/"
    try:
        # The page's own big icons first (apple-touch-icon & co. are 180 px and usually
        # sit on a background, so they read on the dark panel), then /favicon.ico.
        try:
            status, _, html = request(base, timeout=TIMEOUT_S, headers={"User-Agent": "pandadeck"},
                                      max_bytes=4 * MAX_HTML)
        except OSError:
            status, html = 0, b""
        if status == 200 and html:
            for link in icon_links(html[:MAX_HTML].decode("utf-8", "replace"), base)[:3]:
                try:
                    status, _, data = request(link, timeout=TIMEOUT_S, headers={"User-Agent": "pandadeck"},
                                              max_bytes=MAX_BYTES)
                except OSError:
                    continue
                img = decode(data) if status == 200 else None
                if img is not None:
                    break
        if img is None:
            status, _, data = request(base + "favicon.ico", timeout=TIMEOUT_S, headers={"User-Agent": "pandadeck"},
                                      max_bytes=MAX_BYTES)
            img = decode(data) if status == 200 else None
    except OSError as e:
        log.debug("favicon for %s failed: %s", host, e)
    if img is not None:
        img = readable(img)
    if cache:
        try:
            cache.mkdir(parents=True, exist_ok=True)
            if img is not None:
                buf = io.BytesIO()
                img.save(buf, "PNG")
                (cache / f"{key}.png").write_bytes(buf.getvalue())
            (cache / f"{key}.json").write_text(json.dumps({"found": img is not None, "t": clock()}), encoding="utf-8")
        except OSError:
            pass
    return img
