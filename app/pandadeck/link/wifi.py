"""Wi-Fi link: the panel's HTTP API on port 80, every request carrying the
panel token in X-Panda-Token (docs/protocol.md "Wi-Fi")."""
from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import logging
import secrets
import socket
import urllib.parse

from ..util import http_request, multipart
from .base import LinkError, Progress, Unsupported

log = logging.getLogger("link.wifi")

MDNS_NAME = "pandadeck.local"
UPLOAD_CHUNK = 16 * 1024


def resolve_mdns(name: str = MDNS_NAME, timeout: float = 2.0) -> str | None:
    """IPv4 address for the panel's mDNS name: python-zeroconf when installed,
    else the OS resolver (macOS, Windows 10+, Linux with nss-mdns)."""
    try:
        from zeroconf import Zeroconf  # optional dependency
        try:
            from zeroconf import AddressResolver  # zeroconf >= 0.131
        except ImportError:  # pragma: no cover
            AddressResolver = None  # noqa: N806
        if AddressResolver is not None:
            zc = Zeroconf()
            try:
                r = AddressResolver(name if name.endswith(".") else name + ".")
                if r.request(zc, int(timeout * 1000)):
                    addrs = r.parsed_addresses()
                    if addrs:
                        return addrs[0]
            finally:
                zc.close()
    except Exception as e:  # noqa: BLE001 - zeroconf missing or no multicast here
        log.debug("zeroconf lookup failed: %s", e)
    old = socket.getdefaulttimeout()
    try:
        socket.setdefaulttimeout(timeout)
        infos = socket.getaddrinfo(name, 80, socket.AF_INET, socket.SOCK_STREAM)
        return infos[0][4][0] if infos else None
    except OSError:
        return None
    finally:
        socket.setdefaulttimeout(old)


def _clean_host(host: str) -> str:
    host = (host or "").strip()
    if "://" in host:
        host = urllib.parse.urlparse(host).netloc
    return host.rstrip("/")


def panel_proof(token: str, nonce: str) -> str:
    """What a genuine panel answers to X-Panda-Nonce: hex HMAC-SHA256(token, nonce)."""
    return hmac.new(token.encode(), nonce.encode(), hashlib.sha256).hexdigest()


