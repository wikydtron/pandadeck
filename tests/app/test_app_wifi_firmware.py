"""Wi-Fi link against a fake panel HTTP server; firmware checks, release lookup,
stock-panel install and USB->Wi-Fi update fallback."""
import email.parser
import email.policy
import hashlib
import json
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from pandadeck import firmware as fwmod
from pandadeck.link.base import LinkError, Unsupported, check_firmware
from pandadeck.link.wifi import WifiLink, normalize_info

TOKEN = "panel-token-1"


def parse_multipart(ctype: str, body: bytes) -> dict:
    msg = email.parser.BytesParser(policy=email.policy.default).parsebytes(
        f"Content-Type: {ctype}\r\n\r\n".encode() + body)
    out = {}
    for part in msg.iter_parts():
        out[part.get_param("name", header="content-disposition")] = part.get_payload(decode=True)
    return out


class FakePanel:
    def __init__(self, has_config=True, needs_token=True):
        self.has_config, self.needs_token = has_config, needs_token
        self.uploads = []
        self.syncs = []
        self.configs = []
        panel = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj=None, text=None):
                body = text.encode() if text is not None else json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _authed(self):
                return not panel.needs_token or self.headers.get("X-Panda-Token") == TOKEN

            def do_GET(self):
                if self.path == "/api/info":
                    self._send(200, {"fw": "0.5.0", "ip": "10.0.0.9", "bl": 70, "flip": True, "tflip": False})
                elif self.path == "/update":
                    self._send(200, text="<form>ElegantOTA</form>")
                else:
                    self._send(404, {})

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n)
                if not self._authed():
                    return self._send(401, {"ok": False})
                path, _, query = self.path.partition("?")
                if path == "/api/sync":
                    panel.syncs.append(json.loads(body))
                    return self._send(200, {"ok": True, "boot": 3, "icons": 1, "events": []})
                if path in ("/api/icon", "/api/backdrop", "/update"):
                    panel.uploads.append((path, query, parse_multipart(self.headers["Content-Type"], body)))
                    return self._send(200, text="ok")
                if path == "/api/config" and panel.has_config:
                    panel.configs.append(json.loads(body))
                    return self._send(200, {"ok": True, "settings": json.loads(body)})
                return self._send(404, {})

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.host = f"127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture
def panel():
    p = FakePanel()
    yield p
    p.close()


def esp_image(size=4096, project=b"pio-proj", version=b"1.0.0", marker=b"PandaDeck") -> bytes:
    data = bytearray(size)
    data[0] = 0xE9
    off = 32
    struct.pack_into("<I", data, off, 0xABCD5432)
    data[off + 16:off + 16 + len(version)] = version
    data[off + 48:off + 48 + len(project)] = project
    data[off + 112:off + 112 + 6] = b"v5.5.1"
    if marker:
        data[1000:1000 + len(marker)] = marker
    return bytes(data)


# ---- Wi-Fi link -------------------------------------------------------------------------

def test_hello_normalizes_old_info(panel):
    info = WifiLink(panel.host, TOKEN).hello()
    assert info["app"] == "pandadeck" and info["fw"] == "0.5.0"
    assert info["wifi"]["ip"] == "10.0.0.9" and info["settings"] == {"flip": True, "tflip": False, "brightness": 70}


def test_sync_image_config_update(panel):
    link = WifiLink("http://" + panel.host + "/", TOKEN)  # URL form is accepted too
    assert link.sync({"t": 1})["boot"] == 3 and panel.syncs == [{"t": 1}]
    assert link.image({"kind": "icon", "key": "a1", "w": 56, "h": 56, "tag": "t9"}, b"\1\2\3") is True
    assert link.image({"kind": "backdrop", "w": 200, "h": 108, "tag": "t9"}, b"\4\5") is True
    (p1, q1, f1), (p2, q2, f2) = panel.uploads
    assert p1 == "/api/icon" and "key=a1" in q1 and "w=56" in q1 and "tag=t9" in q1 and f1["img"] == b"\1\2\3"
    assert p2 == "/api/backdrop" and "w=200" in q2 and f2["img"] == b"\4\5"
    assert link.config({"brightness": 50})["settings"] == {"brightness": 50}
    fw = esp_image()
    seen = []
    link.update(fw, lambda d, t: seen.append(d))
    path, _, fields = panel.uploads[-1]
    assert path == "/update" and fields["firmware"] == fw
    assert fields["MD5"].decode() == hashlib.md5(fw).hexdigest()
    assert seen[-1] >= len(fw)


def test_bad_token_and_missing_endpoint():
    p = FakePanel(has_config=False)
    try:
        with pytest.raises(LinkError, match="token"):
            WifiLink(p.host, "wrong").sync({})
        with pytest.raises(Unsupported):
            WifiLink(p.host, TOKEN).config({"pages": ["deck"]})
    finally:
        p.close()


