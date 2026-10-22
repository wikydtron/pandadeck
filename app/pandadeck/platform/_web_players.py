"""Which web player a browser's media session really is.

A browser reports itself ("Google Chrome") to the OS media session, whatever
tab plays. The tab says more: Spotify's web player titles its tab
"<title> • <artist>" (U+2022), YouTube "<title> - YouTube", and so on, and the
browser shows the active tab's title in its window title. When the Spotify tab
isn't the active one, the Spotify desktop app (if it runs) still shows what the
account plays, as its window title "<artist> - <title>". On Linux, MPRIS
players can also report the page address (xesam:url).

Everything here is pure; the per-OS window-title reader is passed in.
"""
from __future__ import annotations

import re
import time
import unicodedata
import urllib.parse
from typing import Callable, Iterable

BULLET = "•"

# name -> colour of the dot the panel draws next to the source name
SERVICE_COLORS = {
    "Spotify": "#1DB954",
    "YouTube Music": "#FF0033",
    "YouTube": "#FF0033",
    "SoundCloud": "#FF5500",
    "Apple Music": "#FA2D48",
    "Deezer": "#A238FF",
    "TIDAL": "#FFFFFF",
}

# Words in a window title that name the service (after the track title); most
# specific first: "YouTube Music" before "YouTube".
TITLE_MARKERS = (
    ("YouTube Music", re.compile(r"\bYouTube Music\b", re.IGNORECASE)),
    ("YouTube", re.compile(r"\bYouTube\b", re.IGNORECASE)),
    ("SoundCloud", re.compile(r"\bSoundCloud\b", re.IGNORECASE)),
    ("Apple Music", re.compile(r"\bApple Music\b", re.IGNORECASE)),
    ("Deezer", re.compile(r"\bDeezer\b", re.IGNORECASE)),
    ("TIDAL", re.compile(r"\bTIDAL\b", re.IGNORECASE)),
)

URL_HOSTS = (  # host suffix -> service, most specific first
    ("open.spotify.com", "Spotify"),
    ("music.youtube.com", "YouTube Music"),
    ("youtube.com", "YouTube"),
    ("youtu.be", "YouTube"),
    ("soundcloud.com", "SoundCloud"),
    ("music.apple.com", "Apple Music"),
    ("deezer.com", "Deezer"),
    ("tidal.com", "TIDAL"),
)

# Browsers (by display name or Windows AppUserModelID): Chromium forks report
# "Chromium.<hash>" or their own vendor name.
BROWSER = re.compile(
    r"\b(chrome|chromium|msedge|edge|brave|opera|vivaldi|firefox|arc|yandex|waterfox|librewolf|"
    r"floorp|thorium|safari|epiphany|falkon|sidekick|duckduckgo|mullvad browser|tor browser)\b",
    re.IGNORECASE)
# Firefox registers its media session under a hex AUMID (an install-path hash);
# the Start-menu name lookup turns that into "Firefox" before we get here.

RECHECK_S = 5.0  # an unrecognised track is looked at again this often (the user may switch tabs)


def _norm(s: str) -> str:
    """Casefolded, single-spaced, accents dropped (Spotify's app may write "VËLVET-TIDE" where the web says "VELVET-TIDE")."""
    folded = unicodedata.normalize("NFKD", s or "")
    return " ".join("".join(c for c in folded if not unicodedata.combining(c)).split()).casefold()


def is_browser(app: str, source: str = "") -> bool:
    return bool(BROWSER.search(app or "") or BROWSER.search((source or "").replace(".", " ").replace("_", " ")))


def service_color(name: str) -> str:
    return SERVICE_COLORS.get(name or "", "")


def service_from_titles(window_titles: Iterable[str], title: str, artist: str = "") -> str:
    """Pure: the web player whose tab is showing in one of these window titles
    while playing `title` by `artist`, or ""."""
    t = _norm(title)
    if not t:
        return ""
    first_artist = _norm(re.split(r",|;| & | feat\. | ft\. ", artist or "")[0])
    spotify = f"{t} {BULLET} "
    spotify_app = {f"{a} - {t}" for a in (first_artist, _norm(artist)) if a}
    for raw in window_titles:
        w = _norm(raw)
        if not w:
            continue
        # Spotify web tab: "<title> • <artist(s)>" (+ " - Google Chrome" from the browser)
        if w.startswith(spotify) and (not first_artist or w[len(spotify):].startswith(first_artist)):
            return "Spotify"
        # Spotify's desktop app showing the account's playback (here: on the web player)
        if w in spotify_app:
            return "Spotify"
        if t not in w:
            continue
        rest = w[w.index(t) + len(t):]
        for name, rx in TITLE_MARKERS:
            if rx.search(rest):
                return name
    return ""


def service_from_url(url: str) -> str:
    """Pure: page address (MPRIS xesam:url) -> web player, or ""."""
    try:
        u = urllib.parse.urlsplit(url or "")
    except ValueError:
        return ""
    if u.scheme not in ("http", "https"):
        return ""
    host = (u.hostname or "").lower()
    for suffix, name in URL_HOSTS:
        if host == suffix or host.endswith("." + suffix):
            return name
    return ""


class WebPlayerLabels:
    """Per-track cache in front of service_from_titles(): a found service sticks
    for the track; a miss is re-checked every RECHECK_S."""

    def __init__(self, titles: Callable[[], list[str]], clock: Callable[[], float] = time.monotonic):
        self._titles, self._clock = titles, clock
        self._key: tuple = ()
        self._found = ""
        self._at = float("-inf")

    def label(self, app: str, source: str, title: str, artist: str, url: str = "") -> str:
        """The service's name when `app` is a browser playing a known web player, else `app`."""
        if not title or not is_browser(app, source):
            return app
        by_url = service_from_url(url)
        if by_url:
            return by_url
        key, now = (source or app, title, artist), self._clock()
        if key == self._key and (self._found or now - self._at < RECHECK_S):
            return self._found or app
        try:
            titles = list(self._titles())
        except Exception:  # noqa: BLE001 - window list unavailable: keep the browser's name
            titles = []
        self._key, self._at, self._found = key, now, service_from_titles(titles, title, artist)
        return self._found or app
