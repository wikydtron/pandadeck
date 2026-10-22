"""Now playing for the MUSIC page.

Polls the active MediaBackend (the OS media session from the platform layer,
or Spotify's Web API when signed in) once a second. Per track the panel gets
the cover (RGB565A8 icon "art") and a blurred, darkened backdrop (RGB565),
both tagged with the same hash so the panel never pairs an old cover with a
new title; an accent colour from the cover tints the controls.
"""
from __future__ import annotations

import hashlib
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from ..ai.summarize import one_line
from ..platform._web_players import SERVICE_COLORS, is_browser, service_color
from ..util import Loop
from . import images as im

log = logging.getLogger("media")

POLL_S = 1.0


def app_label(name: str) -> str:
    if name in SERVICE_COLORS:  # already a service name (a web player recognised by the platform layer)
        return name
    a = (name or "").lower()
    for key, label in (("spotify", "Spotify"), ("chrome", "Chrome"), ("msedge", "Edge"), ("firefox", "Firefox"),
                       ("zune", "Media Player"), ("vlc", "VLC"), ("youtube", "YouTube"), ("music", "Music")):
        if key in a:
            return label
    return one_line((name or "").split("!")[0].split(".")[0], 16) or "Media"


def choose(source: str, system, spotify):
    """Pure: which NowPlaying to show, as (backend_name, now_playing)."""
    if source == "spotify":
        return ("spotify", spotify) if spotify else ("", None)
    if source == "system":
        return ("system", system) if system else ("", None)
    if spotify and spotify.playing:
        return "spotify", spotify
    if system and (system.playing or not spotify):
        return "system", system
    if spotify:
        return "spotify", spotify
    return ("system", system) if system else ("", None)


def same_track(a, b) -> bool:
    return bool(a and b and a.title and " ".join(a.title.split()).casefold() == " ".join(b.title.split()).casefold())


def source_label(np, spotify_np=None) -> str:
    """Pure: the source name the panel shows. A browser whose track is what the
    signed-in Spotify account is playing is Spotify's web player."""
    if is_browser(np.app, str((np.extra or {}).get("source", ""))) and same_track(np, spotify_np):
        return "Spotify"
    return app_label(np.app)


def snapshot_block(np, art_key: str, tint: str, label: str | None = None, liked: int = -1) -> dict:
    if np is None:
        return {"ok": False}
    app = label if label is not None else app_label(np.app)
    return {
        "ok": True,
        "app": app,
        "liked": liked,      # ❤ Like: 1 in Liked Songs, 0 not, -1 = no heart (not Spotify / not signed in)
        "app_color": service_color(app),  # the panel's dot next to the source name ("" = cover tint)
        "title": one_line(np.title or "", 90),
        "artist": one_line(np.artist or "", 60),
        "album": one_line(np.album or "", 60),
        "playing": bool(np.playing),
        "pos": int(max(0.0, np.pos)),
        "dur": int(max(0.0, np.dur)),
        "shuffle": int(np.shuffle),
        "repeat": int(np.repeat),
        "art": art_key,
        "tint": tint,
    }


def render_art(cover, backdrop_src, focus_top: bool) -> tuple[bytes | None, bytes | None, str, str, tuple]:
    """(art RGB565A8, backdrop RGB565, tag, tint, backdrop size) for one track.
    An artist photo becomes a sharp backdrop; the cover a blurred one."""
    if cover is None and backdrop_src is None:
        return None, None, "", im.DEFAULT_TINT, im.BACKDROP
    art = im.encode_rgb565a8(im.art_image(cover)) if cover is not None else None
    if backdrop_src is not None:
        size = im.BACKDROP_PHOTO
        bg = im.encode_rgb565(im.photo_backdrop_image(backdrop_src, size))
    else:
        size = im.BACKDROP
        bg = im.encode_rgb565(im.backdrop_image(cover, focus_top=False))
    tint = im.tint_from(cover) if cover is not None else im.DEFAULT_TINT
    tag = hashlib.sha1((art or b"") + bg).hexdigest()[:8]
    return art, bg, tag, tint, size