def test_unreachable_panel_raises_link_error():
    with pytest.raises(LinkError):
        WifiLink("127.0.0.1:1", TOKEN, timeout=0.5).hello()


def test_normalize_info_keeps_new_format():
    info = {"app": "pandadeck", "fw": "1.0.0", "wifi": {"connected": False, "ip": ""},
            "settings": {"pages": ["deck"], "brightness": 90}}
    out = normalize_info(info)
    assert out["wifi"]["connected"] is False and out["settings"]["brightness"] == 90


# ---- firmware helpers ----------------------------------------------------------------------

def test_check_firmware_reads_app_description():
    info = check_firmware(esp_image())
    assert info["project"] == "pio-proj" and info["version"] == "1.0.0" and info["pandadeck"] and not info["stock"]
    stock = check_firmware(esp_image(project=b"knomi_p1", marker=b""))
    assert stock["stock"] and not stock["pandadeck"]
    with pytest.raises(ValueError, match="0xE9"):
        check_firmware(b"\x00" * 1000)
    with pytest.raises(ValueError):
        check_firmware(b"\xe9" * 10)
    with pytest.raises(ValueError, match="too big"):
        check_firmware(b"\xe9" + b"\0" * (9 * 1024 * 1024))


def test_versions():
    assert fwmod.is_newer("1.2.0", "1.1.9") and fwmod.is_newer("v1.10.0", "1.9.0")
    assert not fwmod.is_newer("1.0.0", "1.0.0") and not fwmod.is_newer("0.9", "1.0.0")
    assert fwmod.is_newer("1.0.0", "") and not fwmod.is_newer("", "1.0.0")
    assert fwmod.version_tuple("0.5.0-dirty") == (0, 5, 0)


def test_latest_release_picks_the_firmware_asset():
    def get(url, **kw):
        assert url == fwmod.RELEASES_API
        return 200, {"tag_name": "v1.2.0", "html_url": "https://x/rel", "assets": [
            {"name": "pandadeck-app-windows.zip", "browser_download_url": "https://x/a.zip"},
            {"name": "SHA256SUMS.txt", "browser_download_url": "https://x/sums"},
            {"name": "pandadeck-firmware.bin", "browser_download_url": "https://x/fw.bin", "size": 1234}]}
    rel = fwmod.latest_release(get=get)
    assert rel == {"version": "1.2.0", "url": "https://x/fw.bin", "size": 1234, "page": "https://x/rel", "name": "",
                   "sums_url": "https://x/sums"}
    with pytest.raises(LinkError):
        fwmod.latest_release(get=lambda url, **kw: (200, {"tag_name": "v1", "assets": []}))
    with pytest.raises(LinkError):
        fwmod.latest_release(get=lambda url, **kw: (403, None))


GH = "https://github.com/o/r/releases/download/v1/"


def test_download_only_from_github_releases():
    for bad in ("http://github.com/fw.bin", "https://example.com/fw.bin", "https://github.com.evil.io/fw.bin"):
        with pytest.raises(LinkError):
            fwmod.download(bad, request=lambda url, **kw: (200, {}, b"x"))
    assert fwmod.download(GH + "fw.bin", request=lambda url, **kw: (200, {}, b"\xe9abc")) == b"\xe9abc"
    with pytest.raises(LinkError):
        fwmod.download(GH + "fw.bin", request=lambda url, **kw: (404, {}, b""))


def test_download_release_checks_the_published_checksum():
    fw = esp_image()
    good = hashlib.sha256(fw).hexdigest()
    files = {GH + "fw.bin": fw, GH + "SHA256SUMS.txt": f"{good}  pandadeck-firmware.bin\n0000  other\n".encode()}

    def req(url, **kw):
        return 200, {}, files[url]

    rel = {"url": GH + "fw.bin", "sums_url": GH + "SHA256SUMS.txt"}
    assert fwmod.download_release(rel, request=req) == fw
    files[GH + "SHA256SUMS.txt"] = ("f" * 64 + " *pandadeck-firmware.bin").encode()
    with pytest.raises(LinkError, match="checksum"):
        fwmod.download_release(rel, request=req)
    assert fwmod.download_release({"url": GH + "fw.bin"}, request=req) == fw  # no sums published: still validated
    assert fwmod.expected_sha256("nonsense") == ""


@pytest.mark.parametrize("raw,ok", [("10.0.0.57", "10.0.0.57"), ("http://10.0.0.57/update", "10.0.0.57"),
                                    (" pandadeck.local ", "pandadeck.local"), ("10.0.0.2:8080", "10.0.0.2:8080")])
def test_clean_host(raw, ok):
    assert fwmod.clean_host(raw) == ok


@pytest.mark.parametrize("raw", ["", "a b", "x;rm -rf", "1.2.3.4:99x", "-host"])
def test_clean_host_rejects(raw):
    with pytest.raises(ValueError):
        fwmod.clean_host(raw)


