"""The app's local web server on 127.0.0.1:<port> (47321, or the next free one).

    /                         settings page (static, no build step, no CDN)
    /api/...                  JSON API for the page (see api.py)
    /pandadeck/<secret>/hook  Claude Code http hooks     } no X-PandaDeck header,
    /pandadeck/<secret>/codex Codex hooks via curl       } rejected if Origin is set
    /pandadeck/health         liveness (old installers check it)
    /ext/<name>.js|.css       settings-page sections of optional extensions
    /spotify/callback         Spotify OAuth redirect

Security (docs/app.md): binds 127.0.0.1 only; every request's Host must be
127.0.0.1:<port> or localhost:<port> (DNS-rebinding guard); API calls need
`X-PandaDeck: 1` (a cross-site page can't send it without a CORS preflight,
which we never grant) and, when present, an Origin of the page itself.
"""
from __future__ import annotations

import hmac
import json
import logging
import mimetypes
import socket
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ..ai.bridge import MAX_BODY as HOOK_MAX_BODY
from ..ai.bridge import socket_alive

log = logging.getLogger("web")

STATIC = Path(__file__).resolve().parent / "static"
STATIC_FILES = {"/": "index.html", "/index.html": "index.html", "/app.js": "app.js", "/deck.js": "deck.js",
                "/lab.js": "lab.js", "/sounds.js": "sounds.js", "/works.js": "works.js",
                "/aiicons.js": "aiicons.js",
                "/style.css": "style.css",
                "/logo.svg": "logo.svg", "/favicon.svg": "logo.svg"}
MAX_JSON = 256 * 1024
MAX_UPLOAD = 8 * 1024 * 1024
DRAIN_MAX = 16 * 1024 * 1024
CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; "
       "font-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
UNGUARDED_GETS = ("/api/ping", "/api/apps/icon", "/api/music/cover", "/api/icons/get",  # <img> can't send headers
                  "/api/targets/icon", "/api/sounds/artwork/thumb", "/api/ai/icons/thumb")


class Request:
    def __init__(self, method: str, path: str, query: dict, headers, body: bytes, alive=lambda: True):
        self.method, self.path, self.query, self.headers, self.body = method, path, query, headers, body
        self.alive = alive

    def json(self):
        try:
            return json.loads(self.body or b"{}")
        except ValueError:
            raise ApiError(400, "the request body isn't valid JSON") from None

    def arg(self, name: str, default: str = "") -> str:
        v = self.query.get(name)
        return v[0] if v else default


class Response:
    def __init__(self, status: int = 200, body: bytes = b"", ctype: str = "application/json",
                 headers: dict | None = None):
        self.status, self.body, self.ctype, self.headers = status, body, ctype, headers or {}


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def json_response(obj, status: int = 200) -> Response:
    return Response(status, json.dumps(obj).encode())


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    # Windows' SO_REUSEADDR lets a second socket share a busy port: never use it
    # there, and claim the port exclusively instead.
    allow_reuse_address = sys.platform != "win32"
    # The page loads several scripts, styles and icons at once over HTTP/1.0 (one
    # connection each): socketserver's default backlog of 5 makes Windows refuse some.
    request_queue_size = 64

    def server_bind(self):
        if sys.platform == "win32" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def allowed_hosts(port: int) -> set[str]:
    return {f"127.0.0.1:{port}", f"localhost:{port}"}


def check_host(host: str | None, port: int) -> bool:
    return bool(host) and host.strip().lower() in allowed_hosts(port)


def check_origin(origin: str | None, port: int) -> bool:
    if not origin:
        return True
    return origin.strip().lower() in {f"http://{h}" for h in allowed_hosts(port)}


def redact(path: str) -> str:
    """For logs: no query string, no hook secret."""
    path = path.split("?")[0]
    if path.startswith("/pandadeck/") and path.count("/") >= 3:
        parts = path.split("/")
        parts[2] = "<secret>"
        path = "/".join(parts)
    return path


