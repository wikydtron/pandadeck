"""Panel firmware: check for releases, update a PandaDeck panel (USB `U`/`D`
frames first, Wi-Fi `/update` as the fallback) and the "Install PandaDeck on
a panel" wizard for a stock Panda Touch (its ElegantOTA page at /update).

One job at a time; the settings page polls job.status() for progress.
"""
from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from typing import Callable

from .link.base import LinkError, Unsupported, check_firmware
from .link.wifi import WifiLink, upload_with_progress
from .util import http_json, http_request, multipart

log = logging.getLogger("firmware")

# The project's GitHub releases.
RELEASES_API = "https://api.github.com/repos/wikydtron/pandadeck/releases/latest"
FIRMWARE_ASSET = "pandadeck-firmware.bin"
SUMS_ASSET = "SHA256SUMS.txt"
MAX_DOWNLOAD = 8 * 1024 * 1024
DOWNLOAD_HOSTS = ("github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com")
_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-]{0,252}(:\d{1,5})?$")


def version_tuple(v: str) -> tuple:
    nums = re.findall(r"\d+", (v or "").split("-")[0].split("+")[0])
    return tuple(int(n) for n in nums[:3]) if nums else ()


def is_newer(latest: str, current: str) -> bool:
    a, b = version_tuple(latest), version_tuple(current)
    return bool(a) and (not b or a > b)


def latest_release(get=http_json) -> dict:
    status, data = get(RELEASES_API, headers={"Accept": "application/vnd.github+json", "User-Agent": "pandadeck"},
                       timeout=10)
    if status != 200 or not isinstance(data, dict):
        raise LinkError(f"couldn't check for updates (HTTP {status})")
    assets = {a.get("name"): a for a in data.get("assets") or [] if isinstance(a, dict)}
    asset = assets.get(FIRMWARE_ASSET)
    if not asset:
        raise LinkError(f"the latest release has no {FIRMWARE_ASSET}")
    sums = assets.get(SUMS_ASSET) or {}
    return {"version": str(data.get("tag_name") or "").lstrip("v"), "url": asset.get("browser_download_url", ""),
            "size": asset.get("size", 0), "page": data.get("html_url", ""), "name": data.get("name", ""),
            "sums_url": sums.get("browser_download_url", "")}


def _allowed(url: str) -> bool:
    import urllib.parse
    u = urllib.parse.urlsplit(url)
    return u.scheme == "https" and (u.hostname or "") in DOWNLOAD_HOSTS


def download(url: str, request=http_request) -> bytes:
    if not _allowed(url):
        raise LinkError("refusing a download from outside GitHub releases")
    status, _, data = request(url, headers={"User-Agent": "pandadeck", "Accept": "application/octet-stream"},
                              timeout=120, max_bytes=MAX_DOWNLOAD)
    if status != 200 or not data:
        raise LinkError(f"download failed (HTTP {status})")
    if len(data) > MAX_DOWNLOAD:
        raise LinkError("download is too big to be panel firmware")
    return data


def expected_sha256(sums_text: str, name: str = FIRMWARE_ASSET) -> str:
    """`sha256sum` output -> the hash listed for `name` ("" if absent)."""
    for line in sums_text.splitlines():
        parts = line.strip().split()
        if len(parts) == 2 and parts[1].lstrip("*") == name and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
            return parts[0].lower()
    return ""


def download_release(rel: dict, request=http_request) -> bytes:
    """The release's firmware, checked against its SHA256SUMS.txt when published."""
    data = download(rel["url"], request=request)
    if rel.get("sums_url"):
        want = expected_sha256(download(rel["sums_url"], request=request).decode("utf-8", "replace"))
        if want and hashlib.sha256(data).hexdigest() != want:
            raise LinkError("the downloaded firmware doesn't match the release checksum")
    check_firmware(data)
    return data


