#!/usr/bin/env python3
"""pandadeck-lab: a tiny homelab agent for the PandaDeck HUD page.

Runs on a Linux, macOS or Windows server (Python 3.8+ and psutil). The
PandaDeck app polls it and shows CPU / RAM / temperature / GPU / disk, and,
when the `docker` command is there, container health and the busiest
containers (hold a container's tile on the panel to restart it).

    python3 pandadeck_lab.py              first run: makes a token, prints it and the address
    python3 pandadeck_lab.py --show-token prints the token again
    python3 pandadeck_lab.py --new-token  replaces the token (re-enter it in the app)

    GET  /health                    {"ok": true}           (no token)
    GET  /api/lab                   the HUD `lab` block    (token)
    POST /api/restart/<container>   docker restart <id>    (token)

Every /api request needs `Authorization: Bearer <token>` (checked in constant
time). The token comes from --token, --token-file, $PANDADECK_LAB_TOKEN, or
the token file this script keeps: `pandadeck_lab.token` next to it (when that
folder is writable), else in your config folder.

Listens on all interfaces (--host 0.0.0.0) on port 47380 by default. It's plain
HTTP: the token and the stats cross your LAN unencrypted, so run it on a
network you trust (or --host 127.0.0.1 behind a TLS reverse proxy).

Temperatures: psutil on Linux; on Windows, LibreHardwareMonitor's web server
(Options > Remote Web Server, port 8085) when it runs; none on macOS. GPU:
nvidia-smi when present. Disk: the root / system drive.
Container restarts take a hex id only and run `docker restart <id>` without a shell.

Optional: --backup-stamp /path/to/file  shows the age of that file's mtime
as "last backup" (touch it at the end of your backup job).

Keeping it running: the PandaDeck settings page (HUD > Connect a homelab)
has copy-paste steps for systemd, launchd and Task Scheduler.
"""
from __future__ import annotations

import argparse
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

log = logging.getLogger("pandadeck-lab")

VERSION = "1.1.0"
DEFAULT_PORT = 47380
DEFAULT_HOST = "0.0.0.0"
TOKEN_FILE = "pandadeck_lab.token"
MIN_TOKEN = 16
LHM_URL = "http://127.0.0.1:8085/data.json"  # LibreHardwareMonitor's own web server (Windows)
LHM_RETRY_S = 60.0
NO_WINDOW = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW: no console flashes under pythonw
CONTAINER_ID = re.compile(r"^[0-9a-f]{12,64}$")
CACHE_S = 3.0
DOCKER_CACHE_S = 10.0
TEMP_SENSORS = ("coretemp", "k10temp", "zenpower", "cpu_thermal", "cpu-thermal", "acpitz")


# ---- pure helpers (unit-tested) ---------------------------------------------------------

def _pct(s) -> float:
    try:
        return float(str(s).strip().rstrip("%") or 0)
    except ValueError:
        return 0.0


def parse_status(status: str) -> tuple[str, str, int | None]:
    """docker ps .Status -> (state, health, exit code)."""
    s = (status or "").strip()
    low = s.lower()
    health = "unhealthy" if "(unhealthy)" in low else "healthy" if "(healthy)" in low else \
        "starting" if "health: starting" in low else ""
    if low.startswith("up"):
        state = "running"
    elif low.startswith("restarting"):
        state = "restarting"
    elif low.startswith("exited"):
        state = "exited"
    elif low.startswith("created"):
        state = "created"
    elif low.startswith("dead"):
        state = "dead"
    elif "paused" in low:
        state = "paused"
    else:
        state = low.split(" ")[0] if low else ""
    m = re.search(r"exited \((-?\d+)\)", low)
    return state, health, int(m.group(1)) if m else None