class WifiLink:
    """`trusted` is True only when the device proved it knows the panel token
    (nonce challenge in hello, answered without us sending the token). An
    untrusted link never carries approvals (see sync.py). With
    require_proof (automatic addresses: mDNS, last known IP) an unproven
    device is refused before the token is ever sent to it."""

    kind = "wifi"

    def __init__(self, host: str, token: str, timeout: float = 3.0, require_proof: bool = False):
        self.where = _clean_host(host)
        if not self.where:
            raise LinkError("no panel address")
        self.base = f"http://{self.where}"
        self.token = token
        self.timeout = timeout
        self.require_proof = require_proof
        self.trusted = False

    # ---- helpers -------------------------------------------------------------
    def _headers(self, extra: dict | None = None) -> dict:
        return {"X-Panda-Token": self.token, **(extra or {})}

    def _call(self, method: str, path: str, body: bytes | None = None, ctype: str | None = None,
              timeout: float | None = None, send_token: bool = True,
              extra: dict | None = None) -> tuple[int, bytes]:
        hdrs = {**(self._headers() if send_token else {}), **(extra or {})}
        if ctype:
            hdrs["Content-Type"] = ctype
        try:
            status, _, data = http_request(self.base + path, method=method, headers=hdrs, body=body,
                                           timeout=timeout or self.timeout, follow_redirects=False)
        except OSError as e:
            raise LinkError(f"panel unreachable at {self.where}: {e}") from e
        if status == 401:
            raise LinkError("the panel rejected the token (plug it in by USB once to pair)")
        return status, data

    def _json(self, method: str, path: str, obj=None, timeout: float | None = None, **kw) -> dict:
        body = None if obj is None else json.dumps(obj, separators=(",", ":")).encode()
        status, data = self._call(method, path, body, "application/json" if body is not None else None, timeout,
                                  **kw)
        if status == 404:
            raise Unsupported(f"panel firmware has no {path}")
        if status != 200:
            raise LinkError(f"{path}: HTTP {status}")
        try:
            out = json.loads(data or b"{}")
        except ValueError as e:
            raise LinkError(f"{path}: bad JSON") from e
        if not isinstance(out, dict):
            raise LinkError(f"{path}: bad reply")
        return out

    # ---- Link ---------------------------------------------------------------------
    def hello(self) -> dict:
        # The token is NOT sent here: the device must first prove it already knows it.
        nonce = secrets.token_hex(16)
        info = self._json("GET", "/api/info", send_token=False, extra={"X-Panda-Nonce": nonce})
        proof = str(info.pop("proof", "") or "")
        self.trusted = bool(proof) and bool(self.token) and hmac.compare_digest(proof, panel_proof(self.token, nonce))
        if self.require_proof and not self.trusted:
            raise LinkError(f"{self.where} didn't prove it's your panel (update the panel firmware, "
                            "or type its address in Panel > Advanced)")
        return normalize_info(info, self.where)

    def sync(self, snap: dict) -> dict:
        return self._json("POST", "/api/sync", snap)

    def image(self, hdr: dict, data: bytes) -> bool:
        if hdr.get("kind") == "backdrop":
            q = urllib.parse.urlencode({"w": hdr["w"], "h": hdr["h"], "tag": hdr.get("tag", "")})
            path = f"/api/backdrop?{q}"
        else:
            q = urllib.parse.urlencode({"key": hdr["key"], "w": hdr["w"], "h": hdr["h"], "tag": hdr.get("tag", "")})
            path = f"/api/icon?{q}"
        body, ctype = multipart({}, "img", "img", data)
        status, _ = self._call("POST", path, body, ctype, timeout=15.0)
        return status == 200

    def config(self, d: dict) -> dict:
        return self._json("POST", "/api/config", d, timeout=5.0)

    def update(self, firmware: bytes, progress: Progress | None = None) -> None:
        md5 = hashlib.md5(firmware).hexdigest()  # noqa: S324 - ElegantOTA-style integrity field
        body, ctype = multipart({"MD5": md5}, "firmware", "firmware.bin", firmware)
        status, text = upload_with_progress(self.where, "/update", body, ctype, self._headers(), progress)
        if status != 200:
            raise LinkError(f"update failed: HTTP {status} {text[:120]}")

    def close(self) -> None:
        pass


def normalize_info(info: dict, where: str = "") -> dict:
    """/api/info (and older firmware's debug blob) -> the `h` reply shape."""
    out = dict(info)
    out.setdefault("app", "pandadeck")
    wifi = dict(out.get("wifi") or {})
    wifi.setdefault("connected", True)
    wifi.setdefault("ip", info.get("ip") or where)
    out["wifi"] = wifi
    st = dict(out.get("settings") or {})
    for k in ("flip", "tflip"):
        if k in info and k not in st:
            st[k] = bool(info[k])
    if "brightness" not in st and isinstance(info.get("bl"), (int, float)):
        st["brightness"] = info["bl"]
    out["settings"] = st
    out.setdefault("features", ["wifi"])
    return out


def upload_with_progress(host: str, path: str, body: bytes, ctype: str, headers: dict,
                         progress: Progress | None = None, timeout: float = 240.0) -> tuple[int, str]:
    """POST a multipart body in chunks so the UI can show a progress bar."""
    hostname, _, port = host.partition(":")
    conn = http.client.HTTPConnection(hostname, int(port or 80), timeout=timeout)
    try:
        conn.putrequest("POST", path)
        for k, v in {**headers, "Content-Type": ctype, "Content-Length": str(len(body))}.items():
            conn.putheader(k, v)
        conn.endheaders()
        total = len(body)
        for off in range(0, total, UPLOAD_CHUNK):
            conn.send(body[off:off + UPLOAD_CHUNK])
            if progress:
                progress(min(off + UPLOAD_CHUNK, total), total)
        r = conn.getresponse()
        return r.status, r.read(400).decode("utf-8", "replace")
    except OSError as e:
        raise LinkError(f"upload to {host} failed: {e}") from e
    finally:
        conn.close()