def test_install_on_stock_uploads_like_elegant_ota(panel):
    panel.needs_token = False
    assert fwmod.stock_updater_present(panel.host)
    job = fwmod.FirmwareJob()
    fw = esp_image(8192)
    fwmod.install_on_stock(job, panel.host, fw)
    _, _, fields = panel.uploads[-1]
    assert fields["firmware"] == fw and fields["MD5"].decode() == hashlib.md5(fw).hexdigest()
    st = job.status()
    assert st["state"] == "done" and "USB-C" in st["message"]
    assert not fwmod.stock_updater_present("127.0.0.1:1")


class _Links:
    def __init__(self, link):
        self.link, self.busy, self.reconnected = link, "", False

    def current(self):
        return self.link

    def reconnect(self):
        self.reconnected = True


class _UsbUpdates:
    kind = "usb"

    def __init__(self, unsupported=False):
        self.unsupported, self.got = unsupported, None

    def update(self, data, progress):
        if self.unsupported:
            raise Unsupported("old firmware")
        progress(len(data), len(data))
        self.got = data


def test_update_panel_prefers_usb(fake_secrets, fake_state):
    link = _UsbUpdates()
    links = _Links(link)
    job = fwmod.FirmwareJob()
    fw = esp_image()
    fwmod.update_panel(job, links, fake_secrets, fake_state, fw)
    assert link.got == fw and job.status()["state"] == "done" and links.reconnected and links.busy == ""


def test_update_panel_falls_back_to_wifi(panel, fake_secrets, fake_state):
    links = _Links(_UsbUpdates(unsupported=True))
    fake_secrets.set("panel_token", TOKEN)
    fake_state.set("last_ip", panel.host)
    job = fwmod.FirmwareJob()
    fw = esp_image()
    fwmod.update_panel(job, links, fake_secrets, fake_state, fw)
    assert panel.uploads[-1][0] == "/update" and panel.uploads[-1][2]["firmware"] == fw


def test_update_panel_without_wifi_explains(fake_secrets, fake_state):
    with pytest.raises(LinkError, match="Wi-Fi"):
        fwmod.update_panel(fwmod.FirmwareJob(), _Links(_UsbUpdates(unsupported=True)), fake_secrets, fake_state,
                           esp_image())


def test_firmware_job_runs_one_at_a_time():
    job = fwmod.FirmwareJob()
    gate = threading.Event()
    assert job.start("panel", lambda: gate.wait(2))
    assert not job.start("panel", lambda: None)
    gate.set()
    for _ in range(100):
        if not job.busy():
            break
        threading.Event().wait(0.02)
    failing = fwmod.FirmwareJob()
    failing.start("install", lambda: (_ for _ in ()).throw(LinkError("boom")))
    for _ in range(100):
        if failing.status()["state"] == "error":
            break
        threading.Event().wait(0.02)
    assert failing.status()["message"] == "boom"


# ---- Wi-Fi trust: the panel proves it knows the token before we send it ----------------------

class ProvingPanel(FakePanel):
    def __init__(self, token, nonce_seen):
        super().__init__()
        self.secret, self.nonce_seen = token, nonce_seen
        handler = self.srv.RequestHandlerClass
        orig = handler.do_GET
        panel = self

        def do_GET(h):  # noqa: N802
            if h.path == "/api/info":
                nonce = h.headers.get("X-Panda-Nonce", "")
                panel.nonce_seen.append((nonce, h.headers.get("X-Panda-Token")))
                from pandadeck.link.wifi import panel_proof
                body = json.dumps({"app": "pandadeck", "fw": "1.0.0",
                                   "proof": panel_proof(panel.secret, nonce) if panel.secret else ""}).encode()
                h.send_response(200)
                h.send_header("Content-Length", str(len(body)))
                h.end_headers()
                h.wfile.write(body)
                return
            return orig(h)

        handler.do_GET = do_GET


def test_genuine_panel_is_trusted_and_never_sees_the_token_in_hello():
    seen = []
    p = ProvingPanel(TOKEN, seen)
    try:
        link = WifiLink(p.host, TOKEN, require_proof=True)
        info = link.hello()
        assert link.trusted and "proof" not in info
        assert seen[0][1] is None and len(seen[0][0]) == 32  # nonce sent, token not
    finally:
        p.close()


def test_impostor_is_refused_before_getting_the_token():
    seen = []
    p = ProvingPanel("", seen)  # doesn't know the token
    try:
        with pytest.raises(LinkError, match="prove"):
            WifiLink(p.host, TOKEN, require_proof=True).hello()
        link = WifiLink(p.host, TOKEN)  # typed address, old firmware: allowed but untrusted
        link.hello()
        assert not link.trusted
        assert all(tok is None for _, tok in seen)
    finally:
        p.close()