def summarize(ps_rows: list[dict], stats_rows: list[dict]) -> dict:
    """`docker ps -a` + `docker stats` JSON lines -> counts, bad and top lists.

    bad = running but unhealthy, restarting, dead, or exited with an error
    code (a clean `docker stop` exits 0 and isn't a problem)."""
    stats = {}
    for r in stats_rows or []:
        cid = str(r.get("ID") or r.get("Container") or "")[:12]
        stats[cid] = (_pct(r.get("CPUPerc")), _pct(r.get("MemPerc")))
    bad, top = [], []
    running = healthy = 0
    for r in ps_rows or []:
        cid = str(r.get("ID") or "")[:12]
        state, health, code = parse_status(r.get("Status") or "")
        state = (r.get("State") or state or "").lower()
        cpu, mem = stats.get(cid, (0.0, 0.0))
        item = {"id": cid, "name": str(r.get("Names") or "")[:31], "state": state[:15], "health": health[:15],
                "cpu": round(cpu, 1), "mem": round(mem, 1)}
        if state == "running":
            running += 1
            if health == "healthy":
                healthy += 1
        if (state == "running" and health == "unhealthy") or state in ("restarting", "dead") or \
                (state == "exited" and code not in (None, 0)):
            bad.append(item)
        elif state == "running":
            top.append(item)
    bad.sort(key=lambda x: (x["health"] != "unhealthy", x["name"]))
    top.sort(key=lambda x: x["cpu"], reverse=True)
    return {"run": running, "total": len(ps_rows or []), "healthy": healthy, "bad": bad[:6], "top": top[:3]}


def token_ok(header: str, token: str) -> bool:
    if not token or not header:
        return False
    scheme, _, value = header.partition(" ")
    return scheme.lower() == "bearer" and hmac.compare_digest(value.strip().encode(), token.encode())


# ---- collectors ---------------------------------------------------------------------------

def _run(argv: list[str], timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False,  # noqa: S603
                          creationflags=NO_WINDOW)


def _docker_json_lines(args: list[str], timeout: float = 15.0) -> list[dict]:
    docker = shutil.which("docker")
    if not docker:
        return []
    out = _run([docker, *args], timeout)
    rows = []
    for line in out.stdout.splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows


def lhm_cpu_temp(data) -> float:
    """CPU package temperature from LibreHardwareMonitor's data.json (-1 if absent).
    Same rules as the app's hud/pc.py (this file stays standalone)."""
    best = -1.0
    stack = [data] if isinstance(data, dict) else []
    while stack:
        node = stack.pop()
        is_temp = node.get("Type") == "Temperature" or "\u00b0" in str(node.get("Value", ""))
        if str(node.get("Text", "")) in ("CPU Package", "Core (Tctl/Tdie)") and is_temp:
            try:
                best = max(best, float(str(node["Value"]).split()[0].replace(",", ".")))
            except (ValueError, IndexError):
                pass
        stack.extend(c for c in node.get("Children", []) if isinstance(c, dict))
    return best


_lhm_down_until = 0.0


def _lhm_temp() -> float:
    global _lhm_down_until
    if time.monotonic() < _lhm_down_until:
        return -1
    import urllib.request
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # localhost: never via a proxy
    try:
        with opener.open(LHM_URL, timeout=2) as r:
            return lhm_cpu_temp(json.loads(r.read(4_000_000)))
    except (OSError, ValueError):
        _lhm_down_until = time.monotonic() + LHM_RETRY_S
        return -1


def cpu_temp(platform: str = sys.platform) -> float:
    if platform == "win32":
        t = _lhm_temp()
        return round(t, 1) if t > 0 else -1
    import psutil
    try:
        temps = psutil.sensors_temperatures() if hasattr(psutil, "sensors_temperatures") else {}
    except Exception:  # noqa: BLE001 - macOS has none; some Linux boxes raise
        return -1
    for name in TEMP_SENSORS:
        vals = [e.current for e in temps.get(name) or [] if e.current and e.current > 0]
        if vals:
            return round(max(vals), 1)
    return -1


def disk_path(platform: str = sys.platform) -> str:
    """The root filesystem, or Windows' system drive."""
    if platform == "win32":
        return (os.environ.get("SystemDrive") or "C:") + "\\"
    return "/"


