"""❤ Like: Liked Songs state for whatever is playing, with mocked Spotify HTTP:
currently-playing match, strict search fallback, contains cache, save /
remove, optimistic toggle with revert, scopes, and the panel commands."""
import json
import threading

from pandadeck.actions import Actions
from pandadeck.music import likes as lk
from pandadeck.music import spotify as sp
from pandadeck.music.collector import MediaCollector
from pandadeck.platform.base import NowPlaying


class Sync:
    def submit(self, fn, *args):
        fn(*args)


class Auth:
    def __init__(self, signed=True, can=True):
        self.signed, self.can = signed, can

    def signed_in(self):
        return self.signed

    def can_like(self):
        return self.can


def track(tid, name, *artists):
    return {"type": "track", "id": tid, "name": name, "artists": [{"name": a} for a in artists]}


class FakeApi:
    def __init__(self, playing=None, search=(), liked=(), fail_save=False):
        self.playing, self.search, self.liked = playing, list(search), set(liked)
        self.fail_save = fail_save
        self.calls = []

    def __call__(self, method, path, query=None, timeout=8.0):
        self.calls.append((method, path, dict(query or {})))
        if path == "/me/player/currently-playing":
            return (200, json.dumps({"item": self.playing}).encode()) if self.playing else (204, b"")
        if path == "/search":
            return 200, json.dumps({"tracks": {"items": self.search}}).encode()
        if path == "/me/tracks/contains":
            return 200, json.dumps([query["ids"] in self.liked]).encode()
        if path == "/me/tracks":
            if self.fail_save:
                return 403, b'{"error": {"status": 403}}'
            (self.liked.add if method == "PUT" else self.liked.discard)(query["ids"])
            return 200, b""
        return 404, b""


def test_matching_is_strict_but_forgiving_about_case_and_feats():
    assert lk.norm("Lantern Light (feat. Somebody)") == "lantern light" and lk.norm("Déjà Vu!") == "deja vu"
    assert lk.first_artist("DJ-K, Paper Satellites") == "dj k"
    assert lk.item_matches(track("1", "Lantern Light", "Glass Harbor"), "lantern light", "Glass Harbor")
    assert lk.item_matches(track("1", "Out Of The Blue Hour", "DJ-K", "Paper Satellites"), "Out of the Blue Hour",
                           "Paper Satellites")  # any of the track's artists
    assert not lk.item_matches(track("1", "Lantern Light 88", "Glass Harbor"), "Lantern Light", "Glass Harbor")
    assert not lk.item_matches(track("1", "Lantern Light", "Velvet Tide"), "Lantern Light", "Glass Harbor")  # a cover
    assert not lk.item_matches({"type": "episode", "id": "e", "name": "Lantern Light"}, "Lantern Light", "")
    assert lk.search_query('Say "Hi" (feat. X)', "A, B") == 'track:"Say  Hi" artist:"A"'


def test_currently_playing_then_contains_and_cache():
    api = FakeApi(playing=track("t1", "Lantern Light", "Glass Harbor"), liked={"t1"})
    now = [1000.0]
    likes = lk.Likes(Auth(), api, clock=lambda: now[0], executor=Sync())
    assert likes.state("Lantern Light", "Glass Harbor") == -1          # looked up in the background
    assert likes.state("Lantern Light", "Glass Harbor") == 1
    assert [c[1] for c in api.calls] == ["/me/player/currently-playing", "/me/tracks/contains"]
    likes.state("Lantern Light", "Glass Harbor")
    assert len(api.calls) == 2                                     # cached
    now[0] += lk.REFRESH_S + 1
    likes.state("Lantern Light", "Glass Harbor")
    assert api.calls[-1][1] == "/me/tracks/contains" and len(api.calls) == 3  # refreshed after 60 s


def test_search_fallback_when_something_else_plays():
    api = FakeApi(playing=track("x", "Other Song", "Other"),
                  search=[track("cover", "Lantern Light", "Velvet Tide"), track("t9", "Lantern Light", "Glass Harbor")])
    likes = lk.Likes(Auth(), api, executor=Sync())
    likes.state("Lantern Light", "Glass Harbor")
    assert likes.state("Lantern Light", "Glass Harbor") == 0
    q = [c for c in api.calls if c[1] == "/search"][0][2]
    assert q["type"] == "track" and q["q"] == 'track:"Lantern Light" artist:"Glass Harbor"'
    api2 = FakeApi(search=[track("cover", "Lantern Light", "Velvet Tide")])
    likes2 = lk.Likes(Auth(), api2, executor=Sync())
    likes2.state("Lantern Light", "Glass Harbor")
    assert likes2.state("Lantern Light", "Glass Harbor") == -1          # no strict match: no heart
    assert not likes2.toggle()


