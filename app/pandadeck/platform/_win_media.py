"""Now playing on Windows from the system media session (GSMTC, via winrt).

Any app that reports to Windows (Spotify desktop, browsers, Media Player,
...) shows up, no login needed.

All winrt calls run on one long-lived asyncio loop thread; now_playing()
and command() just submit coroutines to it. winrt is imported on that
thread, lazily, so this module imports on any OS.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import threading
from typing import Callable

from ._util import art_key, clean, image_from_bytes, parse_seek
from ._web_players import WebPlayerLabels
from ._win_titles import top_level_titles
from .base import NowPlaying

log = logging.getLogger("pandadeck.platform.win")

PLAYING = 4                     # GlobalSystemMediaTransportControlsSessionPlaybackStatus.PLAYING
REPEAT_NEXT = {0: 2, 2: 1, 1: 0}  # none -> all -> one -> none (Spotify's order)
POLL_TIMEOUT_S = 3.0
START_TIMEOUT_S = 10.0
ART_REFRESH_POLLS = 3           # re-read the cover for a few polls after a track change
TICKS_PER_S = 10_000_000        # TimeSpan unit is 100 ns
MAX_ADVANCE_S = 6 * 3600        # unknown duration: never extrapolate further than this
APP_LABELS = (("spotify", "Spotify"), ("chrome", "Chrome"), ("msedge", "Edge"), ("firefox", "Firefox"),
              ("zune", "Media Player"), ("vlc", "VLC"), ("youtube", "YouTube"), ("applemusic", "Apple Music"),
              ("tidal", "TIDAL"), ("deezer", "Deezer"), ("foobar", "foobar2000"), ("musicbee", "MusicBee"))


def live_position(pos_s: float, updated: dt.datetime | None, now: dt.datetime, playing: bool,
                  dur_s: float) -> float:
    """Pure: GSMTC position is a snapshot taken at `updated` (Spotify only
    refreshes it on seeks/state changes), so advance it to `now` while playing.
    A default/epoch timestamp (some sessions never set it) isn't advanced from."""
    if playing and updated is not None:
        try:
            elapsed = (now - updated).total_seconds()
        except TypeError:  # naive vs aware datetime: leave the snapshot as-is
            elapsed = 0.0
        limit = dur_s + 60 if dur_s > 0 else MAX_ADVANCE_S
        if 0 < elapsed <= limit:
            pos_s += elapsed
    if dur_s > 0:
        pos_s = min(pos_s, dur_s)
    return max(0.0, pos_s)


def app_label(aumid: str) -> str:
    """Pure: friendly player name from the session's AppUserModelID."""
    a = (aumid or "").lower()
    for key, name in APP_LABELS:
        if key in a:
            return name
    base = (aumid or "").split("!")[0].split("\\")[-1].split(".")[0]
    return clean(base)[:16] or "Media"


def flag(v) -> int:
    """GSMTC optional bool/enum -> int, -1 when the app doesn't report it."""
    try:
        return -1 if v is None else int(v)
    except (TypeError, ValueError):
        return -1


def _is_playing(session) -> bool:
    try:
        return int(session.get_playback_info().playback_status) == PLAYING
    except Exception:  # noqa: BLE001 - session closed under us
        return False


def pick_session(current, sessions: list, is_playing) -> object | None:
    """Pure: Windows' "current" session, unless it isn't playing and another
    one is (e.g. a paused browser tab got focus while Spotify keeps playing)."""
    if current is not None and is_playing(current):
        return current
    for s in sessions:
        if is_playing(s):
            return s
    if current is not None:
        return current
    return sessions[0] if sessions else None


