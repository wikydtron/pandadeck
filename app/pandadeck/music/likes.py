"""❤ Like: the playing track in the user's Spotify "Liked Songs".

Works whatever plays the music (Spotify's app, its web player in a browser,
...), as long as the user signed in to Spotify with the library scopes:

    track id   the Spotify item playing (GET /me/player/currently-playing) when
               it is the same song; else GET /search for a strict title + artist
               match; else none (the heart shows "unavailable")
    liked?     GET /me/tracks/contains?ids=<id>, per track id, refreshed on a
               track change and every REFRESH_S
    toggle     PUT / DELETE /me/tracks?ids=<id>; the new state shows at once and
               is reverted if Spotify refuses

state() never blocks: lookups run on one background thread and the next
snapshot picks the answer up (on_change() is called when one lands).
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

log = logging.getLogger("likes")

REFRESH_S = 60.0
MAX_CACHE = 256
UNKNOWN = -1

_FEAT = re.compile(r"\s*[\(\[]\s*(?:feat|ft|with)\.?\s[^\)\]]*[\)\]]|\s+(?:feat|ft)\.?\s.*$", re.IGNORECASE)


def norm(text: str) -> str:
    """Pure: 'Lantern Light (feat. X)' -> 'lantern light'; accents, case and punctuation ignored."""
    t = _FEAT.sub("", text or "")
    t = unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode().casefold()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", t).split())


def first_artist(artist: str) -> str:
    return norm(re.split(r",|;| & | x | feat\. | ft\. ", artist or "", maxsplit=1)[0])


def item_matches(item, title: str, artist: str) -> bool:
    """Pure: is this Spotify track item the song `title` by `artist` (strict, normalized)?"""
    if not isinstance(item, dict) or item.get("type", "track") != "track" or not item.get("id"):
        return False
    if norm(item.get("name", "")) != norm(title):
        return False
    want = first_artist(artist)
    names = {norm(a.get("name", "")) for a in item.get("artists") or [] if isinstance(a, dict)}
    return not want or want in names


def search_query(title: str, artist: str) -> str:
    t = _FEAT.sub("", title or "").replace('"', " ").strip()
    a = re.split(r",|;| & | feat\. | ft\. ", artist or "", maxsplit=1)[0].replace('"', " ").strip()
    return f'track:"{t}"' + (f' artist:"{a}"' if a else "")


class Likes:
    def __init__(self, auth, api: Callable[..., tuple[int, bytes]], on_change: Callable[[], None] = lambda: None,
                 clock: Callable[[], float] = time.time, executor=None):
        """auth: SpotifyAuth (signed_in(), can_like()); api(method, path, query) -> (status, body);
        executor: anything with submit(fn, *args) (a one-thread pool by default)."""
        self.auth, self._api, self._on_change, self._clock = auth, api, on_change, clock
        self._lock = threading.Lock()
        self._ids: dict[tuple, str] = {}               # (title, artist) normalized -> track id, "" = none
        self._liked: dict[str, tuple[bool, float]] = {}  # track id -> (liked, checked at)
        self._busy: set = set()
        self._current: tuple[tuple, str] = ((), "")     # (key, track id) of the track on screen
        self._pool = executor or ThreadPoolExecutor(max_workers=1, thread_name_prefix="likes")
        self.error = ""

    def available(self) -> bool:
        return self.auth.signed_in() and self.auth.can_like()

    # ---- what the snapshot shows -----------------------------------------------------------
    def state(self, title: str, artist: str, track_id: str = "") -> int:
        """1 liked, 0 not liked, -1 unknown / unavailable (never blocks)."""
        if not title or not self.available():
            with self._lock:
                self._current = ((), "")
            return UNKNOWN
        key = (norm(title), norm(artist))
        now = self._clock()
        job = None
        with self._lock:
            if track_id:
                self._ids[key] = track_id
            tid = self._ids.get(key)
            self._current = (key, tid or "")
            liked = self._liked.get(tid) if tid else None
            if tid is None:
                job = (("id", key), self._resolve, key, title, artist)
            elif tid and (liked is None or now - liked[1] > REFRESH_S):
                job = (("check", tid), self._check, tid)
        if job:
            self._submit(*job)
        return UNKNOWN if liked is None else int(liked[0])

    def toggle(self) -> bool:
        """media:like on the track on screen; False when there's nothing to toggle."""
        with self._lock:
            tid = self._current[1]
            liked = self._liked.get(tid) if tid else None
            if liked is None:
                return False
            new = not liked[0]
            self._liked[tid] = (new, self._clock())
        self._pool.submit(self._save, tid, new, liked[0])
        self._on_change()
        return True

    # ---- background work -------------------------------------------------------------------
    def _submit(self, job: tuple, fn, *args) -> None:
        with self._lock:
            if job in self._busy:
                return
            self._busy.add(job)

        def run():
            try:
                fn(*args)
            except Exception as e:  # noqa: BLE001 - network, JSON, sign-in trouble
                self.error = str(e) or type(e).__name__
                log.info("like lookup failed: %s", self.error)
            finally:
                with self._lock:
                    self._busy.discard(job)
                self._on_change()

        self._pool.submit(run)

    def _resolve(self, key: tuple, title: str, artist: str) -> None:
        tid = ""
        status, body = self._api("GET", "/me/player/currently-playing", {"additional_types": "track"})
        if status == 200 and body:
            item = (json.loads(body) or {}).get("item")
            if item_matches(item, title, artist):
                tid = item["id"]
        if not tid:
            status, body = self._api("GET", "/search", {"type": "track", "limit": 5, "q": search_query(title, artist)})
            if status == 200:
                items = ((json.loads(body or b"{}").get("tracks") or {}).get("items")) or []
                tid = next((it["id"] for it in items if item_matches(it, title, artist)), "")
            elif status not in (400, 404):
                raise OSError(f"Spotify search answered HTTP {status}")
        with self._lock:
            if len(self._ids) > MAX_CACHE:
                self._ids.clear()
            self._ids[key] = tid
        if tid:
            self._check(tid)

    def _check(self, tid: str) -> None:
        status, body = self._api("GET", "/me/tracks/contains", {"ids": tid})
        if status != 200:
            raise OSError(f"Spotify answered HTTP {status} for Liked Songs")
        values = json.loads(body or b"[]")
        liked = bool(values[0]) if isinstance(values, list) and values else False
        with self._lock:
            if len(self._liked) > MAX_CACHE:
                self._liked.clear()
            self._liked[tid] = (liked, self._clock())
        self.error = ""

    def _save(self, tid: str, new: bool, old: bool) -> None:
        try:
            status, body = self._api("PUT" if new else "DELETE", "/me/tracks", {"ids": tid})
            ok = status in (200, 201, 204)
            if not ok:
                self.error = f"Spotify refused the change (HTTP {status})"
        except Exception as e:  # noqa: BLE001
            ok, self.error = False, str(e) or type(e).__name__
        if not ok:
            log.info("like %s -> %s failed: %s", tid, new, self.error)
            with self._lock:
                self._liked[tid] = (old, self._clock())  # revert
        else:
            self.error = ""
        self._on_change()
