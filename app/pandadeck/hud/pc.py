"""This computer, for the HUD page: psutil everywhere, nvidia-smi only when it
is on PATH (one long-running `-lms` stream, no process per sample), mic and
volume from the platform's AudioBackend. The `pc` block format is unchanged."""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import threading
import time

from ..deck.grammar import ascii_label
from ..util import Loop
from .hw import cpu_brand, host_name, short_cpu, short_gpu

log = logging.getLogger("pc")

NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW
TEMP_SENSORS = ("coretemp", "k10temp", "zenpower", "cpu_thermal", "cpu-thermal", "acpitz")
# Windows has no CPU temperature API without a kernel driver; if the user runs
# LibreHardwareMonitor with its web server on (Options > Remote Web Server), we read it.
LHM_URL = "http://127.0.0.1:8085/data.json"


def system_disk() -> str:
    if sys.platform == "win32":
        return (os.environ.get("SystemDrive") or "C:") + "\\"
    return "/"


def cpu_temp(sensors=None) -> float:
    """Best CPU package temperature psutil can see (Linux/FreeBSD), else -1."""
    try:
        import psutil
        temps = sensors if sensors is not None else (psutil.sensors_temperatures()
                                                     if hasattr(psutil, "sensors_temperatures") else {})
    except Exception:  # noqa: BLE001
        return -1
    for name in TEMP_SENSORS:
        entries = temps.get(name) or []
        pkg = [e for e in entries if "package" in (e.label or "").lower() or "tctl" in (e.label or "").lower()]
        pick = pkg or entries
        vals = [e.current for e in pick if e.current and e.current > 0]
        if vals:
            return round(max(vals), 1)
    return -1


def lhm_cpu_temp(data: dict) -> float:
    """Pure: CPU package temperature from LibreHardwareMonitor's data.json (-1 if absent).
    Matches on the sensor Type, not the unit: LHM 0.9.x sends the degree sign in a
    legacy encoding, so it can arrive as U+FFFD."""
    best = -1.0
    stack = [data] if isinstance(data, dict) else []
    while stack:
        node = stack.pop()
        is_temp = node.get("Type") == "Temperature" or "°" in str(node.get("Value", ""))
        if str(node.get("Text", "")) in ("CPU Package", "Core (Tctl/Tdie)") and is_temp:
            try:
                best = max(best, float(str(node["Value"]).split()[0].replace(",", ".")))
            except (ValueError, IndexError):
                pass
        stack.extend(c for c in node.get("Children", []) if isinstance(c, dict))
    return best


class LhmPoller(threading.Thread):
    """LibreHardwareMonitor's local web server on its own thread: it can take a second
    or two to answer, which must never stall the 1 s PC sample. Silent when absent."""
    EVERY_S, TIMEOUT_S, RETRY_S, STALE_S = 2.0, 4.0, 30.0, 15.0

    def __init__(self, url: str = LHM_URL):
        super().__init__(name="lhm", daemon=True)
        self.url = url
        self._temp, self._at = -1.0, 0.0
        self._halt = threading.Event()

    def value(self) -> float:
        return self._temp if time.time() - self._at < self.STALE_S else -1

    def run(self) -> None:
        import json
        import urllib.request
        while not self._halt.is_set():
            try:
                with urllib.request.urlopen(self.url, timeout=self.TIMEOUT_S) as r:  # noqa: S310 - fixed localhost URL
                    t = lhm_cpu_temp(json.loads(r.read(4_000_000)))
                if t > 0:
                    self._temp, self._at = t, time.time()
                self._halt.wait(self.EVERY_S)
            except Exception:  # noqa: BLE001 - not running: look again later
                self._halt.wait(self.RETRY_S)

    def stop(self) -> None:
        self._halt.set()


def parse_nvidia_line(line: str) -> dict | None:
    """Pure: one `nvidia-smi --query-gpu=... --format=csv,noheader,nounits` line."""
    parts = [p.strip() for p in line.split(",")]
    if len(parts) < 5:
        return None
    try:
        used, total = float(parts[3]), float(parts[4])
        out = {"gpu": float(parts[0]), "gpu_t": float(parts[1]),
               "gpu_w": float(parts[2]) if parts[2] not in ("[N/A]", "N/A", "[Not Supported]") else -1,
               "vram": round(100.0 * used / total, 1) if total else -1}
    except ValueError:
        return None
    if len(parts) >= 6:
        out["gpu_model"] = short_gpu(",".join(parts[5:]))
    return out


