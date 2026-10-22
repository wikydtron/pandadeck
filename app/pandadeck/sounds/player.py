"""Play sound files on this computer, without extra dependencies.

    Windows  winmm MCI through ctypes (mciSendStringW): WAV and MP3, several at
             once, per-sound volume ("setaudio ... volume"); OGG only with a codec
    macOS    afplay -v <0..1>          (WAV, MP3; OGG via ffplay/mpv if installed)
    Linux    paplay --volume (WAV, OGG) -> aplay (WAV) -> ffplay / mpv (anything)

play(path, volume, key, mode):
    "toggle"  the soundboard: one sound at a time; another one cuts it, and
              the sound that is playing (same key) stops when tapped again
    "solo"    the settings page's preview: cuts whatever plays, never toggles
    "mix"     layered, at most MAX_AT_ONCE together (another stops the oldest)
"""
from __future__ import annotations

import logging
import queue
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable

log = logging.getLogger("sounds.player")

MAX_AT_ONCE = 4
MODES = ("mix", "solo", "toggle")
MCI_POLL_S = 0.15
MCI_MAX_PATH = 200  # MCI fails on paths near MAX_PATH: longer ones go through their 8.3 short name


def _vol(volume) -> int:
    try:
        return max(0, min(100, int(volume)))
    except (TypeError, ValueError):
        return 100


def command(platform: str, path: str, volume: int, which: Callable[[str], str | None] = shutil.which) -> list[str] | None:
    """Pure: the player command for a file on macOS / Linux, or None when nothing here can play it."""
    v = _vol(volume)
    ext = Path(path).suffix.lower().lstrip(".")
    ffplay = ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", "-volume", str(v), path]
    mpv = ["mpv", "--no-video", "--really-quiet", f"--volume={v}", path]
    if platform == "darwin":
        options = [["afplay", "-v", f"{v / 100:.2f}", path]] if ext in ("wav", "mp3", "m4a") else []
        options += [ffplay, mpv]
    else:
        paplay = ["paplay", f"--volume={round(65536 * v / 100)}", path]
        if ext == "wav":
            options = [paplay, ["aplay", "-q", path], ffplay, mpv]
        elif ext == "ogg":
            options = [paplay, ffplay, mpv]
        else:  # mp3 / m4a: libsndfile (paplay) reads mp3 only from 1.1 on, m4a never: media players first
            options = [ffplay, mpv, paplay]
    for argv in options:
        if which(argv[0]):
            return argv
    return None


def mci_open(alias: str, path: str, volume: int) -> list[str]:
    """Pure: the MCI command strings that start one sound (Windows)."""
    return [f'open "{path}" type mpegvideo alias {alias}',
            f"setaudio {alias} volume to {_vol(volume) * 10}",
            f"play {alias}"]


class _Mci:
    """All MCI calls on one thread (MCI devices belong to the thread that opened
    them); it closes each sound when it ends."""

    def __init__(self, send: Callable[[str], tuple[int, str]] | None = None):
        self._send = send or _mci_send
        self._q: queue.Queue = queue.Queue()
        self._open: list[tuple[str, str]] = []   # (alias, key), oldest first
        self._n = 0
        threading.Thread(target=self._run, name="sounds-mci", daemon=True).start()

    def play(self, path: str, volume: int, key: str = "", mode: str = "mix") -> None:
        self._q.put(("play", path, volume, key, mode))

    def stop_all(self) -> None:
        self._q.put(("stop",))

    def _run(self) -> None:
        while True:
            try:
                job = self._q.get(timeout=MCI_POLL_S if self._open else None)
            except queue.Empty:
                job = None
            try:
                if job and job[0] == "play":
                    self._start(*job[1:])
                elif job and job[0] == "stop":
                    self._close_all()
                self._reap()
            except Exception as e:  # noqa: BLE001 - never let the sound thread die
                log.warning("MCI: %s", e)

    def _start(self, path: str, volume: int, key: str = "", mode: str = "mix") -> None:
        if mode == "toggle" and key:
            mine = [a for a, k in self._open if k == key and self._playing(a)]
            if mine:                       # tapped again while it plays: that's "stop"
                self._close_all()
                return
        if mode in ("solo", "toggle"):
            self._close_all()
        if len(path) > MCI_MAX_PATH:
            path = _short_path(path)
        while len(self._open) >= MAX_AT_ONCE:
            self._close(self._open[0][0])
        self._n += 1
        alias = f"pdsnd{self._n}"
        for cmd in mci_open(alias, path, volume):
            err, msg = self._send(cmd)
            if err:
                if cmd.startswith("setaudio"):  # some devices can't change volume: play anyway
                    continue
                log.warning("couldn't play %s: %s", Path(path).name, msg or err)
                self._send(f"close {alias}")
                return
            if cmd.startswith("open"):
                self._open.append((alias, key))

    def _playing(self, alias: str) -> bool:
        err, mode = self._send(f"status {alias} mode")
        return not err and mode.strip().lower() not in ("stopped", "not ready", "")

    def _reap(self) -> None:
        for alias, _ in list(self._open):
            if not self._playing(alias):
                self._close(alias)

    def _close_all(self) -> None:
        for alias, _ in list(self._open):
            self._close(alias)

    def _close(self, alias: str) -> None:
        self._send(f"close {alias}")
        self._open = [(a, k) for a, k in self._open if a != alias]


