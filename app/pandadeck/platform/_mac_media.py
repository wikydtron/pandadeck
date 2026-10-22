"""Now playing on macOS: Spotify and Apple Music via osascript.

A player is only scripted when its process is already running (pgrep), so
we never launch it, and never compile a script against an app that isn't
installed. One osascript call per running player per poll; numbers come
back as integer milliseconds so the user's decimal separator can't bite.
Covers: Spotify's `artwork url` (fetched over https), Music's raw artwork
data (written to a temp file by AppleScript). The first call triggers the
macOS Automation consent prompt for each player.
"""
from __future__ import annotations

import logging
import os
import shutil
import tempfile
import threading
import time

from ._util import ArtCache, choose_player, clean, fetch_url, parse_seek, read_limited, run, to_float
from .base import NowPlaying

log = logging.getLogger("pandadeck.platform.mac")

PLAYERS = ("Spotify", "Music")
SEP = "\x1f"
FIELDS = ("state", "title", "artist", "album", "dur_ms", "pos_ms", "shuffle", "repeat", "art_url", "track_id")
OSA_TIMEOUT_S = 4.0
FAIL_BACKOFF_S = 15.0
STATE_MAP = {"playing": "Playing", "paused": "Paused", "stopped": "Stopped",
             "fast forwarding": "Playing", "rewinding": "Playing"}
MUSIC_REPEAT = {"off": 0, "one": 1, "all": 2}


def _script(app: str, dur_expr: str, shuffle_expr: str, repeat_expr: str, art_expr: str) -> str:
    """Status script: one line of FIELDS joined by \\x1f (missing values -> "")."""
    def field(var: str, expr: str, default: str = '""') -> str:
        return f"\tset {var} to {default}\n\ttry\n\t\tset {var} to {expr}\n\tend try\n"

    return (
        "set sep to character id 31\n"
        # It may have quit since pgrep: bail out rather than relaunch it.
        f'if not (application "{app}" is running) then return "stopped"\n'
        f'tell application "{app}"\n'
        "\tset st to (player state as text)\n"
        '\tif st is "stopped" then return st\n'
        "\ttry\n\t\tset t to current track\n\ton error\n\t\treturn st\n\tend try\n"
        + field("nm", "(name of t) as text")
        + field("ar", "(artist of t) as text")
        + field("al", "(album of t) as text")
        + field("d", "duration of t", "0")
        + field("ppos", "player position", "0")
        + field("sh", f"({shuffle_expr}) as text")
        + field("rp", f"({repeat_expr}) as text")
        + field("au", art_expr)
        + field("tid", "(id of t) as text" if app == "Spotify" else "(persistent ID of t) as text")
        + "end tell\n"
        'set dms to "0"\ntry\n'
        f"\tset dms to (round ({dur_expr})) as text\n"
        "end try\n"
        'set pms to "0"\ntry\n\tset pms to (round (ppos * 1000)) as text\nend try\n'
        "return st & sep & nm & sep & ar & sep & al & sep & dms & sep & pms & sep & sh & sep & rp"
        " & sep & au & sep & tid\n"
    )


SCRIPTS = {
    # Spotify reports duration in ms already, Music in seconds.
    "Spotify": _script("Spotify", "d", "shuffling", "repeating", "(artwork url of t) as text"),
    "Music": _script("Music", "d * 1000", "shuffle enabled", "song repeat", '""'),
}

MUSIC_ART_SCRIPT = """on run argv
	if not (application "Music" is running) then error "Music is not running"
	set p to POSIX file (item 1 of argv)
	tell application "Music"
		set d to raw data of artwork 1 of current track
	end tell
	set f to open for access p with write permission
	try
		set eof f to 0
		write d to f
		close access f
	on error e
		close access f
		error e
	end try
end run
"""

COMMANDS = {
    "Spotify": {
        "playpause": "playpause", "next": "next track", "prev": "previous track",
        "shuffle": "set shuffling to not shuffling", "repeat": "set repeating to not repeating",
    },
    "Music": {
        "playpause": "playpause", "next": "next track", "prev": "previous track",
        "shuffle": "set shuffle enabled to not shuffle enabled",
        "repeat": ("if song repeat is off then\n set song repeat to all\n"
                   "else if song repeat is all then\n set song repeat to one\n"
                   "else\n set song repeat to off\nend if"),
    },
}


def _text(v: str) -> str:
    return "" if v.strip() == "missing value" else clean(v)


