"""Import a sound from a link the user pastes.

    a direct audio link   (audio/* or a .mp3 / .wav / .ogg / .m4a address) -> downloaded
    a web page            -> the first <audio src>, <source src>, og:audio, or quoted
                             "...mp3/.wav/.ogg/.m4a" string (href, onclick, ...) on it,
                             then that file is downloaded

Safety: https only (http only for local testing), every address the link or a
redirect leads to is resolved and must be a public internet address (no
loopback / private / link-local), and the connection goes to exactly the
address that was checked. Downloads are streamed with a 5 MB cap and a 10 s
limit, and the bytes must be real audio (WAV / MP3 / OGG / M4A by their magic).
Nothing is ever run; the file is stored as sounds/<sha1>.<ext>.
"""
from __future__ import annotations

import codecs
import html as htmlmod
import http.client
import ipaddress
import logging
import re
import socket
import ssl
import threading
import time
import urllib.parse
from html.parser import HTMLParser
from typing import Callable

from .store import MAX_BYTES, SoundError, sniff

log = logging.getLogger("sounds.fetch")

TIMEOUT_S = 10.0
MAX_HTML = 2 * 1024 * 1024
MAX_REDIRECTS = 4
MAX_CANDIDATES = 3
AUDIO_EXTS = ("mp3", "wav", "ogg", "m4a")
CHUNK = 64 * 1024
USER_AGENT = "PandaDeck/1.0 (sound import)"

_QUOTED_AUDIO = re.compile(r"""["']([^"'\s<>]{1,1024}?\.(?:mp3|wav|ogg|m4a)(?:\?[^"'\s<>]{0,512})?)["']""", re.IGNORECASE)

NO_SOUND = "That page has no sound file I can find. Copy the direct link of the sound instead."


class FetchError(Exception):
    """Shown to the user as is."""


# ---- pure helpers ------------------------------------------------------------------------------

def check_url(url: str, allow_local: bool = False) -> urllib.parse.SplitResult:
    url = (url or "").strip()
    try:
        u = urllib.parse.urlsplit(url)
    except ValueError:
        raise FetchError("That doesn't look like a link.") from None
    if u.scheme not in ("https", "http") or not u.hostname:
        raise FetchError("Paste a link that starts with https://")
    if u.scheme == "http" and not allow_local:
        raise FetchError("Only https:// links can be used (the site must be secure).")
    if u.username or u.password:
        raise FetchError("Links with a user name or password can't be used.")
    try:
        _ = u.port
    except ValueError:
        raise FetchError("That link has a bad port number.") from None
    return u


def ip_allowed(ip: str, allow_local: bool = False) -> bool:
    """Pure: a public internet address (or, for local testing, loopback)."""
    try:
        addr = ipaddress.ip_address(ip.split("%")[0])
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    if allow_local and addr.is_loopback:
        return True
    return addr.is_global and not addr.is_multicast


def audio_ext_of(url: str) -> str:
    path = urllib.parse.urlsplit(url).path.lower()
    ext = path.rsplit(".", 1)[-1] if "." in path.rsplit("/", 1)[-1] else ""
    return ext if ext in AUDIO_EXTS else ""


class _AudioFinder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.media: list[str] = []     # <audio src>, <source src> (document order)
        self.og: list[str] = []        # og:audio
        self.title = ""
        self.og_title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag in ("audio", "source") and a.get("src"):
            if tag == "audio" or a.get("type", "").startswith("audio/") or audio_ext_of(a["src"]):
                self.media.append(a["src"])
        elif tag == "meta":
            prop = (a.get("property") or a.get("name") or "").lower()
            if prop in ("og:audio", "og:audio:url", "og:audio:secure_url") and a.get("content"):
                self.og.append(a["content"])
            elif prop == "og:title" and a.get("content") and not self.og_title:
                self.og_title = a["content"]
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title and len(self.title) < 300:
            self.title += data


def audio_links(page: str, base: str) -> list[str]:
    """Pure: audio file addresses a page names, best first, made absolute (http(s) only)."""
    finder = _AudioFinder()
    try:
        finder.feed(page)
    except Exception:  # noqa: BLE001 - broken HTML: use what was parsed
        pass
    found = finder.media + finder.og + [htmlmod.unescape(m) for m in _QUOTED_AUDIO.findall(page)]
    out: list[str] = []
    for raw in found:
        url = urllib.parse.urljoin(base, raw.strip())
        if urllib.parse.urlsplit(url).scheme in ("http", "https") and url not in out:
            out.append(url)
    return out