def make_server(svc, port: int, routes: dict, host: str = "127.0.0.1") -> LocalServer:
    """routes: {(METHOD, path): handler(svc, Request) -> Response | dict}."""

    class Handler(BaseHTTPRequestHandler):
        # One request per connection: a refused request's unread body can never be
        # parsed as a second, attacker-shaped request (no keep-alive smuggling).
        protocol_version = "HTTP/1.0"
        server_version = "PandaDeck"
        sys_version = ""
        timeout = 30  # socket reads/writes; a stalled client can't pin a thread forever

        def log_message(self, fmt, *args):  # quiet: no query strings (OAuth codes), no hook secret
            log.debug("http %s %s", self.command, redact(self.path))

        def _send(self, resp: Response) -> None:
            self.send_response(resp.status)
            self.send_header("Content-Type", resp.ctype)
            self.send_header("Content-Length", str(len(resp.body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            if resp.ctype.startswith("text/html"):
                self.send_header("Content-Security-Policy", CSP)
            if "Cache-Control" not in resp.headers:
                self.send_header("Cache-Control", "no-store")
            for k, v in resp.headers.items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD" and resp.body:
                self.wfile.write(resp.body)

        def _error(self, status: int, msg: str) -> None:
            self.close_connection = True
            self._send(json_response({"ok": False, "error": msg}, status))

        def _read_body(self, limit: int) -> bytes | None:
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = -1
            if n < 0 or n > limit:
                if 0 < n <= DRAIN_MAX:  # read it away so the client sees the 413, not a reset
                    while n > 0:
                        chunk = self.rfile.read(min(n, 65536))
                        if not chunk:
                            break
                        n -= len(chunk)
                self._error(413, "request too large")
                self.close_connection = True
                return None
            return self.rfile.read(n) if n else b""

        # ---- dispatch ------------------------------------------------------------
        def do_GET(self):  # noqa: N802
            self._dispatch("GET")

        def do_HEAD(self):  # noqa: N802
            self._dispatch("GET")

        def do_POST(self):  # noqa: N802
            self._dispatch("POST")

        def do_PUT(self):  # noqa: N802
            self._dispatch("PUT")

        def do_DELETE(self):  # noqa: N802
            self._dispatch("DELETE")

        def _dispatch(self, method: str) -> None:
            port = self.server.server_address[1]
            if not check_host(self.headers.get("Host"), port):
                self._error(421, "wrong host")
                return
            if self.headers.get("Transfer-Encoding"):  # we only speak Content-Length bodies
                self._error(400, "chunked bodies aren't supported")
                return
            parsed = urllib.parse.urlsplit(self.path)
            path = parsed.path
            query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            try:
                if path.startswith("/pandadeck/"):
                    self._pandadeck(method, path)
                elif method == "GET" and path in STATIC_FILES:
                    self._static(STATIC / STATIC_FILES[path])
                elif method == "GET" and path in ext_static:  # extension UI bundles (exact paths only)
                    self._static(ext_static[path])
                elif path == "/spotify/callback" and method == "GET":
                    self._route(method, path, query, b"")
                elif path.startswith("/api/"):
                    self._api(method, path, query, port)
                else:
                    self._error(404, "not found")
            except ApiError as e:
                self._error(e.status, e.message)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception:  # noqa: BLE001 - never kill the server thread
                log.exception("request %s %s failed", method, redact(path))
                try:
                    self._error(500, "internal error (see the log)")
                except OSError:
                    pass

        def _api(self, method: str, path: str, query: dict, port: int) -> None:
            guarded = not (method == "GET" and path in UNGUARDED_GETS)
            if guarded and self.headers.get("X-PandaDeck") != "1":
                raise ApiError(403, "missing X-PandaDeck header")
            if method != "GET":
                if not check_origin(self.headers.get("Origin"), port):
                    raise ApiError(403, "cross-origin request refused")
                site = self.headers.get("Sec-Fetch-Site")
                if site and site not in ("same-origin", "none"):
                    raise ApiError(403, "cross-site request refused")
            limit = MAX_UPLOAD if path in svc_upload_paths else MAX_JSON
            body = self._read_body(limit) if method != "GET" else b""
            if body is None:
                return
            if method != "GET" and body and path not in svc_upload_paths:
                ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                if ctype != "application/json":
                    raise ApiError(415, "send JSON")
            self._route(method, path, query, body)

        def _route(self, method: str, path: str, query: dict, body: bytes) -> None:
            fn = routes.get((method, path))
            if fn is None:
                raise ApiError(404 if not any(p == path for _, p in routes) else 405, "not found")
            req = Request(method, path, query, self.headers, body, lambda: socket_alive(self.connection))
            out = fn(svc, req)
            self._send(out if isinstance(out, Response) else json_response(out))

        def _static(self, f: Path) -> None:
            name = f.name
            try:
                data = f.read_bytes()
            except OSError:
                self._error(404, "not found")
                return
            ctype = {"html": "text/html; charset=utf-8", "js": "text/javascript; charset=utf-8",
                     "css": "text/css; charset=utf-8", "svg": "image/svg+xml"}.get(
                name.rsplit(".", 1)[-1], mimetypes.guess_type(name)[0] or "application/octet-stream")
            self._send(Response(200, data, ctype, {"Cache-Control": "no-cache"}))

        # ---- hooks -----------------------------------------------------------------
        def _pandadeck(self, method: str, path: str) -> None:
            if path == "/pandadeck/health" and method == "GET":
                self._send(json_response({"ok": True}))
                return
            parts = path.split("/")  # ["", "pandadeck", secret, kind]
            secret = svc.hook_secret
            if (method != "POST" or len(parts) != 4 or parts[3] not in ("hook", "codex")
                    or not hmac.compare_digest(parts[2].encode(), secret.encode())
                    or self.headers.get("Origin")):  # browsers send Origin on cross-site POSTs; agents don't
                self._error(404, "not found")
                return
            body = self._read_body(HOOK_MAX_BODY)
            if body is None:
                return
            try:
                payload = json.loads(body) if body else None
            except ValueError:
                payload = None
            if payload is None:
                self._send(Response(200, b""))  # never break an agent over a bad payload
                return
            src = "codex" if parts[3] == "codex" else "claude"
            out = svc.handle_hook(payload, src, lambda: socket_alive(self.connection))
            self._send(Response(200, out, "application/json"))

    svc_upload_paths = set(getattr(svc, "upload_paths", ()))
    ext_static = dict(getattr(svc, "ext_static", {}) or {})
    srv = LocalServer((host, port), Handler)
    return srv


def bind_first_free(svc, start_port: int, span: int, routes: dict) -> LocalServer:
    """The configured port, else the next free one in [start, start+span)."""
    last: OSError | None = None
    for port in range(start_port, start_port + span):
        try:
            return make_server(svc, port, routes)
        except OSError as e:
            last = e
            log.info("port %s busy (%s), trying the next one", port, e)
    raise OSError(f"no free port in {start_port}..{start_port + span - 1}: {last}")
