"""Now playing on Linux via MPRIS (org.mpris.MediaPlayer2.*) on the session bus.

Preferred transport: jeepney (pure-Python D-Bus, one long-lived connection,
guarded by a lock). Fallback: the `playerctl` CLI. Both map into the same
pure helpers below, which the tests exercise with fake property dicts.

Player choice per poll: a Playing player wins; otherwise the one we last saw
playing; otherwise any Paused player with a title. playerctld (a proxy that
duplicates the active player) is ignored.
"""
from __future__ import annotations

import logging
import os
import shutil
import threading
from typing import Any

from ._util import ArtCache, choose_player, clean, file_url_path, load_art_url, parse_seek, run
from ._web_players import WebPlayerLabels
from .base import NowPlaying

log = logging.getLogger("pandadeck.platform.linux")

BUS_PREFIX = "org.mpris.MediaPlayer2."
OBJ_PATH = "/org/mpris/MediaPlayer2"
ROOT_IFACE = "org.mpris.MediaPlayer2"
PLAYER_IFACE = "org.mpris.MediaPlayer2.Player"
IGNORED_PLAYERS = ("org.mpris.MediaPlayer2.playerctld",)
CALL_TIMEOUT_S = 1.0
US = 1_000_000
LOOP_TO_REPEAT = {"None": 0, "Track": 1, "Playlist": 2}
REPEAT_NEXT_LOOP = {"None": "Playlist", "Playlist": "Track", "Track": "None"}  # off -> all -> one -> off
SEP = "\x1f"
PLAYERCTL_FIELDS = ("playerInstance", "playerName", "status", "xesam:title", "xesam:artist", "xesam:album",
                    "mpris:length", "position", "shuffle", "loop", "mpris:artUrl", "xesam:url")
PLAYERCTL_FORMAT = SEP.join("{{%s}}" % f for f in PLAYERCTL_FIELDS)
SPOTIFY_OLD_ART = "https://open.spotify.com/image/"
SPOTIFY_CDN_ART = "https://i.scdn.co/image/"


# ---- pure mapping --------------------------------------------------------------------------------

def unwrap(v: Any) -> Any:
    """jeepney returns variants as (signature, value); strip them recursively."""
    if isinstance(v, tuple) and len(v) == 2 and isinstance(v[0], str) and _looks_like_sig(v[0]):
        return unwrap(v[1])
    if isinstance(v, dict):
        return {k: unwrap(x) for k, x in v.items()}
    if isinstance(v, list):
        return [unwrap(x) for x in v]
    return v


def _looks_like_sig(s: str) -> bool:
    return 0 < len(s) <= 255 and all(c in "ybnqiuxtdsogavh(){}" for c in s)


def join_artists(v: Any) -> str:
    if isinstance(v, (list, tuple)):
        return ", ".join(clean(a) for a in v if clean(a))
    return clean(v)


