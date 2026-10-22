"""Artist photos for the MUSIC backdrop without signing in to anything:
Deezer's public search API (no key) -> `picture_xl`.

    https://api.deezer.com/search/artist?q=<artist>&limit=5  ->  the exact-name match's picture_xl

Only https URLs on Deezer's own image CDN (*.dzcdn.net) are followed; the
download is size- and time-limited and decoded under a pixel budget. Results
(including "not found") are cached per artist in memory and in the cache
folder for 24 h. Lookups run on a background thread: now_playing() never waits.
"""
from __future__ import annotations

import hashlib
import io
import json
import unicodedata
import logging
import re
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from ..util import http_request

log = logging.getLogger("artist")

SEARCH_URL = "https://api.deezer.com/search/artist"
IMAGE_HOST_SUFFIX = ".dzcdn.net"
TTL_S = 24 * 3600
MAX_IMAGE_BYTES = 3 * 1024 * 1024
MAX_PIXELS = 2048 * 2048
TIMEOUT_S = 8
MEMORY_ITEMS = 48
_SPLIT = re.compile(r"\s*(?:,|;|/| feat\.? | ft\.? | featuring | with | x )\s*", re.IGNORECASE)


def primary_artist(artist: str) -> str:
    """"A, B" / "A feat. B" / "A ft. B" -> "A" (pure)."""
    name = (artist or "").strip()
    return _SPLIT.split(name, maxsplit=1)[0].strip() if name else ""


def image_url_ok(url: str) -> bool:
    u = urllib.parse.urlsplit(url or "")
    host = (u.hostname or "").lower()
    return u.scheme == "https" and (host.endswith(IMAGE_HOST_SUFFIX) or host == IMAGE_HOST_SUFFIX.lstrip("."))


def _norm_name(s: str) -> str:
    """'The Paper Satellites' == 'paper satellites'; accents/punctuation ignored."""
    folded = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    folded = re.sub(r"[^a-z0-9]+", " ", folded).strip()
    return folded[4:] if folded.startswith("the ") else folded


EMPTY_MD5 = "d41d8cd98f00b204e9800998ecf8427e"  # md5(b"") -- Deezer's other "no photo" id


def is_placeholder(url: str) -> bool:
    """Deezer's stand-in images for artists without a photo: an empty image id
    (.../images/artist//1000x1000...) or the md5 of nothing."""
    u = url or ""
    return "/images/artist//" in u or f"/images/artist/{EMPTY_MD5}/" in u


def pick_picture(search_json, artist: str = "") -> str:
    """Deezer search JSON -> the photo URL of the artist named `artist`, or "" (pure).

    Only an exact (normalized) name match counts — search ranking can put a
    different artist first ("Lumen Coast" -> "Lumen Craft") — and Deezer's
    no-photo placeholder counts as no photo (the blurred cover is used instead).
    Without `artist`, the first result is taken."""
    data = (search_json or {}).get("data") if isinstance(search_json, dict) else None
    if not data or not isinstance(data, list):
        return ""
    candidates = [d for d in data if isinstance(d, dict)]
    if artist:
        want = _norm_name(artist)
        candidates = [d for d in candidates if _norm_name(str(d.get("name", ""))) == want]
        # several artists can share a name (a near-empty namesake with no photo can be
        # listed above the real one): the most-followed one is the one people mean
        candidates.sort(key=lambda d: -int(d.get("nb_fan") or 0))
    for entry in candidates[:1]:
        for key in ("picture_xl", "picture_big", "picture_medium"):
            url = entry.get(key) or ""
            if image_url_ok(url) and not is_placeholder(url):
                return url
    return ""


def decode_image(data: bytes):
    """Bytes -> RGB PIL image, refusing oversized or non-image data."""
    from PIL import Image
    if not data or len(data) > MAX_IMAGE_BYTES:
        return None
    try:
        img = Image.open(io.BytesIO(data))
        if img.format not in ("JPEG", "PNG", "WEBP") or img.size[0] * img.size[1] > MAX_PIXELS:
            return None
        img.load()
        return img.convert("RGB")
    except Exception:  # noqa: BLE001 - not an image / truncated
        return None