def parse_status(app: str, text: str | None) -> dict | None:
    """Pure: one status line from SCRIPTS[app] -> status dict (None if osascript failed)."""
    if text is None:
        return None
    parts = text.rstrip("\r\n").split(SEP)
    state = STATE_MAP.get(parts[0].strip().lower(), "Stopped")
    if len(parts) < len(FIELDS):
        return {"app": app, "status": state, "title": "", "artist": "", "album": "", "dur": 0.0, "pos": 0.0,
                "shuffle": -1, "repeat": -1, "art_url": "", "track_id": ""}
    f = dict(zip(FIELDS, parts))
    dur = max(0.0, to_float(f["dur_ms"]) / 1000)
    pos = max(0.0, to_float(f["pos_ms"]) / 1000)
    shuffle = {"true": 1, "false": 0}.get(f["shuffle"].strip().lower(), -1)
    rep = f["repeat"].strip().lower()
    repeat = MUSIC_REPEAT.get(rep, -1) if app == "Music" else {"true": 2, "false": 0}.get(rep, -1)
    return {"app": app, "status": state, "title": _text(f["title"]), "artist": _text(f["artist"]),
            "album": _text(f["album"]), "dur": dur, "pos": min(pos, dur) if dur > 0 else pos,
            "shuffle": shuffle, "repeat": repeat, "art_url": _text(f["art_url"]),
            "track_id": _text(f["track_id"])}


def parse_pgrep(text: str | None) -> set[str]:
    """`pgrep -lx` output ("123 Spotify") -> process names."""
    out = set()
    for line in (text or "").splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2:
            out.add(parts[1].strip())
    return out


def command_script(app: str, cmd: str) -> str | None:
    """Pure: AppleScript for a panel media command (None if unknown)."""
    if cmd.startswith("seek:"):
        secs = parse_seek(cmd)
        if secs is None:
            return None
        body = f"set player position to {secs:.3f}"
    else:
        body = COMMANDS.get(app, {}).get(cmd)
        if body is None:
            return None
    # Guarded: a `tell` to a player that has quit would launch it.
    return f'if application "{app}" is running then\ntell application "{app}"\n{body}\nend tell\nend if'


class MacMedia:
    """MediaBackend for Spotify + Apple Music."""

    def __init__(self):
        self._lock = threading.Lock()
        self._active: str | None = None
        self._current: str | None = None
        self._backoff: dict[str, float] = {}   # app -> monotonic time to try osascript again
        self._art = ArtCache()

    @classmethod
    def create(cls) -> "MacMedia | None":
        return cls() if shutil.which("osascript") else None

    def _running(self) -> set[str]:
        return parse_pgrep(run(["pgrep", "-lx", "|".join(PLAYERS)], timeout=2)) & set(PLAYERS)

    def _status(self, app: str) -> dict | None:
        """One player's status; a failing osascript (Automation denied, hung app)
        is skipped for FAIL_BACKOFF_S instead of costing OSA_TIMEOUT_S every poll."""
        if time.monotonic() < self._backoff.get(app, 0.0):
            return None
        st = parse_status(app, run(["osascript", "-e", SCRIPTS[app]], timeout=OSA_TIMEOUT_S))
        if st is None:
            self._backoff[app] = time.monotonic() + FAIL_BACKOFF_S
        return st

    def now_playing(self) -> NowPlaying | None:
        with self._lock:
            running = self._running()
            statuses = {app: st for app in PLAYERS if app in running
                        for st in [self._status(app)] if st is not None}
            name = choose_player(statuses, self._active)
            self._current = name
            if name is None:
                return None
            st = statuses[name]
            if st["status"] == "Playing":
                self._active = name
        self._update_art(name, st)  # outside the lock: a download must not block command()
        return NowPlaying(app=name, title=st["title"], artist=st["artist"], album=st["album"],
                          playing=st["status"] == "Playing", pos=st["pos"], dur=st["dur"],
                          shuffle=st["shuffle"], repeat=st["repeat"], art=self._art.image,
                          art_key=self._art.key, extra={"source": name, "track_id": st["track_id"]})

    def _update_art(self, app: str, st: dict) -> None:
        if app == "Spotify":
            url = st["art_url"]
            self._art.update(f"Spotify:{url}" if url.startswith("https://") else "", lambda: fetch_url(url))
        else:
            tid = st["track_id"]
            self._art.update(f"Music:{tid}" if tid else "", self._music_artwork)

    def _music_artwork(self) -> bytes | None:
        fd, path = tempfile.mkstemp(prefix="pandadeck-art-", suffix=".bin")
        os.close(fd)
        try:
            if run(["osascript", "-e", MUSIC_ART_SCRIPT, path], timeout=OSA_TIMEOUT_S) is None:
                return None
            return read_limited(path)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    def command(self, cmd: str) -> None:
        with self._lock:
            app = self._current or self._active
        if app is None:
            running = self._running()
            app = next((p for p in PLAYERS if p in running), None)
        if app is None:
            return
        script = command_script(app, cmd)
        if script is None:
            log.warning("unknown media command %r", cmd)
            return
        if run(["osascript", "-e", script], timeout=OSA_TIMEOUT_S) is None:
            log.warning("media command %s for %s failed", cmd, app)