_winmm = None


def _short_path(path: str) -> str:
    """The 8.3 form of a long Windows path (unchanged if the volume has none)."""
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetShortPathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
        k32.GetShortPathNameW.restype = wintypes.DWORD
        buf = ctypes.create_unicode_buffer(1024)
        n = k32.GetShortPathNameW(path, buf, 1024)
        return buf.value if 0 < n < 1024 else path
    except (OSError, AttributeError):
        return path


def _mci_send(cmd: str) -> tuple[int, str]:
    """mciSendStringW -> (error code, reply or error text). Windows only."""
    global _winmm
    import ctypes
    from ctypes import wintypes
    if _winmm is None:
        lib = ctypes.WinDLL("winmm")  # private instance: our argtypes stay ours
        lib.mciSendStringW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.UINT, wintypes.HANDLE]
        lib.mciSendStringW.restype = wintypes.DWORD
        lib.mciGetErrorStringW.argtypes = [wintypes.DWORD, wintypes.LPWSTR, wintypes.UINT]
        lib.mciGetErrorStringW.restype = wintypes.BOOL
        _winmm = lib
    buf = ctypes.create_unicode_buffer(256)
    err = _winmm.mciSendStringW(cmd, buf, 255, None)
    if err:
        ebuf = ctypes.create_unicode_buffer(256)
        _winmm.mciGetErrorStringW(err, ebuf, 255)
        return int(err), ebuf.value
    return 0, buf.value


class Player:
    def __init__(self, platform: str = sys.platform, which: Callable[[str], str | None] = shutil.which,
                 popen: Callable = subprocess.Popen, mci_send: Callable[[str], tuple[int, str]] | None = None):
        self.platform, self._which, self._popen = platform, which, popen
        self._mci_send = mci_send
        self._mci: _Mci | None = None
        self._procs: list[tuple[str, object]] = []   # (key, process), oldest first
        self._lock = threading.Lock()

    def play(self, path: Path | str, volume: int = 100, key: str = "", mode: str = "mix") -> bool:
        """Start a sound (see the module doc for `mode`). True when it started, or when a "toggle" tap
        stopped the sound that was playing."""
        path, mode = str(path), mode if mode in MODES else "mix"
        if self.platform == "win32":
            with self._lock:
                if self._mci is None:
                    self._mci = _Mci(self._mci_send)
            self._mci.play(path, volume, key, mode)
            return True
        with self._lock:
            self._procs = [(k, p) for k, p in self._procs if p.poll() is None]
            if mode == "toggle" and key and any(k == key for k, _ in self._procs):
                self._stop_locked()        # tapped again while it plays: that's "stop"
                return True
        argv = command(self.platform, path, volume, self._which)
        if argv is None:
            log.warning("no sound player for %s here (install ffmpeg or mpv)", Path(path).suffix)
            return False
        with self._lock:
            if mode in ("solo", "toggle"):
                self._stop_locked()
            while len(self._procs) >= MAX_AT_ONCE:
                self._kill(self._procs.pop(0)[1])
            try:
                self._procs.append((key, self._popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                                     stderr=subprocess.DEVNULL, start_new_session=True)))
            except OSError as e:
                log.warning("couldn't start %s: %s", argv[0], e)
                return False
        return True

    def stop_all(self) -> None:
        if self.platform == "win32":
            if self._mci is not None:
                self._mci.stop_all()
            return
        with self._lock:
            self._stop_locked()

    def _stop_locked(self) -> None:
        procs, self._procs = self._procs, []
        for _, p in procs:
            self._kill(p)

    @staticmethod
    def _kill(proc) -> None:
        try:
            if proc.poll() is None:
                proc.terminate()
        except OSError:
            pass
