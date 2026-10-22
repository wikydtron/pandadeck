"""Small helpers shared by the platform backends (stdlib + Pillow only).

Nothing here imports an OS-only module, so it loads on every OS.
"""
from __future__ import annotations

import hashlib
import io
import ipaddress
import logging
import math
import os
import socket
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Generic, TypeVar

log = logging.getLogger("pandadeck.platform")

T = TypeVar("T")

NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW
DETACHED = 0x00000008 | 0x00000200 if sys.platform == "win32" else 0  # DETACHED_PROCESS | NEW_PROCESS_GROUP
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGE_PIXELS = 16_000_000
ART_MAX_PX = 640     # covers are scaled down to this before they're handed out
ICON_PX = 256        # icons are requested/scaled to about this size
FETCH_TIMEOUT_S = 4.0
USER_AGENT = "PandaDeck/1"


# ---- processes ---------------------------------------------------------------------------

def run(args: list[str], timeout: float = 5.0, c_locale: bool = False,
        env: dict[str, str] | None = None) -> str | None:
    """Run a program (never through a shell). stdout on exit 0, else None."""
    if c_locale:
        env = {**(env or os.environ), "LC_ALL": "C", "LANG": "C"}
    try:
        proc = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, env=env, stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as e:
        log.debug("%s failed: %s", args[0], e)
        return None
    if proc.returncode != 0:
        log.debug("%s exited %s: %s", args[0], proc.returncode, (proc.stderr or "").strip()[:200])
        return None
    return proc.stdout


def spawn(args: list[str], cwd: str | None = None, env: dict[str, str] | None = None) -> subprocess.Popen:
    """Start a program detached from us (never through a shell). Raises OSError."""
    if not args or not args[0] or not all(isinstance(a, str) and "\0" not in a for a in args):
        raise ValueError(f"bad command: {args!r}")
    kwargs: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
                    "close_fds": True, "cwd": cwd or None, "env": env}
    if sys.platform == "win32":
        kwargs["creationflags"] = DETACHED
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(args, **kwargs)  # noqa: S603 - argv list, no shell


def validate_argv(argv: object) -> list[str]:
    """Autostart command line: a non-empty list of non-empty strings without NULs."""
    ok = isinstance(argv, (list, tuple)) and len(argv) > 0 and all(
        isinstance(a, str) and a and "\x00" not in a for a in argv)
    if not ok:
        raise ValueError("autostart argv must be a non-empty list of strings")
    return list(argv)


def normalize_path(path: object) -> str | None:
    """A user-supplied file/folder path -> normalised absolute path if it exists,
    else None. Accepts ~ and the surrounding quotes of Explorer's "Copy as path"."""
    if not isinstance(path, str) or "\x00" in path:
        return None
    p = path.strip().strip('"').strip()
    if not p:
        return None
    p = os.path.expanduser(p)
    if not os.path.isabs(p):
        return None
    p = os.path.normpath(p)
    try:
        return p if os.path.exists(p) else None
    except (OSError, ValueError):
        return None


# ---- caching -------------------------------------------------------------------------------

class TTLCache(Generic[T]):
    """One value, reloaded when older than `ttl` seconds. A failing loader keeps
    the last good value (or `default` if there never was one) and isn't retried
    for `retry_s`. `force` reloads early, but not more often than every
    `min_force_s` (unknown ids passed to launch_app must not turn into a scan
    per call)."""

    def __init__(self, ttl: float, default: T, min_force_s: float = 10.0, retry_s: float = 30.0):
        self._ttl = ttl
        self._min_force_s = min_force_s
        self._retry_s = retry_s
        self._default = default
        self._value: T | None = None
        self._at = 0.0
        self._failed_at: float | None = None
        self._lock = threading.Lock()

    def peek(self) -> T | None:
        """The cached value without loading or waiting on a running load (lock-free)."""
        return self._value

    def get(self, load: Callable[[], T], force: bool = False) -> T:
        with self._lock:
            now = time.monotonic()
            if self._failed_at is not None and now - self._failed_at < self._retry_s:
                return self._value if self._value is not None else self._default
            age = now - self._at
            if self._value is not None and age < self._ttl and not (force and age >= self._min_force_s):
                return self._value
            try:
                value = load()
            except Exception as e:  # noqa: BLE001 - keep the last good value
                log.warning("refresh failed (%s); keeping the previous result", e)
                self._failed_at = time.monotonic()
                return self._value if self._value is not None else self._default
            self._value, self._at, self._failed_at = value, time.monotonic(), None
            return value


