"""Small shared helpers: atomic private JSON files, HTTP without surprises."""
from __future__ import annotations

import json
import os
import tempfile
import threading
import urllib.error
import urllib.request
import uuid
from pathlib import Path

PRIVATE_MODE = 0o600


def read_json(path: Path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def load_json_file(path: Path, default=None):
    """Like read_json, but a file that exists and can't be parsed is first set
    aside as <name>.bad-<time> (and logged), so nothing is silently lost when
    the caller later writes defaults over it."""
    import logging
    import shutil
    import time as _time
    path = Path(path)
    if not path.exists():
        return default
    for attempt in range(3):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            break
        except OSError:  # locked by a backup tool / antivirus: try again shortly
            if attempt == 2:
                raise
            _time.sleep(0.2)
    bad = path.with_name(f"{path.name}.bad-{_time.strftime('%Y%m%d-%H%M%S')}")
    try:
        shutil.copy2(path, bad)
    except OSError:
        pass
    logging.getLogger("files").warning("%s was unreadable; kept a copy as %s and started fresh", path.name, bad.name)
    return default


def write_json_atomic(path: Path, data, private: bool = True, ensure_ascii: bool = True) -> None:
    """Write to a temp file in the same folder, fsync, then rename over the
    target, so a crash never leaves a half-written file. `private` files get
    0600 where the OS supports POSIX permissions."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        if private and os.name == "posix":
            os.fchmod(fd, PRIVATE_MODE)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=False, ensure_ascii=ensure_ascii)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        if private and os.name == "posix":
            os.chmod(path, PRIVATE_MODE)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects: a bearer token must never follow a 30x elsewhere."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401, PLR0913
        raise urllib.error.HTTPError(req.full_url, code, f"redirect refused ({code})", headers, fp)


_NO_PROXY = urllib.request.ProxyHandler({})


def is_local_host(host: str) -> bool:
    """Loopback, private (RFC 1918 / link-local / ULA) or *.local: never via a proxy."""
    import ipaddress
    host = (host or "").strip("[]").lower()
    if host in ("localhost",) or host.endswith(".local") or "." not in host and ":" not in host:
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local


def _opener(url: str, follow_redirects: bool):
    import urllib.parse
    handlers = [] if follow_redirects else [NoRedirect]
    if is_local_host(urllib.parse.urlsplit(url).hostname or ""):
        handlers.append(_NO_PROXY)  # a corporate proxy can't reach the panel, the lab or ourselves
    return urllib.request.build_opener(*handlers)


def http_request(url: str, *, method: str = "GET", headers: dict | None = None, body: bytes | None = None,
                 timeout: float = 10.0, follow_redirects: bool = True, max_bytes: int | None = None):
    """Returns (status, headers, body bytes). HTTP errors are returned, not
    raised (callers often need the headers of a 4xx); network errors raise
    OSError, and so does a body larger than max_bytes."""
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    opener = _opener(url, follow_redirects)
    try:
        with opener.open(req, timeout=timeout) as r:
            if max_bytes is None:
                return r.status, r.headers, r.read()
            data = r.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise OSError(f"response larger than {max_bytes} bytes")
            return r.status, r.headers, data
    except urllib.error.HTTPError as e:
        try:
            data = e.read() if e.fp else b""
        except OSError:
            data = b""
        return e.code, e.headers, data


def http_json(url: str, *, method: str = "GET", headers: dict | None = None, payload=None,
              timeout: float = 10.0, follow_redirects: bool = True):
    """(status, parsed JSON or None)."""
    hdrs = {"Accept": "application/json", **(headers or {})}
    body = None
    if payload is not None:
        body = json.dumps(payload).encode()
        hdrs.setdefault("Content-Type", "application/json")
    status, _, data = http_request(url, method=method, headers=hdrs, body=body, timeout=timeout,
                                   follow_redirects=follow_redirects)
    try:
        return status, json.loads(data) if data else None
    except ValueError:
        return status, None


def multipart(fields: dict[str, str], file_field: str, filename: str, payload: bytes) -> tuple[bytes, str]:
    """multipart/form-data body (ElegantOTA / the panel's upload handlers)."""
    boundary = uuid.uuid4().hex
    parts = [f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
             for k, v in fields.items()]
    parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; filename=\"{filename}\"\r\n"
                 "Content-Type: application/octet-stream\r\n\r\n".encode() + payload + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


class Loop(threading.Thread):
    """A daemon thread that calls step() every `interval` seconds until stopped.
    step() exceptions are logged by the subclass and never kill the loop."""

    def __init__(self, name: str, interval: float):
        super().__init__(name=name, daemon=True)
        self.interval = interval
        self._halt = threading.Event()
        self._poke = threading.Event()

    def run(self) -> None:
        while not self._halt.is_set():
            try:
                self.step()
            except Exception:  # noqa: BLE001 - background loops must survive anything
                import logging
                logging.getLogger(self.name).exception("%s step failed", self.name)
            self._poke.wait(self.next_interval())
            self._poke.clear()

    def next_interval(self) -> float:
        return self.interval

    def step(self) -> None:
        raise NotImplementedError

    def poke(self) -> None:
        self._poke.set()

    def stop(self) -> None:
        self._halt.set()
        self._poke.set()