class MediaCollector(Loop):
    def __init__(self, settings, system: Callable[[], object], spotify: Callable[[], object], artists=None,
                 likes=None):
        """system(): the platform MediaBackend or None; spotify(): SpotifyBackend
        when signed in, else None; artists: an ArtistImages for the "artist photo"
        backdrop when Spotify doesn't provide one (no sign-in needed)."""
        super().__init__("media", POLL_S)
        self.settings = settings
        self.artists = artists
        self.likes = likes        # music/likes.Likes (❤ Like), or None
        self.backdrop_source = ""  # "spotify" | "deezer" | "cover" | "" (for the settings page)
        self._system, self._spotify = system, spotify
        self._lock = threading.Lock()
        self._snap: dict | None = None
        self._active = None
        self.active_name = ""
        self._render_key: tuple = ()
        self._art: bytes | None = None
        self._bg: bytes | None = None
        self._tag = ""
        self._tint = im.DEFAULT_TINT
        self._cover = None
        self._bg_size = im.BACKDROP
        self._cmds = ThreadPoolExecutor(max_workers=1, thread_name_prefix="media-cmd")
        self.error = ""

    # ---- loop -----------------------------------------------------------------------
    def step(self) -> None:
        music = self.settings.section("music") or {}
        source = music.get("source", "auto")
        sysb = self._system() if source in ("auto", "system") else None
        spb = self._spotify() if source in ("auto", "spotify") else None
        sys_np = _safe_np(sysb)
        sp_np = _safe_np(spb)
        name, np = choose(source, sys_np, sp_np)
        backend = {"system": sysb, "spotify": spb}.get(name)
        if sysb is None and spb is None:
            with self._lock:
                self._snap, self._active, self.active_name = None, None, ""
            return
        if np is not None:
            self._update_art(np, music.get("backdrop", "cover"))
        with self._lock:
            self._active, self.active_name = backend, name
            liked = self._liked(name, np, sp_np) if np is not None else -1
            self._snap = (snapshot_block(np, self._tag, self._tint, source_label(np, sp_np), liked)
                          if np is not None else {"ok": False})

    def _liked(self, name: str, np, sp_np) -> int:
        """❤ state of the track on screen: 1 / 0, or -1 = no heart on the panel. Only when Spotify is
        connected AND is what's playing (the desktop app or Spotify's own source), never for a
        browser or another player."""
        if self.likes is None:
            return -1
        if name != "spotify" and "spotify" not in (getattr(np, "app", "") or "").lower():
            return -1
        hint = ""
        if name == "spotify":
            hint = (np.extra or {}).get("track_id", "")
        elif same_track(np, sp_np):
            hint = (sp_np.extra or {}).get("track_id", "")
        try:
            return self.likes.state(np.title, np.artist, hint)
        except Exception as e:  # noqa: BLE001 - never break the music page over a heart
            log.debug("like state failed: %s", e)
            return -1

    def artist_photo(self, np, style: str):
        """(image, source) for the backdrop: Spotify's artist image when signed
        in, else Deezer's public catalog, else None (blurred cover)."""
        if style != "artist":
            return None, "cover"
        if np.artist_image is not None:
            return np.artist_image, "spotify"
        if self.artists is not None and np.artist:
            img = self.artists.get(np.artist)
            if img is not None:
                return img, "deezer"
        return None, "cover"

    def _update_art(self, np, style: str) -> None:
        photo, source = self.artist_photo(np, style)
        use_artist = photo is not None
        key = (np.art_key or (np.title, np.artist, np.album), source,
               ((np.extra or {}).get("artist_id", "") or np.artist) if use_artist else "", np.art is not None)
        self.backdrop_source = source
        if key == self._render_key:
            return
        try:
            art, bg, tag, tint, size = render_art(np.art, photo, use_artist)
        except Exception as e:  # noqa: BLE001 - a broken image must not stop the page
            log.debug("cover art failed: %s", e)
            art, bg, tag, tint, size = None, None, "", im.DEFAULT_TINT, im.BACKDROP
        with self._lock:
            self._render_key = key
            self._art, self._bg, self._tag, self._tint, self._cover = art, bg, tag, tint, np.art
            self._bg_size = size

    def _refresh_like(self) -> None:
        """Show a toggled heart in the very next snapshot (optimistic)."""
        with self._lock:
            snap = self._snap
            if snap and snap.get("ok") and snap.get("liked", -1) in (0, 1):
                self._snap = {**snap, "liked": 1 - snap["liked"]}
        self.poke()

    # ---- public -----------------------------------------------------------------------
    def snapshot(self) -> dict | None:
        with self._lock:
            return dict(self._snap) if self._snap is not None else None

    def images(self) -> dict:
        """name -> (header, pixels) the panel should have."""
        with self._lock:
            out = {}
            if self._art:
                out["art"] = ({"kind": "icon", "key": "art", "w": im.ART_PX, "h": im.ART_PX, "tag": self._tag},
                              self._art)
            if self._bg:
                out["backdrop"] = ({"kind": "backdrop", "w": self._bg_size[0], "h": self._bg_size[1], "tag": self._tag},
                                   self._bg)
            return out

    def cover_png(self) -> bytes | None:
        with self._lock:
            cover = self._cover
        return im.png_bytes(cover, 160) if cover is not None else None

    def command(self, cmd: str) -> None:
        """seek:<s> | shuffle | repeat | playpause | next | prev | like (off the sync thread)."""
        if cmd == "like":
            if self.likes is not None and self.likes.toggle():
                self._refresh_like()
            return
        with self._lock:
            backend = self._active
        if backend is None:
            return

        def run():
            try:
                backend.command(cmd)
            except Exception as e:  # noqa: BLE001
                log.warning("media cmd %s failed: %s", cmd, e)
            self.poke()

        self._cmds.submit(run)


def _safe_np(backend):
    if backend is None:
        return None
    try:
        return backend.now_playing()
    except Exception as e:  # noqa: BLE001 - app closed mid-call, etc.
        log.debug("now_playing failed: %s", e)
        return None