def test_known_track_id_skips_the_lookup():
    api = FakeApi(liked=set())
    likes = lk.Likes(Auth(), api, executor=Sync())
    likes.state("Song", "Band", track_id="abc")
    assert likes.state("Song", "Band") == 0 and [c[1] for c in api.calls] == ["/me/tracks/contains"]


def test_toggle_saves_removes_and_reverts():
    api = FakeApi(liked=set())
    changes = []
    likes = lk.Likes(Auth(), api, executor=Sync(), on_change=lambda: changes.append(1))
    likes.state("Song", "Band", track_id="abc")
    assert likes.state("Song", "Band", "abc") == 0
    assert likes.toggle() and likes.state("Song", "Band", "abc") == 1 and "abc" in api.liked
    assert api.calls[-1][:2] == ("PUT", "/me/tracks") and api.calls[-1][2] == {"ids": "abc"}
    assert likes.toggle() and likes.state("Song", "Band", "abc") == 0 and api.calls[-1][0] == "DELETE"
    api.fail_save = True
    seen = []

    class Watch:  # the optimistic value is visible before Spotify answers
        def submit(self, fn, *args):
            seen.append(likes.state("Song", "Band", "abc"))
            fn(*args)

    likes._pool = Watch()
    assert likes.toggle()
    assert seen == [1] and likes.state("Song", "Band", "abc") == 0 and "403" in likes.error  # reverted
    assert changes


def test_unavailable_without_sign_in_or_scopes():
    api = FakeApi()
    assert lk.Likes(Auth(signed=False), api, executor=Sync()).state("Song", "Band") == -1
    assert lk.Likes(Auth(can=False), api, executor=Sync()).state("Song", "Band") == -1
    assert api.calls == []


def test_scopes_are_requested_and_remembered():
    assert "user-library-read" in sp.SCOPES and "user-library-modify" in sp.SCOPES
    url = sp.authorize_url("c" * 32, "http://127.0.0.1:47321/spotify/callback", "x", "s")
    assert "user-library-modify" in url.replace("+", " ").replace("%20", " ")

    class Secrets(dict):
        def get(self, k, default=None):
            return super().get(k, default)

        def set(self, k, v):
            self[k] = v

        def clear(self, k):
            self.pop(k, None)

    secrets = Secrets(spotify={"refresh_token": "r", "access_token": "a", "expires_at": 0, "client_id": "c"})
    auth = sp.SpotifyAuth(secrets, lambda: "c")
    assert auth.signed_in() and not auth.can_like()               # signed in before ❤ Like: sign in again
    auth._store({"access_token": "a2", "scope": sp.SCOPES}, "c", keep_refresh="r")
    assert auth.can_like()
    auth._store({"access_token": "a3"}, "c", keep_refresh="r")    # refreshes without "scope" keep it
    assert auth.can_like()
    assert sp.map_player({"item": track("t1", "Song", "Band"), "is_playing": True})["track_id"] == "t1"
    assert sp.map_player({"item": {"type": "episode", "id": "e1", "name": "Pod", "show": {}}})["track_id"] == ""


def test_panel_like_and_settings_commands(settings):
    toggled, opened = [], []

    class L:
        def available(self):
            return True

        def state(self, title, artist, track_id=""):
            return 1 if toggled else 0

        def toggle(self):
            toggled.append(1)
            return True

    np = NowPlaying(app="Google Chrome", title="Song", artist="Band", playing=True, extra={"source": "Chrome"})

    class Sys:
        def now_playing(self):
            return np

        def command(self, c):
            raise AssertionError("like must not reach the player")

    col = MediaCollector(settings, lambda: Sys(), lambda: None, likes=L())
    col.step()
    assert col.snapshot()["liked"] == 0
    col.command("like")
    assert toggled == [1] and col.snapshot()["liked"] == 1          # optimistic, before the next poll
    done = threading.Event()
    a = Actions(open_settings=lambda section="": opened.append(section) or done.set(), media=col)
    a.run({"k": "cmd", "a": "settings:music"})
    assert done.wait(2) and opened == ["music"]


def test_settings_page_anchor(fake_platform, monkeypatch):
    from pandadeck import service as svcmod
    urls = []
    monkeypatch.setattr(svcmod.webbrowser, "open", urls.append)
    svc = svcmod.Service(panel=False, platform=fake_platform)
    svc.port = 47399
    svc.open_settings_page("music")
    svc.open_settings_page("nope")
    assert urls == ["http://127.0.0.1:47399/#music-spotify", "http://127.0.0.1:47399/"]
