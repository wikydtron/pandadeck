"""Importing a sound from a link: URL / address safety, redirects, size and
time limits, finding the audio on a page (own minimal fixture pages), and
magic-byte checks. No real sites are contacted."""
import http.server
import io
import socket
import struct
import threading
import wave

import pytest

from pandadeck.sounds import fetch as sf
from pandadeck.sounds.store import SoundStore, sniff


def wav_bytes(n=400):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(struct.pack("<h", 0) * n)
    return buf.getvalue()


MP3 = b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\xff\xfb\x90\x00" * 50

PAGE_ONCLICK = b"""<!doctype html><html><head><title>Vine Boom Sound Effect - Example Instants</title></head>
<body><div class="small-button" onclick="play('/media/sounds/vine-boom.mp3', 'loader-1', 'vine-boom-1')"></div>
<a href="/other-page/">more</a></body></html>"""

PAGE_AUDIO = b"""<html><head><meta property="og:title" content="Clang | Sounds">
<meta property="og:audio" content="https://cdn.example.net/og/clang.ogg"></head>
<body><audio controls><source src="clips/clang.mp3" type="audio/mpeg"></audio>
<a href="javascript:alert('x.mp3')">x</a></body></html>"""


# ---- pure helpers ---------------------------------------------------------------------------------

def test_url_and_address_rules():
    assert sf.check_url("https://example.com/a.mp3").hostname == "example.com"
    for bad in ("http://example.com/a.mp3", "ftp://example.com/a.mp3", "file:///etc/passwd", "example.com/a.mp3",
                "https://user:pw@example.com/a.mp3", "https://example.com:99999/x", "javascript:alert(1)", ""):
        with pytest.raises(sf.FetchError):
            sf.check_url(bad)
    assert sf.check_url("http://127.0.0.1:8000/a.mp3", allow_local=True)
    for ip in ("8.8.8.8", "1.1.1.1", "2606:4700:4700::1111"):
        assert sf.ip_allowed(ip), ip
    for ip in ("127.0.0.1", "10.1.2.3", "192.168.1.10", "172.16.0.1", "169.254.169.254", "100.64.0.1", "0.0.0.0",
               "::1", "fe80::1", "fc00::1", "::ffff:10.0.0.1", "224.0.0.1", "not-an-ip"):
        assert not sf.ip_allowed(ip), ip
    assert sf.ip_allowed("127.0.0.1", allow_local=True) and not sf.ip_allowed("10.0.0.1", allow_local=True)


def test_finding_audio_on_pages():
    assert sf.audio_links(PAGE_ONCLICK.decode(), "https://sounds.example.com/instant/vine-boom/") == [
        "https://sounds.example.com/media/sounds/vine-boom.mp3"]
    links = sf.audio_links(PAGE_AUDIO.decode(), "https://site.example.org/clang/")
    assert links[:2] == ["https://site.example.org/clang/clips/clang.mp3",   # <audio>/<source> first
                         "https://cdn.example.net/og/clang.ogg"]             # then og:audio
    assert all(u.startswith("https://") for u in links)                     # never a javascript: address
    page = '<p><a href="https://cdn.example.net/a.wav?x=1&amp;y=2">get</a> <img src="cat.png"></p>'
    assert sf.audio_links(page, "https://e.example/") == ["https://cdn.example.net/a.wav?x=1&y=2"]
    assert sf.audio_links("<html><body>no sound here, just .mp3 talk</body></html>", "https://e.example/") == []
    assert sf.audio_links("<audio src='data:audio/mp3;base64,AAAA'></audio><<<", "https://e.example/") == []


def test_labels_from_titles_and_names():
    assert sf.page_title(PAGE_ONCLICK.decode()) == "Vine Boom Sound Effect - Example Instants"
    assert sf.clean_label(sf.page_title(PAGE_ONCLICK.decode())) == "Vine Boom"
    assert sf.clean_label(sf.page_title(PAGE_AUDIO.decode())) == "Clang"         # og:title wins
    assert sf.label_from_url("https://x.example/s/steel_drum-roll.mp3?v=2") == "steel drum roll"
    assert sf.label_from_url("https://x.example/") == ""


def test_magic_bytes():
    assert sniff(wav_bytes()) == "wav" and sniff(MP3) == "mp3" and sniff(b"\xff\xfb\x90\x00") == "mp3"
    assert sniff(b"OggS\x00\x02" + b"\0" * 20) == "ogg"
    assert sniff(b"\x00\x00\x00\x20ftypM4A \x00\x00\x00\x00") == "m4a"
    for not_audio in (b"<!doctype html>", b"MZ\x90\x00\x03", b"%PDF-1.7", b"\x89PNG\r\n\x1a\n", b"PK\x03\x04", b""):
        assert sniff(not_audio) is None