def clean_host(host: str) -> str:
    host = (host or "").strip()
    host = re.sub(r"^https?://", "", host).split("/")[0]
    if not _HOST_RE.match(host):
        raise ValueError("type the panel's address, like 10.0.0.57")
    return host


def stock_updater_present(host: str, request=http_request) -> bool:
    """True when http://<host>/update answers (stock ElegantOTA or PandaDeck)."""
    try:
        status, _, body = request(f"http://{host}/update", timeout=5, follow_redirects=False)
    except OSError:
        return False
    return status in (200, 401) and len(body or b"") > 0


class FirmwareJob:
    def __init__(self):
        self._lock = threading.Lock()
        self._st = {"state": "idle", "progress": 0.0, "message": "", "target": "", "at": 0.0}

    def status(self) -> dict:
        with self._lock:
            return dict(self._st)

    def _set(self, **kw) -> None:
        with self._lock:
            self._st.update(kw, at=time.time())

    def busy(self) -> bool:
        return self.status()["state"] in ("downloading", "uploading", "checking")

    def progress(self, done: int, total: int) -> None:
        self._set(progress=round(done / total, 3) if total else 0.0)

    def start(self, target: str, fn: Callable[[], None]) -> bool:
        with self._lock:
            if self._st["state"] in ("downloading", "uploading", "checking"):
                return False
            self._st = {"state": "checking", "progress": 0.0, "message": "", "target": target, "at": time.time()}
        threading.Thread(target=self._run, args=(fn,), name="firmware", daemon=True).start()
        return True

    def _run(self, fn: Callable[[], None]) -> None:
        try:
            fn()
        except (LinkError, ValueError, OSError) as e:
            log.warning("firmware job failed: %s", e)
            self._set(state="error", message=str(e))
        except Exception as e:  # noqa: BLE001
            log.exception("firmware job crashed")
            self._set(state="error", message=f"unexpected error: {e}")


def update_panel(job: FirmwareJob, links, secrets, state, data: bytes) -> None:
    """Flash `data` to the connected panel: USB frames first, else Wi-Fi /update."""
    info = check_firmware(data)
    link = links.current()
    if link is None:
        raise LinkError("no panel connected")
    links.busy = "update"
    try:
        job._set(state="uploading", progress=0.0, message=f"Sending {info['size'] // 1024} KB over {link.kind.upper()}")
        try:
            link.update(data, job.progress)
        except Unsupported:
            if link.kind != "usb":
                raise
            host = state.get("last_ip") or ""
            token = secrets.get("panel_token") or ""
            if not (host and token):
                raise LinkError("this firmware can't update over USB and Wi-Fi isn't set up") from None
            job._set(message="Sending over Wi-Fi")
            WifiLink(host, token).update(data, job.progress)
    finally:
        links.busy = ""
    job._set(state="done", progress=1.0, message="Firmware sent. The panel restarts by itself in about 20 seconds.")
    links.reconnect()


def install_on_stock(job: FirmwareJob, host: str, data: bytes) -> None:
    """The wizard's upload: exactly what the stock ElegantOTA page expects
    (multipart field MD5 = hex md5 of the file, file field `firmware`). No
    token is ever sent to the typed address."""
    check_firmware(data)
    md5 = hashlib.md5(data).hexdigest()  # noqa: S324 - ElegantOTA integrity field, not security
    body, ctype = multipart({"MD5": md5}, "firmware", "firmware.bin", data)
    job._set(state="uploading", progress=0.0, message=f"Uploading to {host}")
    status, text = upload_with_progress(host, "/update", body, ctype, {}, job.progress)
    if status == 401:
        raise LinkError("That panel already runs PandaDeck: update it from Panel > Firmware instead.")
    if status != 200:
        raise LinkError(f"the panel answered HTTP {status}: {text[:120]}")
    job._set(state="done", progress=1.0,
             message="Done! The panel restarts by itself. Now plug it into this computer with a USB-C data cable.")