def gpu_stats() -> tuple[float, float, str]:
    """(utilization %, temperature C, model name) from nvidia-smi, if present."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return -1, -1, ""
    try:
        out = _run([exe, "--query-gpu=utilization.gpu,temperature.gpu,name", "--format=csv,noheader,nounits"], 5).stdout
        parts = [x.strip() for x in out.splitlines()[0].split(",")]
        return float(parts[0]), float(parts[1]), ",".join(parts[2:])
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        return -1, -1, ""


# Short model names (same rules as the app's pandadeck/hud/hw.py; this file stays standalone).
_CPU_PATTERNS = (
    r"\b(i[3579]-\d{3,5}[A-Z]{0,3})\b", r"\b(Ultra \d \d{3}[A-Z]{0,2})\b", r"\b(Threadripper (?:PRO )?\d{4}\w*)",
    r"\b(Ryzen AI \d+ (?:HX |PRO )?\d{3}\w*)", r"\b(Ryzen \d (?:PRO )?\d{4}\w*)",
    r"\b(Xeon\b.*?[A-Z]?-?\d{4}\w*(?: v\d)?)", r"\b(EPYC \d{4}\w*)", r"\b(M\d(?: (?:Pro|Max|Ultra))?)\b",
    r"\b(Raspberry Pi \d\w*(?: Model \w)?)", r"\b(Celeron \w+|Pentium \w+|Atom \w+|N\d{2,3})\b",
)
_GPU_RE = re.compile(r"\b((?:RTX|GTX|RX|Arc)\s*(?:PRO\s+)?[A-Z]?\d{3,4}\w*(?:\s+(?:Ti|SUPER|XTX|XT|GRE))*)",
                     re.IGNORECASE)


def _ascii(text: str, limit: int = 23) -> str:
    import unicodedata
    norm = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in norm if 32 <= ord(c) < 127).strip()[:limit]


def short_cpu(brand: str) -> str:
    text = re.sub(r"\s+", " ", re.sub(r"\((?:R|TM|tm|r)\)", "", brand or "").replace(" CPU", " ")).strip()
    for pat in _CPU_PATTERNS:
        m = re.search(pat, text)
        if m:
            return _ascii(m.group(1))
    text = re.sub(r"@.*$", "", text)
    for drop in ("Intel", "AMD", "Core", "Processor", "with Radeon Graphics"):
        text = text.replace(drop, " ")
    return _ascii(re.sub(r"\s+", " ", re.sub(r"\d+-Core", " ", text)).strip())


def short_gpu(name: str) -> str:
    m = _GPU_RE.search(re.sub(r"\((?:R|TM)\)", "", name or ""))
    if m:
        return _ascii(re.sub(r"\s+", " ", m.group(1)))
    for drop in ("NVIDIA", "GeForce", "AMD", "Radeon", "Graphics", "GPU"):
        name = (name or "").replace(drop, " ")
    return _ascii(re.sub(r"\s+", " ", name).strip())


def cpu_brand(platform: str = sys.platform) -> str:
    if platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
                return str(winreg.QueryValueEx(k, "ProcessorNameString")[0]).strip()
        except OSError:
            pass
    elif platform == "darwin":
        sysctl = shutil.which("sysctl") or "/usr/sbin/sysctl"
        try:
            out = _run([sysctl, "-n", "machdep.cpu.brand_string"], 5).stdout.strip()
            if out:
                return out
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        try:
            with open("/proc/cpuinfo", encoding="utf-8", errors="replace") as f:
                info = f.read()
            for key in ("Model", "model name", "Hardware"):
                m = re.search(rf"^{key}\s*:\s*(.+)$", info, re.MULTILINE)
                if m:
                    return m.group(1).strip()
        except OSError:
            pass
    import platform as pf
    return pf.processor() or ""


class Lab:
    def __init__(self, backup_stamp: str = "", name: str = ""):
        import psutil
        self.ps = psutil
        self.backup_stamp = backup_stamp
        self.name = name or socket.gethostname().split(".")[0]
        self.cpu_model = short_cpu(cpu_brand())
        self._lock = threading.Lock()
        self._cache: tuple[float, dict] = (0.0, {})
        self._docker: tuple[float, dict] = (0.0, {"run": -1, "total": -1, "healthy": -1, "bad": [], "top": []})
        psutil.cpu_percent(None)

    def docker(self) -> dict:
        if time.time() - self._docker[0] < DOCKER_CACHE_S:
            return self._docker[1]
        try:
            ps_rows = _docker_json_lines(["ps", "-a", "--no-trunc", "--format", "{{json .}}"])
            stats_rows = _docker_json_lines(["stats", "--no-stream", "--format", "{{json .}}"], timeout=30) \
                if ps_rows else []
            d = summarize(ps_rows, stats_rows) if ps_rows or shutil.which("docker") else self._docker[1]
        except (OSError, subprocess.TimeoutExpired) as e:
            log.warning("docker query failed: %s", e)
            d = self._docker[1]
        self._docker = (time.time(), d)
        return d

    def snapshot(self) -> dict:
        with self._lock:
            if time.time() - self._cache[0] < CACHE_S:
                return self._cache[1]
            ps = self.ps
            gpu, gpu_t, gpu_name = gpu_stats()
            backup_h = -1.0
            if self.backup_stamp:
                try:
                    backup_h = round((time.time() - os.path.getmtime(self.backup_stamp)) / 3600, 1)
                except OSError:
                    backup_h = -1.0
            data = {"ok": True, "name": self.name, "cpu_model": self.cpu_model, "gpu_model": short_gpu(gpu_name),
                    "cpu": round(ps.cpu_percent(None), 1),
                    "ram": round(ps.virtual_memory().percent, 1), "temp": cpu_temp(), "gpu": gpu, "gpu_t": gpu_t,
                    "disk": round(ps.disk_usage(disk_path()).percent, 1), "backup_h": backup_h, **self.docker(),
                    "ver": VERSION}
            self._cache = (time.time(), data)
            return data

    def restart(self, cid: str) -> tuple[bool, str]:
        if not CONTAINER_ID.fullmatch(cid or ""):
            return False, "invalid container id"
        docker = shutil.which("docker")
        if not docker:
            return False, "docker not found"
        r = _run([docker, "restart", cid], 60)
        self._docker = (0.0, self._docker[1])
        return r.returncode == 0, (r.stderr or "").strip()[:200]


# ---- HTTP -------------------------------------------------------------------------------------

def make_server(lab: Lab, token: str, host: str, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        server_version = "pandadeck-lab"
        sys_version = ""

        def log_message(self, fmt, *args):  # noqa: D401
            log.debug("http " + fmt, *args)

        def _send(self, code: int, obj) -> None:
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _authed(self) -> bool:
            if token_ok(self.headers.get("Authorization", ""), token):
                return True
            self._send(401, {"ok": False, "error": "token"})
            return False

        def do_GET(self):  # noqa: N802
            path = self.path.split("?")[0]
            if path == "/health":
                self._send(200, {"ok": True})
            elif path == "/api/lab":
                if self._authed():
                    self._send(200, lab.snapshot())
            else:
                self._send(404, {"ok": False, "error": "not found"})

        def do_POST(self):  # noqa: N802
            path = self.path.split("?")[0]
            m = re.fullmatch(r"/api/restart/([0-9a-f]{12,64})", path)
            n = int(self.headers.get("Content-Length") or 0)
            if 0 < n <= 4096:
                self.rfile.read(n)
            if not m:
                self._send(404, {"ok": False, "error": "not found"})
                return
            if not self._authed():
                return
            ok, err = lab.restart(m.group(1))
            log.info("restart %s -> %s %s", m.group(1), ok, err)
            self._send(200 if ok else 500, {"ok": ok, "error": err})

    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    return srv


def config_dir(platform: str = sys.platform) -> Path:
    if platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    elif platform == "darwin":
        base = str(Path.home() / "Library" / "Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "pandadeck-lab"


def token_paths(script_dir: Path | None = None) -> list[Path]:
    """Where the token file may live, preferred first: next to this script, then the config folder."""
    here = Path(script_dir or Path(__file__).resolve().parent)
    return [here / TOKEN_FILE, config_dir() / TOKEN_FILE]


def _writable(folder: Path) -> bool:
    try:
        folder.mkdir(parents=True, exist_ok=True)
        probe = folder / f".pandadeck-lab-{os.getpid()}"
        probe.write_text("x", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def write_token(path: Path, token: str) -> None:
    """Owner-only (0600) file holding just the token."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(token + "\n")
    os.replace(tmp, path)