def page_title(page: str) -> str:
    finder = _AudioFinder()
    try:
        finder.feed(page)
    except Exception:  # noqa: BLE001
        pass
    return " ".join((finder.og_title or finder.title).split())


def clean_label(text: str) -> str:
    """Pure: 'Vine Boom Sound Effect - Myinstants' -> 'Vine Boom'."""
    t = " ".join((text or "").split())
    t = re.split(r"\s+[-|–—:]\s+", t)[0] if t else ""
    t = re.sub(r"\s*\b(sound effects?|sound button|soundboard|sfx|meme sound|\(?hd\)?)\s*$", "", t, flags=re.IGNORECASE)
    return t.strip()[:60]


def label_from_url(url: str) -> str:
    name = urllib.parse.unquote(urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1])
    name = name.rsplit(".", 1)[0] if "." in name else name
    return clean_label(re.sub(r"[_\-]+", " ", name))


# ---- network ---------------------------------------------------------------------------------------

class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host, ip, port, timeout):
        super().__init__(host, port, timeout=timeout)
        self._ip = ip

    def connect(self):
        self.sock = socket.create_connection((self._ip, self.port), self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Connects to the address we checked, while TLS still verifies the host name."""

    def __init__(self, host, ip, port, timeout):
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self._ip = ip

    def connect(self):
        sock = socket.create_connection((self._ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def open_pinned(u: urllib.parse.SplitResult, ip: str, timeout: float):
    """GET u from the pinned address -> (status, {header: value} lowercased, response)."""
    port = u.port or (443 if u.scheme == "https" else 80)
    cls = _PinnedHTTPSConnection if u.scheme == "https" else _PinnedHTTPConnection
    conn = cls(u.hostname, ip, port, timeout)
    path = (u.path or "/") + (f"?{u.query}" if u.query else "")
    conn.request("GET", path, headers={"User-Agent": USER_AGENT, "Accept": "*/*", "Accept-Encoding": "identity"})
    resp = conn.getresponse()
    return resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp


class Fetcher:
    def __init__(self, store, opener: Callable = open_pinned, resolve: Callable = socket.getaddrinfo,
                 allow_local: bool = False, clock: Callable[[], float] = time.monotonic,
                 progress: Callable[[str, int, int], None] | None = None):
        self.store, self._open, self._resolve = store, opener, resolve
        self.allow_local, self._clock = allow_local, clock
        self._progress = progress or (lambda stage, done, total: None)
        self.last_page: tuple[str, str] | None = None  # (url, html) of the page a sound came from

    def fetch(self, url: str) -> dict:
        """-> {"file": "<sha1>.<ext>", "label": ..., "kind": ext, "source": final url}."""
        check_url(url, self.allow_local)
        self._progress("page", 0, 0)
        final, ctype, body = self._get(url, MAX_BYTES)
        kind = sniff(body)
        if kind:
            return self._store(body, label_from_url(final), final)
        if "html" not in ctype and not body.lstrip()[:1] == b"<":
            if ctype.startswith("audio/") or audio_ext_of(final):
                raise FetchError("That file isn't a sound PandaDeck can play (WAV, MP3, OGG or M4A).")
            raise FetchError("That link isn't a sound or a page with one. " + NO_SOUND.split(". ", 1)[1])
        page = body[:MAX_HTML].decode(_charset(ctype), "replace")
        self.last_page = (final, page)
        links = audio_links(page, final)
        if not links:
            raise FetchError(NO_SOUND)
        title = clean_label(page_title(page))
        last: Exception | None = None
        for link in links[:MAX_CANDIDATES]:
            try:
                self._progress("download", 0, 0)
                final2, _, data = self._get(link, MAX_BYTES)
                if sniff(data):
                    return self._store(data, title or label_from_url(final2), final2)
                last = FetchError("The sound that page links to isn't WAV, MP3, OGG or M4A.")
            except FetchError as e:
                last = e
        raise last or FetchError(NO_SOUND)

    def get(self, url: str, cap: int = MAX_BYTES) -> tuple[str, str, bytes]:
        """One safe GET (same rules as a sound import) -> (final url, content type, body)."""
        check_url(url, self.allow_local)
        return self._get(url, cap)

    def _store(self, data: bytes, label: str, source: str) -> dict:
        try:
            name = self.store.save_upload(data)
        except SoundError as e:
            raise FetchError(str(e)) from None
        self._progress("done", len(data), len(data))
        return {"file": name, "label": label or "Sound", "kind": name.rsplit(".", 1)[-1], "source": source}

    def _pin(self, u: urllib.parse.SplitResult) -> str:
        port = u.port or (443 if u.scheme == "https" else 80)
        try:
            infos = self._resolve(u.hostname, port, type=socket.SOCK_STREAM)
        except OSError:
            raise FetchError(f"Can't find the site {u.hostname}.") from None
        ips = [info[4][0] for info in infos]
        if not ips:
            raise FetchError(f"Can't find the site {u.hostname}.")
        if not all(ip_allowed(ip, self.allow_local) for ip in ips):
            raise FetchError("That link leads to a private or local network address. PandaDeck only downloads "
                             "sounds from the internet.")
        return ips[0]

    def _get(self, url: str, cap: int) -> tuple[str, str, bytes]:
        """GET with manual, re-checked redirects -> (final url, content type, body)."""
        deadline = self._clock() + TIMEOUT_S
        for _ in range(MAX_REDIRECTS + 1):
            u = check_url(url, self.allow_local)
            ip = self._pin(u)
            left = deadline - self._clock()
            if left <= 0:
                raise FetchError("The site took too long to answer.")
            try:
                status, headers, resp = self._open(u, ip, min(left, TIMEOUT_S))
            except (OSError, http.client.HTTPException) as e:
                raise FetchError(_net_error(e)) from None
            try:
                if status in (301, 302, 303, 307, 308) and headers.get("location"):
                    url = urllib.parse.urljoin(url, headers["location"])
                    continue
                if status != 200:
                    raise FetchError(f"The site answered with an error (HTTP {status}).")
                length = int(headers.get("content-length") or 0) if (headers.get("content-length") or "").isdigit() else 0
                if length > cap:
                    raise FetchError("That sound is bigger than 5 MB.")
                body = self._read(resp, cap, length, deadline)
                return url, (headers.get("content-type") or "").lower(), body
            finally:
                try:
                    resp.close()
                except Exception:  # noqa: BLE001
                    pass
        raise FetchError("That link redirects too many times.")

    def _read(self, resp, cap: int, total: int, deadline: float) -> bytes:
        buf = bytearray()
        while True:
            if self._clock() > deadline:
                raise FetchError("The site took too long to answer.")
            try:
                chunk = resp.read(CHUNK)
            except (OSError, http.client.HTTPException) as e:
                raise FetchError(_net_error(e)) from None
            if not chunk:
                return bytes(buf)
            buf += chunk
            if len(buf) > cap:
                raise FetchError("That sound is bigger than 5 MB.")
            self._progress("download", len(buf), total)


def _charset(ctype: str) -> str:
    m = re.search(r"charset=([\w\-]+)", ctype or "")
    if m:
        try:
            codecs.lookup(m.group(1))
            return m.group(1)
        except LookupError:
            pass
    return "utf-8"


def _net_error(e: Exception) -> str:
    if isinstance(e, (socket.timeout, TimeoutError)):
        return "The site took too long to answer."
    if isinstance(e, ssl.SSLError):
        return "The site's secure connection failed (certificate problem)."
    return f"Couldn't download it: {e}"


# ---- one import at a time, with progress for the settings page --------------------------------------

class SoundImport:
    def __init__(self, store, fetcher_factory: Callable[..., Fetcher] | None = None):
        self.store = store
        self._factory = fetcher_factory or (lambda progress: Fetcher(store, progress=progress))
        self._lock = threading.Lock()
        self._state: dict = {"state": "idle"}
        self.pages: dict[str, tuple[str, str]] = {}  # pasted link -> (page url, html) of recent imports

    def status(self) -> dict:
        with self._lock:
            return dict(self._state)

    def start(self, url: str) -> None:
        with self._lock:
            if self._state.get("state") == "running":
                raise FetchError("Already downloading a sound: wait a moment.")
            self._state = {"state": "running", "stage": "page", "done": 0, "total": 0, "url": url[:300]}
        threading.Thread(target=self._run, args=(url,), name="sound-import", daemon=True).start()

    def _set(self, **kw) -> None:
        with self._lock:
            self._state = {**self._state, **kw}

    def _run(self, url: str) -> None:
        try:
            fetcher = self._factory(lambda stage, done, total: self._set(stage=stage, done=done, total=total))
            out = fetcher.fetch(url)
            page = getattr(fetcher, "last_page", None)
            self.pages[url] = page if page else (out.get("source", url), "")  # for the artwork lookup
            while len(self.pages) > 8:
                self.pages.pop(next(iter(self.pages)))
            self._set(state="done", **out)
        except FetchError as e:
            self._set(state="error", error=str(e))
        except Exception as e:  # noqa: BLE001 - never leave the job "running"
            log.exception("sound import failed")
            self._set(state="error", error=f"Couldn't import that sound: {e}")