# ---- the fetcher, with a fake network ----------------------------------------------------------------

class Resp:
    def __init__(self, body, chunk=None):
        self.buf, self.chunk, self.closed = io.BytesIO(body), chunk, False

    def read(self, n):
        return self.buf.read(min(n, self.chunk or n))

    def close(self):
        self.closed = True


class Net:
    """url -> (status, headers, body); host -> ip."""

    def __init__(self, routes, hosts=None):
        self.routes = routes
        self.hosts = {"site.example": "93.184.216.34", "cdn.example": "93.184.216.35", **(hosts or {})}
        self.opened = []

    def resolve(self, host, port, type=0):
        if host not in self.hosts:
            raise socket.gaierror("unknown")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (self.hosts[host], port))]

    def open(self, u, ip, timeout):
        url = u.geturl()
        self.opened.append((url, ip))
        status, headers, body = self.routes[url]
        return status, {k.lower(): v for k, v in headers.items()}, Resp(body)


def fetcher(tmp_path, net, **kw):
    return sf.Fetcher(SoundStore(tmp_path), opener=net.open, resolve=net.resolve, **kw)


def test_direct_audio_link(tmp_path):
    net = Net({"https://cdn.example/sounds/Taco_Bell-bong.mp3": (200, {"Content-Type": "audio/mpeg"}, MP3)})
    out = fetcher(tmp_path, net).fetch("https://cdn.example/sounds/Taco_Bell-bong.mp3")
    assert out["kind"] == "mp3" and out["label"] == "Taco Bell bong" and (tmp_path / out["file"]).read_bytes() == MP3
    assert net.opened == [("https://cdn.example/sounds/Taco_Bell-bong.mp3", "93.184.216.35")]  # pinned to the checked IP


def test_page_with_a_sound(tmp_path):
    net = Net({"https://site.example/instant/vine-boom/": (200, {"Content-Type": "text/html; charset=utf-8"}, PAGE_ONCLICK),
               "https://site.example/media/sounds/vine-boom.mp3": (200, {"Content-Type": "audio/mpeg"}, MP3)})
    steps = []
    f = fetcher(tmp_path, net, progress=lambda stage, done, total: steps.append(stage))
    out = f.fetch("https://site.example/instant/vine-boom/")
    assert out["label"] == "Vine Boom" and out["file"].endswith(".mp3") and out["source"].endswith("vine-boom.mp3")
    assert steps[0] == "page" and "download" in steps and steps[-1] == "done"


def test_page_without_a_sound_explains_what_to_do(tmp_path):
    net = Net({"https://site.example/": (200, {"Content-Type": "text/html"}, b"<html><title>Hi</title><p>nothing</p>")})
    with pytest.raises(sf.FetchError, match="copy the direct link|Copy the direct link"):
        fetcher(tmp_path, net).fetch("https://site.example/")


def test_redirects_are_rechecked(tmp_path):
    net = Net({"https://site.example/go": (302, {"Location": "https://intranet.example/secret.mp3"}, b""),
               "https://site.example/down": (301, {"Location": "http://cdn.example/a.mp3"}, b""),
               "https://site.example/loop": (302, {"Location": "/loop"}, b""),
               "https://site.example/ok": (307, {"Location": "https://cdn.example/a.mp3"}, b""),
               "https://cdn.example/a.mp3": (200, {}, MP3)},
              hosts={"intranet.example": "10.0.0.5", "rebind.example": "127.0.0.1"})
    f = fetcher(tmp_path, net)
    with pytest.raises(sf.FetchError, match="private or local"):
        f.fetch("https://site.example/go")                     # redirect into the LAN: refused before connecting
    assert all(ip != "10.0.0.5" for _, ip in net.opened)
    with pytest.raises(sf.FetchError, match="https"):
        f.fetch("https://site.example/down")                   # no downgrade to http
    with pytest.raises(sf.FetchError, match="too many"):
        f.fetch("https://site.example/loop")
    with pytest.raises(sf.FetchError, match="private or local"):
        f.fetch("https://rebind.example/a.mp3")                # a public name pointing at loopback
    assert f.fetch("https://site.example/ok")["kind"] == "mp3"