class NvidiaStream(threading.Thread):
    QUERY = "utilization.gpu,temperature.gpu,power.draw,memory.used,memory.total,name"

    def __init__(self, exe: str):
        super().__init__(name="nvidia-smi", daemon=True)
        self.exe = exe
        self.latest: dict | None = None
        self.latest_at = 0.0
        self._halt = threading.Event()
        self._proc = None

    def run(self) -> None:
        kw = {"creationflags": NO_WINDOW} if sys.platform == "win32" else {}
        while not self._halt.is_set():
            try:
                self._proc = subprocess.Popen(  # noqa: S603 - fixed argv, exe from PATH
                    [self.exe, f"--query-gpu={self.QUERY}", "--format=csv,noheader,nounits", "-lms", "1000"],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, **kw)
            except OSError as e:
                log.info("nvidia-smi unavailable (%s): GPU stats off", e)
                return
            for line in self._proc.stdout:  # type: ignore[union-attr]
                d = parse_nvidia_line(line)
                if d:
                    self.latest, self.latest_at = d, time.time()
                if self._halt.is_set():
                    break
            if not self._halt.is_set():
                log.info("nvidia-smi exited; restarting in 10s")
                self._halt.wait(10)

    def stop(self) -> None:
        self._halt.set()
        if self._proc:
            try:
                self._proc.terminate()
            except OSError:
                pass


class PcCollector(Loop):
    """`name` is the user's "PC name" (HUD settings) or the cleaned-up host
    name; cpu_model / gpu_model are short model names ("" when unknown)."""

    def __init__(self, audio=None, settings=None, name: str | None = None):
        super().__init__("pc", 1.0)
        import psutil  # required dependency
        self._ps = psutil
        self.audio = audio
        self.settings = settings
        self.hostname = host_name()
        self._fixed_name = name
        self.cpu_model = short_cpu(cpu_brand())
        exe = shutil.which("nvidia-smi")
        self._nv = NvidiaStream(exe) if exe else None
        self._net_prev = None
        self._disk = system_disk()
        self._data: dict | None = None
        self._temp_at = 0.0
        self._temp = -1.0
        self._lhm = LhmPoller() if sys.platform == "win32" else None
        psutil.cpu_percent(None)  # prime

    def start(self) -> None:
        if self._nv:
            self._nv.start()
        if self._lhm:
            self._lhm.start()
        super().start()

    def step(self) -> None:
        ps = self._ps
        now = time.time()
        net = ps.net_io_counters()
        down = up = -1.0
        if self._net_prev and net:
            dt = max(now - self._net_prev[0], 0.1)
            down = (net.bytes_recv - self._net_prev[1]) * 8 / dt / 1e6
            up = (net.bytes_sent - self._net_prev[2]) * 8 / dt / 1e6
        if net:
            self._net_prev = (now, net.bytes_recv, net.bytes_sent)
        if self._lhm:
            self._temp = self._lhm.value()
        elif now - self._temp_at > 5:
            self._temp, self._temp_at = cpu_temp(), now
        try:
            disk = round(ps.disk_usage(self._disk).percent, 1)
        except OSError:
            disk = -1
        d = {
            "name": self.display_name(),
            "cpu_model": self.cpu_model,
            "gpu_model": "",
            "cpu": round(ps.cpu_percent(None), 1),
            "ram": round(ps.virtual_memory().percent, 1),
            "disk": disk,
            "down": round(down, 2), "up": round(up, 2),
            "cpu_t": self._temp,
            "gpu": -1, "gpu_t": -1, "gpu_w": -1, "vram": -1,
        }
        if self._nv and self._nv.latest and now - self._nv.latest_at < 5:
            d.update(self._nv.latest)
        if self.audio is not None:
            try:
                muted = self.audio.mic_muted()
                if muted is not None:
                    d["mic_muted"] = bool(muted)
                d["vol"] = int(self.audio.volume())
            except Exception:  # noqa: BLE001
                pass
        self._data = d

    def display_name(self) -> str:
        if self._fixed_name:
            return self._fixed_name
        custom = ((self.settings.section("hud") or {}).get("pc_name") if self.settings else "") or ""
        return ascii_label(custom, 31) or self.hostname

    def snapshot(self) -> dict | None:
        return dict(self._data) if self._data else None

    def stop(self) -> None:
        super().stop()
        if self._nv:
            self._nv.stop()
        if self._lhm:
            self._lhm.stop()
