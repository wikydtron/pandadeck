"""Spotify Web API backend (optional): sign-in with the user's OWN Client ID
using OAuth "Authorization Code with PKCE" (no client secret), then
now-playing + artist photo + playback commands.

Redirect URI (the user registers it in their Spotify app):
    http://127.0.0.1:<port>/spotify/callback
Tokens live in secrets.json ("spotify"); the settings page never sees them.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import secrets as _secrets
import threading
import time
import urllib.parse
from typing import Callable

from ..platform.base import NowPlaying
from ..util import http_request

log = logging.getLogger("spotify")

AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API = "https://api.spotify.com/v1"
SCOPES = ("user-read-playback-state user-modify-playback-state user-read-currently-playing "
          "user-library-read user-library-modify")  # library: the MUSIC page's ❤ Like (Liked Songs)
LIKE_SCOPES = ("user-library-read", "user-library-modify")
PENDING_TTL_S = 600
REFRESH_MARGIN_S = 60
REPEAT_TO_INT = {"off": 0, "track": 1, "context": 2}
REPEAT_NEXT = {"off": "context", "context": "track", "track": "off"}  # Spotify's own order
POLL_PLAYING_S = 2.0
POLL_IDLE_S = 5.0


class SpotifyError(Exception):
    """Shown to the user as is."""


# ---- PKCE (pure) ------------------------------------------------------------------

def make_verifier() -> str:
    """43..128 chars of [A-Za-z0-9-._~] (RFC 7636)."""
    return _secrets.token_urlsafe(64)[:96]


def code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def authorize_url(client_id: str, redirect_uri: str, challenge: str, state: str) -> str:
    q = {"client_id": client_id, "response_type": "code", "redirect_uri": redirect_uri,
         "code_challenge_method": "S256", "code_challenge": challenge, "state": state, "scope": SCOPES}
    return f"{AUTH_URL}?{urllib.parse.urlencode(q)}"


def redirect_uri_for(port: int) -> str:
    return f"http://127.0.0.1:{port}/spotify/callback"


# ---- player JSON -> NowPlaying fields (pure) ------------------------------------------

def map_player(data: dict | None) -> dict | None:
    """GET /me/player JSON -> plain fields (None = nothing to show)."""
    if not isinstance(data, dict):
        return None
    item = data.get("item")
    if not isinstance(item, dict):
        return None
    if item.get("type") == "episode":
        artist = (item.get("show") or {}).get("name", "")
        album = (item.get("show") or {}).get("publisher", "")
        images = item.get("images") or (item.get("show") or {}).get("images") or []
        artist_id = ""
    else:
        artists = item.get("artists") or []
        artist = ", ".join(a.get("name", "") for a in artists if isinstance(a, dict) and a.get("name"))
        album = (item.get("album") or {}).get("name", "")
        images = (item.get("album") or {}).get("images") or []
        artist_id = next((a.get("id", "") for a in artists if isinstance(a, dict) and a.get("id")), "")
    art_url = pick_image(images, 640)
    return {
        "title": item.get("name", "") or "",
        "artist": artist,
        "album": album,
        "playing": bool(data.get("is_playing")),
        "pos": max(0.0, (data.get("progress_ms") or 0) / 1000.0),
        "dur": max(0.0, (item.get("duration_ms") or 0) / 1000.0),
        "shuffle": 1 if data.get("shuffle_state") else 0,
        "repeat": REPEAT_TO_INT.get(str(data.get("repeat_state") or "off"), 0),
        "repeat_state": str(data.get("repeat_state") or "off"),
        "art_url": art_url,
        "artist_id": artist_id,
        "track_id": str(item.get("id") or "") if item.get("type", "track") == "track" else "",
        "device": ((data.get("device") or {}).get("name") or ""),
    }


def pick_image(images: list, want: int) -> str:
    """Smallest image at least `want` px wide, else the largest."""
    imgs = [i for i in images or [] if isinstance(i, dict) and i.get("url")]
    if not imgs:
        return ""
    big_enough = [i for i in imgs if (i.get("width") or 0) >= want]
    if big_enough:
        return min(big_enough, key=lambda i: i.get("width") or 0)["url"]
    return max(imgs, key=lambda i: i.get("width") or 0)["url"]


def describe_error(status: int, body: bytes) -> str:
    try:
        err = (json.loads(body or b"{}").get("error") or {})
        reason, msg = err.get("reason", ""), err.get("message", "")
    except (ValueError, AttributeError):
        reason, msg = "", ""
    if status == 404 or reason == "NO_ACTIVE_DEVICE":
        return "No active Spotify device: start playing on any device first"
    if status == 403 or reason == "PREMIUM_REQUIRED":
        return "Spotify only lets Premium accounts control playback"
    if status == 429:
        return "Spotify asked us to slow down; retrying shortly"
    return f"Spotify error {status}{': ' + msg if msg else ''}"


# ---- sign-in -------------------------------------------------------------------------

class SpotifyAuth:
    def __init__(self, secrets, client_id: Callable[[], str], request=http_request, clock=time.time):
        self.secrets = secrets
        self._client_id = client_id
        self._request = request
        self._clock = clock
        self._lock = threading.Lock()
        self._pending: dict[str, tuple[str, str, str, float]] = {}  # state -> (verifier, redirect, client, t)
        self._refresh_lock = threading.Lock()
        self.last_result = ""

    def signed_in(self) -> bool:
        return bool((self.secrets.get("spotify") or {}).get("refresh_token"))

    def can_like(self) -> bool:
        """Did the user grant the Liked Songs scopes? (sign-ins before ❤ Like existed didn't)"""
        granted = str((self.secrets.get("spotify") or {}).get("scope") or "").split()
        return all(s in granted for s in LIKE_SCOPES)

    def begin(self, redirect_uri: str) -> str:
        client_id = (self._client_id() or "").strip()
        if not client_id:
            raise SpotifyError("Paste your Spotify Client ID first")
        verifier, state = make_verifier(), _secrets.token_urlsafe(16)
        now = self._clock()
        with self._lock:
            self._pending = {k: v for k, v in self._pending.items() if now - v[3] < PENDING_TTL_S}
            self._pending[state] = (verifier, redirect_uri, client_id, now)
        return authorize_url(client_id, redirect_uri, code_challenge(verifier), state)

    def finish(self, code: str, state: str) -> None:
        with self._lock:
            pending = self._pending.pop(state or "", None)
        if not pending or self._clock() - pending[3] > PENDING_TTL_S:
            raise SpotifyError("This sign-in link expired or didn't come from PandaDeck. Try again.")
        verifier, redirect_uri, client_id, _ = pending
        tok = self._token({"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
                           "client_id": client_id, "code_verifier": verifier})
        self._store(tok, client_id, keep_refresh="")
        self.last_result = "ok"

    def sign_out(self) -> None:
        self.secrets.clear("spotify")

    def access_token(self) -> str:
        with self._refresh_lock:  # the poller and a command must not refresh (and rotate) at once
            sp = self.secrets.get("spotify") or {}
            if not sp.get("refresh_token"):
                raise SpotifyError("Not signed in to Spotify")
            if sp.get("access_token") and self._clock() < float(sp.get("expires_at") or 0) - REFRESH_MARGIN_S:
                return sp["access_token"]
            client_id = sp.get("client_id") or self._client_id()
            tok = self._token({"grant_type": "refresh_token", "refresh_token": sp["refresh_token"],
                               "client_id": client_id})
            return self._store(tok, client_id, keep_refresh=sp["refresh_token"])

    def _token(self, form: dict) -> dict:
        body = urllib.parse.urlencode(form).encode()
        try:
            status, _, data = self._request(TOKEN_URL, method="POST", body=body, timeout=15,
                                            headers={"Content-Type": "application/x-www-form-urlencoded"},
                                            follow_redirects=False)
        except OSError as e:
            raise SpotifyError(f"Can't reach Spotify: {e}") from e
        try:
            tok = json.loads(data or b"{}")
        except ValueError:
            tok = {}
        if status != 200 or not tok.get("access_token"):
            err = tok.get("error_description") or tok.get("error") or f"HTTP {status}"
            if tok.get("error") == "invalid_grant" and form.get("grant_type") == "refresh_token":
                self.secrets.clear("spotify")  # revoked: sign in again
                raise SpotifyError("Spotify sign-in expired: please sign in again")
            raise SpotifyError(f"Spotify sign-in failed: {err}")
        return tok

    def _store(self, tok: dict, client_id: str, keep_refresh: str) -> str:
        old = self.secrets.get("spotify") or {}
        self.secrets.set("spotify", {
            "access_token": tok["access_token"],
            "refresh_token": tok.get("refresh_token") or keep_refresh,
            "expires_at": self._clock() + float(tok.get("expires_in") or 3600),
            "client_id": client_id,
            "scope": str(tok.get("scope") or (old.get("scope", "") if keep_refresh else "")),
        })
        return tok["access_token"]


# ---- the backend ------------------------------------------------------------------------

def _load_image(data: bytes):
    from PIL import Image
    img = Image.open(io.BytesIO(data))
    img.load()
    return img


class SpotifyBackend:
    """MediaBackend over the Web API. now_playing() is cheap to call every
    second: the API is polled every 2 s (5 s when idle) and the position is
    advanced locally in between."""

    def __init__(self, auth: SpotifyAuth, want_artist: Callable[[], bool] = lambda: False,
                 request=http_request, clock=time.time):
        self.auth = auth
        self.want_artist = want_artist
        self._request = request
        self._clock = clock
        self._lock = threading.Lock()
        self._last: dict | None = None
        self._last_at = 0.0
        self._next_poll = 0.0
        self._art: tuple[str, object] = ("", None)
        self._artists: dict[str, object] = {}
        self.error = ""
        self.profile_name = ""

    # ---- API plumbing ---------------------------------------------------------
    def api(self, method: str, path: str, query: dict | None = None, timeout: float = 8.0) -> tuple[int, bytes]:
        """One Web API call -> (status, body); for music/likes.py."""
        return self._api(method, path, query, timeout)

    def _api(self, method: str, path: str, query: dict | None = None, timeout: float = 8.0) -> tuple[int, bytes]:
        url = API + path + (f"?{urllib.parse.urlencode(query)}" if query else "")
        token = self.auth.access_token()
        status, headers, data = self._request(url, method=method, timeout=timeout,
                                              headers={"Authorization": f"Bearer {token}"},
                                              body=b"" if method in ("PUT", "POST") else None,
                                              follow_redirects=False)
        if status == 429:
            retry = float((headers or {}).get("Retry-After") or 5)
            self._next_poll = self._clock() + min(max(retry, 1.0), 120.0)
        return status, data

    def _image(self, url: str):
        if not url or not url.startswith("https://"):
            return None
        status, _, data = self._request(url, timeout=10)
        if status != 200 or not data:
            return None
        try:
            return _load_image(data)
        except Exception:  # noqa: BLE001 - not an image
            return None

    def profile(self) -> str:
        try:
            status, data = self._api("GET", "/me")
            if status == 200:
                self.profile_name = (json.loads(data or b"{}").get("display_name") or "")[:60]
        except (SpotifyError, OSError, ValueError):
            pass
        return self.profile_name

    # ---- MediaBackend -------------------------------------------------------------
    def poll(self) -> None:
        if self._clock() < self._next_poll:
            return
        try:
            status, data = self._api("GET", "/me/player", {"additional_types": "episode"})
        except SpotifyError as e:
            self.error = str(e)
            self._next_poll = self._clock() + 30
            return
        except OSError as e:
            self.error = f"Can't reach Spotify: {e}"
            self._next_poll = self._clock() + POLL_IDLE_S
            return
        if status == 204 or (status == 200 and not data):
            fields = None
            self.error = ""
        elif status == 200:
            try:
                fields = map_player(json.loads(data))
            except ValueError:
                fields = None
            self.error = ""
        else:
            self.error = describe_error(status, data)
            fields = self._last
        with self._lock:
            self._last, self._last_at = fields, self._clock()
        if self._next_poll <= self._clock():
            self._next_poll = self._clock() + (POLL_PLAYING_S if fields and fields["playing"] else POLL_IDLE_S)

    def now_playing(self) -> NowPlaying | None:
        self.poll()
        with self._lock:
            f, at = (dict(self._last) if self._last else None), self._last_at
        if not f:
            return None
        pos = f["pos"] + (self._clock() - at if f["playing"] else 0.0)
        if f["dur"] > 0:
            pos = min(pos, f["dur"])
        art = self._cover(f["art_url"])
        artist_img = self._artist(f["artist_id"]) if self.want_artist() and f["artist_id"] else None
        return NowPlaying(app="Spotify", title=f["title"], artist=f["artist"], album=f["album"],
                          playing=f["playing"], pos=pos, dur=f["dur"], shuffle=f["shuffle"], repeat=f["repeat"],
                          art=art, art_key=f["art_url"], artist_image=artist_img,
                          extra={"device": f["device"], "artist_id": f["artist_id"], "track_id": f.get("track_id", "")})

    def _cover(self, url: str):
        if self._art[0] == url and self._art[1] is not None:
            return self._art[1]
        img = self._image(url)  # a failed download is retried on the next poll, not cached
        self._art = (url, img)
        return img

    def _artist(self, artist_id: str):
        if artist_id in self._artists:
            return self._artists[artist_id]
        img = None
        try:
            status, data = self._api("GET", f"/artists/{urllib.parse.quote(artist_id)}")
            if status == 200:
                img = self._image(pick_image(json.loads(data or b"{}").get("images") or [], 640))
        except (SpotifyError, OSError, ValueError) as e:
            log.debug("artist image failed: %s", e)
        if len(self._artists) > 32:
            self._artists.clear()
        self._artists[artist_id] = img
        return img

    def command(self, cmd: str) -> None:
        with self._lock:
            cur = dict(self._last) if self._last else {}
        try:
            if cmd == "playpause":
                path = "/me/player/pause" if cur.get("playing") else "/me/player/play"
                status, data = self._api("PUT", path)
                if status in (200, 202, 204):
                    self._patch(playing=not cur.get("playing"))
            elif cmd in ("next", "prev"):
                status, data = self._api("POST", "/me/player/next" if cmd == "next" else "/me/player/previous")
            elif cmd.startswith("seek:"):
                secs = max(0.0, float(cmd.split(":", 1)[1]))
                status, data = self._api("PUT", "/me/player/seek", {"position_ms": int(secs * 1000)})
                if status in (200, 202, 204):
                    self._patch(pos=secs)
            elif cmd == "shuffle":
                new = not cur.get("shuffle")
                status, data = self._api("PUT", "/me/player/shuffle", {"state": "true" if new else "false"})
                if status in (200, 202, 204):
                    self._patch(shuffle=1 if new else 0)
            elif cmd == "repeat":
                nxt = REPEAT_NEXT.get(cur.get("repeat_state", "off"), "context")
                status, data = self._api("PUT", "/me/player/repeat", {"state": nxt})
                if status in (200, 202, 204):
                    self._patch(repeat=REPEAT_TO_INT[nxt], repeat_state=nxt)
            else:
                log.debug("unknown media command %r", cmd)
                return
        except (SpotifyError, OSError, ValueError) as e:
            self.error = str(e)
            return
        if status not in (200, 202, 204):
            self.error = describe_error(status, data)
            log.info("spotify %s: %s", cmd, self.error)
        else:
            self.error = ""
        self._next_poll = self._clock() + 0.4  # pick up the new state quickly

    def _patch(self, **kw) -> None:
        with self._lock:
            if not self._last:
                return
            now = self._clock()
            if "playing" in kw and "pos" not in kw and self._last["playing"]:
                kw["pos"] = self._last["pos"] + (now - self._last_at)  # freeze where it is
            if "pos" in kw or "playing" in kw:
                kw.setdefault("pos", self._last["pos"] + ((now - self._last_at) if self._last["playing"] else 0))
                self._last_at = now
            self._last.update(kw)