class ArtistImages:
    """get(artist) -> PIL image | None, never blocking: a miss starts a
    background lookup and the next call (a second later) finds the photo."""

    def __init__(self, cache_dir: Path | None = None, request=http_request, clock: Callable[[], float] = time.time,
                 on_ready: Callable[[], None] | None = None, workers: ThreadPoolExecutor | None = None):
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self._request, self._clock = request, clock
        self.on_ready = on_ready
        self._lock = threading.Lock()
        self._mem: dict[str, tuple[float, object]] = {}  # key -> (time, image or None)
        self._busy: set[str] = set()
        self._pool = workers or ThreadPoolExecutor(max_workers=1, thread_name_prefix="artist")

    @staticmethod
    def key(artist: str) -> str:
        return hashlib.sha1(primary_artist(artist).lower().encode("utf-8")).hexdigest()[:20]

    def get(self, artist: str):
        name = primary_artist(artist)
        if not name:
            return None
        k = self.key(artist)
        now = self._clock()
        with self._lock:
            hit = self._mem.get(k)
            if hit and now - hit[0] < TTL_S:
                return hit[1]
            if k in self._busy:
                return None
            self._busy.add(k)
        self._pool.submit(self._resolve, k, name)
        return None

    def get_blocking(self, artist: str):
        """For tests and tools: resolve now."""
        name = primary_artist(artist)
        if not name:
            return None
        k = self.key(artist)
        with self._lock:
            self._busy.add(k)
        self._resolve(k, name)
        with self._lock:
            return (self._mem.get(k) or (0, None))[1]

    # ---- worker -----------------------------------------------------------------
    def _resolve(self, k: str, name: str) -> None:
        img = None
        try:
            img = self._from_disk(k)
            if img is None and not self._known_missing(k):
                img = self._from_deezer(k, name)
        except Exception as e:  # noqa: BLE001 - network/format trouble: just no photo
            log.debug("artist photo for %r failed: %s", name, e)
        with self._lock:
            self._busy.discard(k)
            self._mem[k] = (self._clock(), img)
            while len(self._mem) > MEMORY_ITEMS:
                self._mem.pop(next(iter(self._mem)))
        if img is not None and self.on_ready:
            try:
                self.on_ready()
            except Exception:  # noqa: BLE001
                pass

    def _paths(self, k: str) -> tuple[Path, Path] | None:
        if not self.cache_dir:
            return None
        d = self.cache_dir / "artists3"  # v3: most-followed exact match, both placeholder forms
        return d / f"{k}.jpg", d / f"{k}.json"

    def _from_disk(self, k: str):
        paths = self._paths(k)
        if not paths or not paths[0].exists():
            return None
        if self._clock() - paths[0].stat().st_mtime > TTL_S:
            return None
        return decode_image(paths[0].read_bytes())

    def _known_missing(self, k: str) -> bool:
        paths = self._paths(k)
        if not paths or not paths[1].exists():
            return False
        try:
            meta = json.loads(paths[1].read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        return not meta.get("found") and self._clock() - float(meta.get("t", 0)) < TTL_S

    def _from_deezer(self, k: str, name: str):
        q = urllib.parse.urlencode({"q": name, "limit": 5})
        status, _, body = self._request(f"{SEARCH_URL}?{q}", timeout=TIMEOUT_S, follow_redirects=False,
                                        headers={"Accept": "application/json", "User-Agent": "pandadeck"},
                                        max_bytes=256 * 1024)
        url = pick_picture(json.loads(body or b"{}"), name) if status == 200 else ""
        img, data = None, b""
        if url:
            status, _, data = self._request(url, timeout=TIMEOUT_S, follow_redirects=False,
                                            headers={"User-Agent": "pandadeck"}, max_bytes=MAX_IMAGE_BYTES)
            img = decode_image(data) if status == 200 else None
        self._save(k, img, data)
        log.info("artist photo for %r: %s", name, "found" if img is not None else "none")
        return img

    def _save(self, k: str, img, data: bytes) -> None:
        paths = self._paths(k)
        if not paths:
            return
        try:
            paths[0].parent.mkdir(parents=True, exist_ok=True)
            if img is not None:
                buf = io.BytesIO()
                img.save(buf, "JPEG", quality=88)
                paths[0].write_bytes(buf.getvalue())
            paths[1].write_text(json.dumps({"found": img is not None, "t": self._clock()}), encoding="utf-8")
        except OSError as e:
            log.debug("artist cache write failed: %s", e)