def _num(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def fix_art_url(url: str) -> str:
    """Older Spotify clients report a dead open.spotify.com image URL."""
    if url.startswith(SPOTIFY_OLD_ART):
        return SPOTIFY_CDN_ART + url[len(SPOTIFY_OLD_ART):]
    return url


def map_player(props: dict, identity: str = "") -> dict:
    """Unwrapped org.mpris.MediaPlayer2.Player properties -> a plain status dict."""
    md = props.get("Metadata") or {}
    if not isinstance(md, dict):
        md = {}
    shuffle = props.get("Shuffle")
    loop = props.get("LoopStatus")
    dur = _num(md.get("mpris:length")) / US
    pos = _num(props.get("Position")) / US
    if dur > 0:
        pos = min(pos, dur)
    return {
        "app": clean(identity),
        "status": str(props.get("PlaybackStatus") or "Stopped"),
        "title": clean(md.get("xesam:title")),
        "artist": join_artists(md.get("xesam:artist")),
        "album": clean(md.get("xesam:album")),
        "dur": max(0.0, dur),
        "pos": max(0.0, pos),
        "shuffle": -1 if shuffle is None else int(bool(shuffle)),
        "repeat": LOOP_TO_REPEAT.get(str(loop), -1) if loop is not None else -1,
        "loop": str(loop) if loop is not None else "",
        "art_url": fix_art_url(str(md.get("mpris:artUrl") or "")),
        "trackid": str(md.get("mpris:trackid") or ""),
        "url": str(md.get("xesam:url") or ""),  # browsers: the page playing (tells Spotify web from YouTube)
    }


def label_from_bus_name(name: str) -> str:
    """'org.mpris.MediaPlayer2.firefox.instance_1_23' -> 'Firefox'."""
    base = name[len(BUS_PREFIX):] if name.startswith(BUS_PREFIX) else name
    base = base.split(".")[0]
    return base[:1].upper() + base[1:] if base else "Media"


def parse_playerctl(text: str) -> dict[str, dict]:
    """`playerctl -a metadata --format PLAYERCTL_FORMAT` output -> {instance: status dict}."""
    out: dict[str, dict] = {}
    for line in (text or "").splitlines():
        parts = line.split(SEP)
        if len(parts) == len(PLAYERCTL_FIELDS) - 1:  # older format without the trailing xesam:url
            parts.append("")
        if len(parts) != len(PLAYERCTL_FIELDS):
            continue
        f = dict(zip(PLAYERCTL_FIELDS, parts))
        inst = f["playerInstance"] or f["playerName"]
        if not inst or BUS_PREFIX + inst in IGNORED_PLAYERS:
            continue
        shuffle = f["shuffle"].strip().lower()
        loop = f["loop"].strip()
        dur = _num(f["mpris:length"]) / US
        pos = _num(f["position"]) / US
        out[inst] = {
            "app": label_from_bus_name(f["playerName"] or inst),
            "status": f["status"].strip() or "Stopped",
            "title": clean(f["xesam:title"]),
            "artist": clean(f["xesam:artist"]),
            "album": clean(f["xesam:album"]),
            "dur": max(0.0, dur),
            "pos": max(0.0, min(pos, dur) if dur > 0 else pos),
            "shuffle": {"true": 1, "on": 1, "false": 0, "off": 0}.get(shuffle, -1),
            "repeat": LOOP_TO_REPEAT.get(loop, -1),
            "loop": loop,
            "art_url": fix_art_url(f["mpris:artUrl"].strip()),
            "trackid": "",
            "url": f["xesam:url"].strip(),
        }
    return out


# ---- transports -------------------------------------------------------------------------------------

class JeepneyBus:
    """Blocking jeepney session-bus connection (thread-safe via a lock)."""

    def __init__(self):
        import jeepney  # noqa: F401 - fail fast if missing
        from jeepney.io.blocking import open_dbus_connection

        self._open = open_dbus_connection
        self._conn = None
        self._lock = threading.Lock()
        self._identity: dict[str, str] = {}
        self._connect()

    def _connect(self):
        if self._conn is None:
            self._conn = self._open(bus="SESSION")
        return self._conn

    def _call(self, msg):
        from jeepney.wrappers import unwrap_msg

        with self._lock:
            try:
                reply = self._connect().send_and_get_reply(msg, timeout=CALL_TIMEOUT_S)
            except TimeoutError:
                raise  # one slow player: keep the connection
            except (OSError, ConnectionError):
                self._reset()  # bus went away: reconnect on the next call
                raise
            return unwrap_msg(reply)

    def _reset(self) -> None:
        try:
            if self._conn is not None:
                self._conn.close()
        except Exception:  # noqa: BLE001
            pass
        self._conn = None

    def _addr(self, name: str, iface: str):
        from jeepney import DBusAddress

        return DBusAddress(OBJ_PATH, bus_name=name, interface=iface)

    def players(self) -> list[str]:
        from jeepney.bus_messages import message_bus

        names = self._call(message_bus.ListNames())[0]
        return sorted(n for n in names if n.startswith(BUS_PREFIX) and not n.startswith(IGNORED_PLAYERS))

    def status(self, name: str) -> dict:
        from jeepney import Properties

        props = unwrap(self._call(Properties(self._addr(name, PLAYER_IFACE)).get_all())[0])
        return map_player(props, self.identity(name))

    def identity(self, name: str) -> str:
        if name not in self._identity:
            from jeepney import Properties

            try:
                ident = unwrap(self._call(Properties(self._addr(name, ROOT_IFACE)).get("Identity"))[0])
            except Exception:  # noqa: BLE001 - optional property
                ident = ""
            self._identity[name] = clean(ident) or label_from_bus_name(name)
        return self._identity[name]

    def call(self, name: str, method: str, signature: str | None = None, body: tuple = ()) -> None:
        from jeepney import new_method_call

        self._call(new_method_call(self._addr(name, PLAYER_IFACE), method, signature, body))

    def set_prop(self, name: str, prop: str, signature: str, value: Any) -> None:
        from jeepney import Properties

        self._call(Properties(self._addr(name, PLAYER_IFACE)).set(prop, signature, value))


class MprisMedia:
    """MediaBackend over MPRIS; transport = jeepney, else playerctl."""

    def __init__(self, bus: JeepneyBus | None, playerctl: str | None):
        self._bus = bus
        self._playerctl = playerctl
        self._lock = threading.Lock()
        self._active: str | None = None
        self._current: dict | None = None
        self._art = ArtCache()
        self._web = WebPlayerLabels(lambda: [])  # browsers: by page address (xesam:url) when they report it

    @classmethod
    def create(cls) -> "MprisMedia | None":
        bus = None
        if os.environ.get("DBUS_SESSION_BUS_ADDRESS") or os.environ.get("XDG_RUNTIME_DIR"):
            try:
                bus = JeepneyBus()
            except Exception as e:  # noqa: BLE001 - jeepney missing / no session bus
                log.info("MPRIS over jeepney unavailable (%s)", e)
        playerctl = shutil.which("playerctl")
        if bus is None and playerctl is None:
            return None
        return cls(bus, playerctl)

    # ---- status -----------------------------------------------------------------------------------
    def _statuses(self) -> dict[str, dict]:
        if self._bus is not None:
            try:
                names = self._bus.players()
            except Exception as e:  # noqa: BLE001 - bus trouble: try playerctl this time
                log.debug("MPRIS ListNames failed: %s", e)
            else:
                out: dict[str, dict] = {}
                for name in names:
                    try:
                        out[name] = self._bus.status(name)
                    except Exception as e:  # noqa: BLE001 - player vanished mid-poll
                        log.debug("MPRIS %s failed: %s", name, e)
                return out
        if not self._playerctl:
            return {}
        return parse_playerctl(run([self._playerctl, "-a", "metadata", "--format", PLAYERCTL_FORMAT],
                                   timeout=3) or "")

    def now_playing(self) -> NowPlaying | None:
        with self._lock:
            statuses = self._statuses()
            name = choose_player(statuses, self._active)
            if name is None:
                self._current = None
                return None
            st = statuses[name]
            if st["status"] == "Playing":
                self._active = name
            self._current = {**st, "name": name}
        self._update_art(st["art_url"])  # outside the lock: a download must not block command()
        app = self._web.label(st["app"], name, st["title"], st["artist"], url=st.get("url", ""))
        return NowPlaying(app=app, title=st["title"], artist=st["artist"], album=st["album"],
                          playing=st["status"] == "Playing", pos=st["pos"], dur=st["dur"],
                          shuffle=st["shuffle"], repeat=st["repeat"], art=self._art.image,
                          art_key=self._art.key, extra={"source": name})

    def _update_art(self, url: str) -> None:
        src = url
        if url.startswith("file://"):  # browsers rewrite the same temp file per track
            try:
                src = f"{url}@{os.stat(file_url_path(url) or '').st_mtime_ns}"
            except (OSError, ValueError):
                pass
        self._art.update(src, lambda: load_art_url(url))

    # ---- commands ----------------------------------------------------------------------------------------
    def command(self, cmd: str) -> None:
        with self._lock:
            cur = self._current
        if not cur:
            return
        if cmd.startswith("seek:") and parse_seek(cmd) is None:
            log.warning("bad seek command %r", cmd)
            return
        try:
            if self._bus is not None and cur["name"].startswith(BUS_PREFIX):
                self._bus_command(cur, cmd)
            elif self._playerctl:
                self._playerctl_command(cur, cmd)
        except Exception as e:  # noqa: BLE001
            log.warning("media command %s failed: %s", cmd, e)

    def _bus_command(self, cur: dict, cmd: str) -> None:
        name = cur["name"]
        if cmd == "playpause":
            self._bus.call(name, "PlayPause")
        elif cmd == "next":
            self._bus.call(name, "Next")
        elif cmd == "prev":
            self._bus.call(name, "Previous")
        elif cmd.startswith("seek:"):
            target = int((parse_seek(cmd) or 0.0) * US)
            if cur["trackid"].startswith("/"):
                self._bus.call(name, "SetPosition", "ox", (cur["trackid"], target))
            else:
                self._bus.call(name, "Seek", "x", (target - int(cur["pos"] * US),))
        elif cmd == "shuffle":
            self._bus.set_prop(name, "Shuffle", "b", not bool(cur["shuffle"] == 1))
        elif cmd == "repeat":
            self._bus.set_prop(name, "LoopStatus", "s", REPEAT_NEXT_LOOP.get(cur["loop"], "Playlist"))
        else:
            log.warning("unknown media command %r", cmd)

    def _playerctl_command(self, cur: dict, cmd: str) -> None:
        base = [self._playerctl, "-p", cur["name"]]
        if cmd == "playpause":
            run(base + ["play-pause"])
        elif cmd == "next":
            run(base + ["next"])
        elif cmd == "prev":
            run(base + ["previous"])
        elif cmd.startswith("seek:"):
            run(base + ["position", f"{parse_seek(cmd) or 0.0:.3f}"])
        elif cmd == "shuffle":
            run(base + ["shuffle", "Toggle"])
        elif cmd == "repeat":
            run(base + ["loop", REPEAT_NEXT_LOOP.get(cur["loop"], "Playlist")])
        else:
            log.warning("unknown media command %r", cmd)