def test_size_and_time_limits(tmp_path):
    big = b"ID3" + b"\0" * (sf.MAX_BYTES + 10)
    net = Net({"https://cdn.example/big.mp3": (200, {"Content-Length": str(len(big))}, big),
               "https://cdn.example/sneaky.mp3": (200, {}, big),
               "https://cdn.example/missing.mp3": (404, {}, b"nope"),
               "https://cdn.example/notes.txt": (200, {"Content-Type": "text/plain"}, b"hello"),
               "https://cdn.example/fake.mp3": (200, {"Content-Type": "audio/mpeg"}, b"MZ\x90\x00 not audio")})
    f = fetcher(tmp_path, net)
    for url, msg in (("https://cdn.example/big.mp3", "5 MB"), ("https://cdn.example/sneaky.mp3", "5 MB"),
                     ("https://cdn.example/missing.mp3", "HTTP 404"), ("https://cdn.example/notes.txt", "isn't a sound"),
                     ("https://cdn.example/fake.mp3", "isn't a sound")):
        with pytest.raises(sf.FetchError, match=msg):
            f.fetch(url)
    now = [0.0]

    class SlowResp(Resp):
        def read(self, n):
            now[0] += 4.0                                        # every chunk takes 4 s
            return super().read(1024)

    net.open = lambda u, ip, timeout: (200, {}, SlowResp(MP3 * 50))
    with pytest.raises(sf.FetchError, match="too long"):
        sf.Fetcher(SoundStore(tmp_path), opener=net.open, resolve=net.resolve, clock=lambda: now[0]).fetch(
            "https://cdn.example/slow.mp3")
    assert not list(tmp_path.glob("*.mp3")) or all(p.stat().st_size < sf.MAX_BYTES for p in tmp_path.glob("*"))


# ---- for real, over loopback (allow_local is for testing only) -------------------------------------------

def test_real_download_over_loopback(tmp_path):
    files = {"/page": (b"text/html", b"<html><title>Coin - Test</title><audio src='/coin.wav'></audio></html>"),
             "/coin.wav": (b"audio/wav", wav_bytes())}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            ctype, body = files.get(self.path, (b"text/plain", b"no"))
            self.send_response(200 if self.path in files else 404)
            self.send_header("Content-Type", ctype.decode())
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        out = sf.Fetcher(SoundStore(tmp_path), allow_local=True).fetch(base + "/page")
        assert out["kind"] == "wav" and out["label"] == "Coin" and (tmp_path / out["file"]).exists()
        with pytest.raises(sf.FetchError):
            sf.Fetcher(SoundStore(tmp_path)).fetch(base + "/page")      # the real app never fetches local addresses
    finally:
        srv.shutdown()
        srv.server_close()


def test_import_job_and_api(fake_platform, tmp_path):
    import http.client
    import json

    from pandadeck.service import Service
    from pandadeck.web import api
    from pandadeck.web.server import make_server
    svc = Service(panel=False, platform=fake_platform)
    gate = threading.Event()
    net = Net({"https://cdn.example/bruh.mp3": (200, {}, MP3)})

    class SlowFetcher(sf.Fetcher):
        def fetch(self, url):
            gate.wait(5)
            return super().fetch(url)

    svc.sound_import = sf.SoundImport(SoundStore(tmp_path), lambda progress: SlowFetcher(
        SoundStore(tmp_path), opener=net.open, resolve=net.resolve, progress=progress))
    srv = make_server(svc, 0, api.ROUTES)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def call(method, body=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        c.request(method, "/api/sounds/fetch", body=json.dumps(body).encode() if body is not None else None,
                  headers={"Host": f"127.0.0.1:{port}", "X-PandaDeck": "1", "Content-Type": "application/json"})
        r = c.getresponse()
        out = r.status, json.loads(r.read() or b"null")
        c.close()
        return out

    try:
        assert call("POST", {"url": "http://cdn.example/bruh.mp3"})[0] == 400        # https only
        status, st = call("POST", {"url": "https://cdn.example/bruh.mp3"})
        assert status == 200 and st["state"] == "running"
        assert call("POST", {"url": "https://cdn.example/bruh.mp3"})[0] == 409       # one at a time
        gate.set()
        for _ in range(200):
            st = call("GET")[1]
            if st["state"] != "running":
                break
            threading.Event().wait(0.02)
        assert st["state"] == "done" and st["file"].endswith(".mp3") and st["label"] == "bruh"
    finally:
        srv.shutdown()
        srv.server_close()