# ---- text / numbers ------------------------------------------------------------------------

def clean(s: object) -> str:
    """Collapse whitespace (titles from players can carry newlines/tabs)."""
    return " ".join(str(s or "").split())


def to_float(s: object, default: float = 0.0) -> float:
    """Number from tool output; accepts a decimal comma (osascript in some locales)."""
    try:
        return float(str(s).strip().replace(",", "."))
    except (TypeError, ValueError):
        return default


def parse_seek(cmd: str) -> float | None:
    """'seek:<seconds>' -> seconds (>= 0), None if not a valid seek command."""
    if not isinstance(cmd, str) or not cmd.startswith("seek:"):
        return None
    try:
        secs = float(cmd[5:])
    except ValueError:
        return None
    if not math.isfinite(secs):
        return None
    return max(0.0, secs)


def art_key(data: bytes | None) -> str:
    return hashlib.sha1(data).hexdigest()[:12] if data else ""  # noqa: S324 - change detection only


# ---- players ---------------------------------------------------------------------------

def choose_player(statuses: dict[str, dict], last_active: str | None) -> str | None:
    """Pure: which of several players to show. statuses = {name: {"status":
    "Playing"|"Paused"|"Stopped", "title": str, ...}}. A playing one wins (the
    last active if it is among them); else the last active if it still has a
    track; else any paused player with a title."""
    playing = [n for n, s in statuses.items() if s.get("status") == "Playing"]
    if playing:
        return last_active if last_active in playing else playing[0]
    last = statuses.get(last_active or "")
    if last is not None and last.get("title") and last.get("status") != "Stopped":
        return last_active
    paused = [n for n, s in statuses.items() if s.get("status") == "Paused" and s.get("title")]
    return paused[0] if paused else None


# ---- images ----------------------------------------------------------------------------------

def _open_checked(src):
    """PIL open with a pixel cap checked before decoding (an 8 MB PNG can be 350 MB of pixels)."""
    from PIL import Image

    img = Image.open(src)
    if img.width * img.height > MAX_IMAGE_PIXELS:
        img.close()
        raise ValueError(f"image too large: {img.width}x{img.height}")
    img.load()
    return img


def image_from_bytes(data: bytes | None, max_px: int = ART_MAX_PX):
    """Decode image bytes -> PIL image (RGB, or RGBA if it has alpha), at most max_px."""
    if not data:
        return None
    from PIL import Image

    try:
        img = _open_checked(io.BytesIO(data))
        has_alpha = "A" in img.getbands() or "transparency" in img.info
        img = img.convert("RGBA" if has_alpha else "RGB")
    except Exception as e:  # noqa: BLE001 - PIL raises many types on bad data
        log.debug("image decode failed: %s", e)
        return None
    if max(img.size) > max_px:
        img.thumbnail((max_px, max_px), Image.LANCZOS)
    return img


def icon_from_file(path: str | Path, px: int = ICON_PX):
    """Load an icon file (png/xpm/ico/icns/jpg...) as RGBA, scaled down to <= px."""
    from PIL import Image

    try:
        img = _open_checked(path)
        try:
            out = img.convert("RGBA")
        finally:
            img.close()
    except Exception as e:  # noqa: BLE001
        log.debug("icon %s unreadable: %s", path, e)
        return None
    if max(out.size) > px:
        out.thumbnail((px, px), Image.LANCZOS)
    return out