class WindowsMedia:
    """MediaBackend for GSMTC. Construct via create(); None if winrt is missing."""

    def __init__(self, name_for: Callable[[str], str | None] | None = None):
        self._name_for = name_for     # AUMID -> Start-menu name (optional, must not block)
        self._web = WebPlayerLabels(top_level_titles)  # "Google Chrome" playing Spotify's web player -> "Spotify"
        self._loop: asyncio.AbstractEventLoop | None = None
        self._alock: asyncio.Lock | None = None
        self._ok = False
        self._failed = False          # winrt missing/broken: this backend can never work
        self._ready = threading.Event()
        self._w: dict = {}            # winrt classes, imported on the loop thread
        self._mgr = None
        self._track: tuple | None = None
        self._art_polls = 0
        self._art = None
        self._art_key = ""
        self._last: NowPlaying | None = None
        threading.Thread(target=self._run, name="pandadeck-gsmtc", daemon=True).start()
        self._ready.wait(START_TIMEOUT_S)

    @classmethod
    def create(cls, name_for: Callable[[str], str | None] | None = None) -> "WindowsMedia | None":
        """None only when winrt is definitely unusable; a slow start still returns
        the backend (now_playing() is None until its loop is up)."""
        backend = cls(name_for)
        return None if backend._failed else backend

    def _label(self, aumid: str) -> str:
        if self._name_for is not None:
            try:
                name = self._name_for(aumid)
            except Exception:  # noqa: BLE001 - a label is never worth a failed poll
                name = None
            if name:
                return clean(name)[:24]
        return app_label(aumid)

    # ---- public (any thread) ----------------------------------------------------------
    def now_playing(self) -> NowPlaying | None:
        if not self._ok or self._loop is None:
            return None
        fut = asyncio.run_coroutine_threadsafe(self._poll(), self._loop)
        try:
            self._last = fut.result(timeout=POLL_TIMEOUT_S)
        except TimeoutError:
            fut.cancel()
            log.debug("GSMTC poll timed out; reusing the last snapshot")
        except Exception as e:  # noqa: BLE001 - player closed mid-call, etc.
            log.debug("GSMTC poll failed: %s", e)
            self._last = None
        return self._last

    def command(self, cmd: str) -> None:
        if self._ok and self._loop is not None:
            asyncio.run_coroutine_threadsafe(self._command(cmd), self._loop)

    # ---- loop thread ----------------------------------------------------------------------
    def _run(self) -> None:
        try:
            self._setup()
        except Exception as e:  # noqa: BLE001 - ImportError, or a DLL that won't load
            log.info("winrt media unavailable (%s): system media disabled", e)
            self._failed = True
            return
        finally:
            self._ready.set()
        self._loop.run_forever()  # type: ignore[union-attr]

    def _setup(self) -> None:
        from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as mgr
        from winrt.windows.storage.streams import Buffer, InputStreamOptions

        try:
            from winrt.windows.media import MediaPlaybackAutoRepeatMode as repeat_mode
        except ImportError:  # optional: repeat stays "not reported"
            repeat_mode = None
        try:  # make sure this thread is in the MTA (no-op if something already did it)
            from winrt.runtime import MTA, init_apartment
            init_apartment(MTA)
        except Exception:  # noqa: BLE001 - already initialised / older winrt
            pass
        self._w = {"mgr": mgr, "Buffer": Buffer, "opts": InputStreamOptions, "repeat": repeat_mode}
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._alock = asyncio.Lock()
        self._ok = True

    async def _session(self):
        if self._mgr is None:
            self._mgr = await self._w["mgr"].request_async()
        current = self._mgr.get_current_session()
        try:
            sessions = list(self._mgr.get_sessions())
        except Exception:  # noqa: BLE001 - collections projection missing
            sessions = []
        return pick_session(current, sessions, _is_playing)

    async def _poll(self) -> NowPlaying | None:
        async with self._alock:  # overlapping polls from several threads run one at a time
            return await self._poll_locked()

    async def _poll_locked(self) -> NowPlaying | None:
        s = await self._session()
        if s is None:
            return None
        props = await s.try_get_media_properties_async()
        aumid = s.source_app_user_model_id or ""
        track = (aumid, props.title, props.artist, props.album_title)
        if track != self._track:
            self._track = track
            self._art_polls = ART_REFRESH_POLLS  # Spotify updates the thumbnail a beat late
        if self._art_polls > 0:
            self._art_polls -= 1
            await self._fetch_art(props)
        info = s.get_playback_info()
        tl = s.get_timeline_properties()
        playing = int(info.playback_status) == PLAYING
        dur = tl.end_time.total_seconds()
        pos = live_position(tl.position.total_seconds(), tl.last_updated_time,
                            dt.datetime.now(dt.timezone.utc), playing, dur)
        repeat = -1
        if self._w.get("repeat") is not None:
            try:
                repeat = flag(info.auto_repeat_mode)
            except Exception:  # noqa: BLE001 - not reported by this app
                repeat = -1
        title, artist = clean(props.title), clean(props.artist)
        return NowPlaying(
            app=self._web.label(self._label(aumid), aumid, title, artist), title=title, artist=artist,
            album=clean(props.album_title), playing=playing, pos=pos, dur=max(0.0, dur),
            shuffle=flag(info.is_shuffle_active), repeat=repeat,
            art=self._art, art_key=self._art_key, extra={"source": aumid},
        )

    async def _fetch_art(self, props) -> None:
        data = None
        try:
            if props.thumbnail:
                stream = await props.thumbnail.open_read_async()
                try:
                    buf = self._w["Buffer"](stream.size)
                    await stream.read_async(buf, buf.capacity, self._w["opts"].READ_AHEAD)
                    data = bytes(buf)
                finally:
                    try:
                        stream.close()
                    except Exception:  # noqa: BLE001
                        pass
        except Exception as e:  # noqa: BLE001
            log.debug("cover art failed: %s", e)
        key = art_key(data)
        if key != self._art_key:
            self._art = image_from_bytes(data)
            self._art_key = key if self._art is not None else ""

    async def _command(self, cmd: str) -> None:
        try:
            s = await self._session()
            if s is None:
                return
            if cmd.startswith("seek:"):
                secs = parse_seek(cmd)
                if secs is None:
                    log.warning("bad seek command %r", cmd)
                    return
                await s.try_change_playback_position_async(int(secs * TICKS_PER_S))
            elif cmd == "shuffle":
                await s.try_change_shuffle_active_async(not s.get_playback_info().is_shuffle_active)
            elif cmd == "repeat" and self._w.get("repeat") is not None:
                cur = flag(s.get_playback_info().auto_repeat_mode)
                await s.try_change_auto_repeat_mode_async(self._w["repeat"](REPEAT_NEXT.get(cur, 2)))
            elif cmd == "playpause":
                await s.try_toggle_play_pause_async()
            elif cmd == "next":
                await s.try_skip_next_async()
            elif cmd == "prev":
                await s.try_skip_previous_async()
            else:
                log.warning("unknown media command %r", cmd)
        except Exception as e:  # noqa: BLE001
            log.warning("media command %s failed: %s", cmd, e)