def stored_token(paths: list[Path]) -> tuple[str, Path | None]:
    for p in paths:
        try:
            tok = p.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if tok:
            return tok, p
    return "", None


def resolve_token(args, paths: list[Path]) -> tuple[str, str]:
    """(token, where it came from). Makes and stores a new one when there's none."""
    if args.token:
        return args.token.strip(), "--token"
    if args.token_file:
        with open(args.token_file, encoding="utf-8") as f:
            return f.read().strip(), args.token_file
    env = os.environ.get("PANDADECK_LAB_TOKEN", "").strip()
    if env:
        return env, "$PANDADECK_LAB_TOKEN"
    tok, where = ("", None) if args.new_token else stored_token(paths)
    if tok:
        return tok, str(where)
    tok = secrets.token_urlsafe(24)
    target = next((p for p in paths if _writable(p.parent)), paths[-1])
    write_token(target, tok)
    return tok, "new:" + str(target)


def lan_address() -> str:
    """Best guess at this machine's LAN IPv4 (a UDP connect sends nothing)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.254.254.254", 1))
        return s.getsockname()[0]
    except OSError:
        return socket.gethostname()
    finally:
        s.close()


def main(argv: list[str] | None = None, script_dir: Path | None = None) -> int:
    ap = argparse.ArgumentParser(description="PandaDeck homelab agent (Linux, macOS, Windows)")
    ap.add_argument("--host", default=DEFAULT_HOST,
                    help=f"address to listen on (default {DEFAULT_HOST} = the whole LAN, plain HTTP; 127.0.0.1 = this machine only)")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"port (default {DEFAULT_PORT})")
    ap.add_argument("--token", default="", help="use this token (visible in the process list: prefer the token file)")
    ap.add_argument("--token-file", help="file holding the token")
    ap.add_argument("--show-token", action="store_true", help="print the token and exit")
    ap.add_argument("--new-token", action="store_true", help="make a new token (the app needs the new one)")
    ap.add_argument("--name", default="", help="name shown on the panel (default: hostname)")
    ap.add_argument("--backup-stamp", default="", help="file whose age is shown as 'last backup'")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    paths = token_paths(script_dir)
    if args.show_token:
        tok, where = stored_token(paths)
        if not tok:
            print("No token yet: run pandadeck_lab.py once to make one.")
            return 1
        print(tok)
        return 0
    try:
        token, source = resolve_token(args, paths)
    except OSError as e:
        log.error("can't read or store the token: %s", e)
        return 2
    if len(token) < MIN_TOKEN:
        log.error("refusing to start without a token of at least %d characters", MIN_TOKEN)
        return 2
    try:
        import psutil  # noqa: F401
    except ImportError:
        log.error("psutil is missing: install it with  python3 -m pip install psutil")
        return 3
    srv = make_server(Lab(args.backup_stamp, args.name), token, args.host, args.port)
    port = srv.server_address[1]
    where = lan_address() if args.host in ("0.0.0.0", "", "::") else args.host
    if source.startswith("new:"):
        print(f"New token (saved in {source[4:]}):\n\n    {token}\n", flush=True)
    print(f"pandadeck-lab {VERSION} is running. In PandaDeck > HUD > Homelab, enter:\n"
          f"    address  http://{where}:{port}\n"
          f"    token    {'(above)' if source.startswith('new:') else 'python3 pandadeck_lab.py --show-token'}",
          flush=True)
    log.info("listening on %s:%s (token from %s)", args.host, port,
             source[4:] if source.startswith("new:") else source)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