class ArtCache:
    """The current cover. Reloaded only when its source changes; a failed load
    (offline, player not ready, a loader that raised) is retried at most every
    `retry_s` seconds so a dead URL can't stall every poll. Thread-safe; callers
    should invoke update() outside their own locks (a fetch can take seconds)."""

    def __init__(self, retry_s: float = 30.0):
        self._retry_s = retry_s
        self._src: str | None = None
        self._retry_at = 0.0
        self._lock = threading.Lock()
        self.image = None
        self.key = ""

    def update(self, src: str, load: Callable[[], bytes | None]) -> None:
        with self._lock:
            now = time.monotonic()
            if src == self._src and (self.image is not None or not src or now < self._retry_at):
                return
            self._src = src
            data = None
            if src:
                try:
                    data = load()
                except Exception as e:  # noqa: BLE001 - never let a cover break now_playing
                    log.debug("cover load failed: %s", e)
            img = image_from_bytes(data)
            self.image, self.key = (img, art_key(data)) if img is not None else (None, "")
            self._retry_at = now + self._retry_s if src and img is None else 0.0


# ---- URLs ------------------------------------------------------------------------------------

def read_limited(path: str | Path, limit: int = MAX_IMAGE_BYTES) -> bytes | None:
    """A regular file's bytes if it's at most `limit` (FIFOs/devices are refused)."""
    try:
        if not stat.S_ISREG(os.stat(path).st_mode):
            return None
        with open(path, "rb") as f:
            data = f.read(limit + 1)
    except (OSError, ValueError) as e:  # ValueError: NUL in the path
        log.debug("read %s failed: %s", path, e)
        return None
    return data if len(data) <= limit else None


def file_url_path(url: str) -> str | None:
    p = urllib.parse.urlparse(url)
    if p.scheme != "file" or p.netloc not in ("", "localhost"):
        return None
    return urllib.parse.unquote(p.path)


def public_http_url(url: str) -> bool:
    """http(s) URL whose host resolves only to public addresses (no loopback,
    LAN, link-local...): art URLs come from players and web pages, so they must
    not be able to make us probe the local network."""
    try:
        p = urllib.parse.urlparse(url)
        if p.scheme not in ("http", "https") or not p.hostname:
            return False
        infos = socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == "https" else 80),
                                   type=socket.SOCK_STREAM)
    except (OSError, ValueError, UnicodeError):
        return False
    addrs = {info[4][0].split("%")[0] for info in infos}
    try:
        return bool(addrs) and all(ipaddress.ip_address(a).is_global for a in addrs)
    except ValueError:
        return False


class _CheckedRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not public_http_url(newurl):
            raise urllib.error.HTTPError(newurl, code, "redirect to a non-public URL refused", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_url(url: str, timeout: float = FETCH_TIMEOUT_S, limit: int = MAX_IMAGE_BYTES) -> bytes | None:
    """GET a public http(s) URL with a size cap and an overall deadline. Anything
    else (file:, ftp:, data:, private hosts) -> None. Never raises."""
    if not public_http_url(url):
        log.debug("refusing to fetch %s", url)
        return None
    deadline = time.monotonic() + 2 * timeout
    opener = urllib.request.build_opener(_CheckedRedirects)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    chunks: list[bytes] = []
    size = 0
    try:
        with opener.open(req, timeout=timeout) as r:
            while size <= limit:
                if time.monotonic() > deadline:
                    raise TimeoutError("download too slow")
                chunk = r.read(64 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
    except Exception as e:  # noqa: BLE001 - http.client raises non-OSError types too
        log.debug("fetch %s failed: %s", url, e)
        return None
    return b"".join(chunks) if size <= limit else None


def load_art_url(url: str) -> bytes | None:
    """Cover bytes from an art URL as players report it (file:// or http(s)://)."""
    if not url:
        return None
    path = file_url_path(url)
    if path is not None:
        return read_limited(path)
    return fetch_url(url)


# ---- files -------------------------------------------------------------------------------------

def write_atomic(path: Path, data: bytes, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    with open(tmp, "wb") as f:
        f.write(data)
    try:
        os.chmod(tmp, mode)
    except OSError:  # pragma: no cover - e.g. odd filesystems
        pass
    os.replace(tmp, path)
