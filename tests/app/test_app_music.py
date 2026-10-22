"""Music: image conversion, the collector, Spotify PKCE + Web API mapping."""
import base64
import hashlib
import io
import json
import urllib.parse

import pytest
from PIL import Image

from pandadeck.music import images as im
from pandadeck.music import spotify as sp
from pandadeck.music.collector import MediaCollector, app_label, choose, render_art, snapshot_block
from pandadeck.platform.base import NowPlaying


# ---- images --------------------------------------------------------------------------------------

def test_tint_picks_vivid_colour_and_greyscale_fallback():
    img = Image.new("RGB", (40, 40), (10, 10, 10))
    img.paste(Image.new("RGB", (10, 10), (200, 30, 30)), (0, 0))
    t = im.tint_from(img)
    r, g, b = int(t[1:3], 16), int(t[3:5], 16), int(t[5:7], 16)
    assert r > g and r > b and r >= 140
    assert im.tint_from(Image.new("RGB", (8, 8), (120, 120, 120))) == "#D9D4C7"


def test_art_is_square_with_rounded_corners_and_encodes_to_panel_size():
    out = im.art_image(Image.new("RGB", (640, 480), (200, 40, 40)))
    assert out.size == (im.ART_PX, im.ART_PX)
    assert out.getpixel((0, 0))[3] == 0 and out.getpixel((im.ART_PX // 2, 0))[3] == 255
    data = im.encode_rgb565a8(im.art_image(Image.new("RGB", (100, 100), (0, 0, 255))))
    assert len(data) == im.ART_PX * im.ART_PX * 3
    mid = (im.ART_PX // 2) * im.ART_PX + im.ART_PX // 2
    assert data[2 * mid:2 * mid + 2] == bytes([0x1F, 0x00])


def test_backdrop_is_small_dark_darker_at_bottom():
    bg = im.backdrop_image(Image.new("RGB", (640, 640), (240, 200, 40)))
    assert bg.size == im.BACKDROP
    top, bottom = bg.getpixel((100, 2)), bg.getpixel((100, im.BACKDROP[1] - 3))
    assert max(top) < 140 and sum(bottom) < sum(top)
    assert len(im.encode_rgb565(bg)) == im.BACKDROP[0] * im.BACKDROP[1] * 2
    assert im.encode_rgb565(Image.new("RGB", (1, 1), (255, 255, 255))) == b"\xff\xff"


def test_backdrop_crops_wide_and_tall_sources():
    wide = Image.new("RGB", (1000, 100), (0, 0, 0))
    wide.paste(Image.new("RGB", (100, 100), (255, 255, 255)), (450, 0))  # bright centre
    assert im.backdrop_image(wide).size == im.BACKDROP
    tall = Image.new("RGB", (300, 900), (0, 0, 0))
    tall.paste(Image.new("RGB", (300, 300), (255, 255, 255)), (0, 0))   # bright top third (a face)
    top_bias = im.backdrop_image(tall, focus_top=True)
    centred = im.backdrop_image(tall)
    assert sum(top_bias.getpixel((100, 5))) > sum(centred.getpixel((100, 5)))


def test_trim_and_icon_encoding():
    img = Image.new("RGBA", (128, 128), (0, 0, 0, 0))
    for i in range(128):
        for p in ((i, 0), (i, 127), (0, i), (127, i)):
            img.putpixel(p, (200, 200, 200, 255))
    img.paste(Image.new("RGBA", (48, 40), (0, 174, 66, 255)), (40, 44))
    out = im.trim(img)
    assert out.size == (48, 48) and out.getchannel("A").getbbox() == (0, 4, 48, 44)
    data = im.to_rgb565a8(Image.new("RGBA", (4, 4), (255, 0, 0, 128)), px=4)
    assert len(data) == 48 and data[0] == 0x00 and data[1] == 0xF8 and data[32] == 128
    assert im.png_bytes(img, 32)[:4] == b"\x89PNG"
    assert im.trim(Image.new("RGBA", (8, 8), (0, 0, 0, 0))).size == (8, 8)  # empty stays


# ---- collector ----------------------------------------------------------------------------------

def np(title="Song", playing=True, art=None, key="", app="Spotify", artist_image=None, **kw):
    return NowPlaying(app=app, title=title, artist="Artist", album="Album", playing=playing, pos=12.7, dur=200.2,
                      shuffle=1, repeat=2, art=art, art_key=key, artist_image=artist_image, **kw)


def test_choose_source_logic():
    sysp, spp = np("sys"), np("sp")
    assert choose("system", sysp, spp) == ("system", sysp)
    assert choose("spotify", sysp, None) == ("", None)
    assert choose("auto", sysp, spp) == ("spotify", spp)
    spp_paused = np("sp", playing=False)
    assert choose("auto", sysp, spp_paused) == ("system", sysp)
    assert choose("auto", np("s", playing=False), spp_paused) == ("spotify", spp_paused)
    assert choose("auto", None, None) == ("", None)


def test_snapshot_block_format_unchanged():
    b = snapshot_block(np("x" * 200), "abcd1234", "#FF0000")
    assert set(b) == {"ok", "app", "app_color", "liked", "title", "artist", "album", "playing", "pos", "dur",
                      "shuffle", "repeat", "art", "tint"}
    assert b["liked"] == -1  # unknown unless Like says otherwise
    assert len(b["title"]) <= 90 and b["pos"] == 12 and b["dur"] == 200 and b["shuffle"] == 1 and b["repeat"] == 2
    assert snapshot_block(None, "", "") == {"ok": False}
    assert app_label("Spotify.exe") == "Spotify" and app_label("Microsoft.ZuneMusic_8w!App") == "Media Player"


def test_render_art_tags_cover_and_backdrop_together():
    cover = Image.new("RGB", (300, 300), (30, 160, 60))
    art, bg, tag, tint, size = render_art(cover, None, False)
    assert size == im.BACKDROP
    assert len(art) == im.ART_PX ** 2 * 3 and len(bg) == im.BACKDROP[0] * im.BACKDROP[1] * 2
    assert tag == hashlib.sha1(art + bg).hexdigest()[:8] and tint.startswith("#")
    artist = Image.new("RGB", (600, 900), (200, 200, 30))
    art2, bg2, tag2, _, size2 = render_art(cover, artist, True)
    # artist photo: same cover, a bigger SHARP backdrop, new tag for both
    assert art2 == art and bg2 != bg and tag2 != tag
    assert size2 == im.BACKDROP_PHOTO and len(bg2) == size2[0] * size2[1] * 2
    assert render_art(None, None, False) == (None, None, "", im.DEFAULT_TINT, im.BACKDROP)


def test_photo_backdrop_keeps_detail_unlike_the_blurred_cover():
    stripes = Image.new("RGB", (800, 800), (255, 255, 255))
    for x in range(0, 800, 8):
        stripes.paste((0, 0, 0), (x, 0, x + 4, 800))
    sharp = im.photo_backdrop_image(stripes)
    blurred = im.backdrop_image(stripes)
    def contrast(img):
        row = [sum(img.getpixel((x, 10))) for x in range(40)]
        return max(row) - min(row)
    assert contrast(sharp) > 3 * contrast(blurred)


class Backend:
    def __init__(self, now=None):
        self.now, self.cmds = now, []

    def now_playing(self):
        return self.now

    def command(self, c):
        self.cmds.append(c)


def test_collector_snapshot_images_and_commands(settings):
    cover = Image.new("RGB", (64, 64), (200, 30, 30))
    sysb = Backend(np("Local", art=cover, key="k1", app="Chrome"))
    col = MediaCollector(settings, lambda: sysb, lambda: None)
    col.step()
    snap = col.snapshot()
    assert snap["ok"] and snap["title"] == "Local" and snap["app"] == "Chrome" and len(snap["art"]) == 8
    imgs = col.images()
    assert imgs["art"][0] == {"kind": "icon", "key": "art", "w": 248, "h": 248, "tag": snap["art"]}
    assert imgs["backdrop"][0]["tag"] == snap["art"] and imgs["backdrop"][0]["w"] == 200
    assert col.cover_png()[:4] == b"\x89PNG"
    col.command("next")
    col._cmds.shutdown(wait=True)
    assert sysb.cmds == ["next"]
    sysb.now = None
    col.step()
    assert col.snapshot() == {"ok": False}


def test_collector_without_any_backend_omits_the_block(settings):
    col = MediaCollector(settings, lambda: None, lambda: None)
    col.step()
    assert col.snapshot() is None and col.images() == {}


def test_artist_backdrop_when_chosen(settings):
    settings.update({"music": {"backdrop": "artist"}})
    cover = Image.new("RGB", (64, 64), (10, 10, 200))
    artist = Image.new("RGB", (64, 64), (250, 250, 250))
    spb = Backend(np("S", art=cover, key="u1", artist_image=artist, extra={"artist_id": "a1"}))
    col = MediaCollector(settings, lambda: None, lambda: spb)
    col.step()
    bg_artist = col.images()["backdrop"][1]
    settings.update({"music": {"backdrop": "cover"}})
    col.step()
    assert col.images()["backdrop"][1] != bg_artist


# ---- Spotify: PKCE --------------------------------------------------------------------------------

def test_pkce_rfc7636_vector_and_verifier_shape():
    assert sp.code_challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk") == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    v = sp.make_verifier()
    assert 43 <= len(v) <= 128 and all(c.isalnum() or c in "-._~" for c in v)
    digest = base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()
    assert sp.code_challenge(v) == digest


def test_authorize_url():
    url = sp.authorize_url("cid", "http://127.0.0.1:47321/spotify/callback", "chal", "st")
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    assert q["client_id"] == ["cid"] and q["response_type"] == ["code"] and q["code_challenge_method"] == ["S256"]
    assert q["redirect_uri"] == ["http://127.0.0.1:47321/spotify/callback"] and "user-modify-playback-state" in q["scope"][0]
    assert sp.redirect_uri_for(47399) == "http://127.0.0.1:47399/spotify/callback"


class TokenEndpoint:
    def __init__(self, replies):
        self.replies, self.forms = list(replies), []

    def __call__(self, url, **kw):
        assert url == sp.TOKEN_URL and kw["method"] == "POST"
        self.forms.append(dict(urllib.parse.parse_qsl(kw["body"].decode())))
        status, body = self.replies.pop(0)
        return status, {}, json.dumps(body).encode()


def test_sign_in_flow_and_refresh(fake_secrets):
    clock = [1000.0]
    ep = TokenEndpoint([(200, {"access_token": "A1", "refresh_token": "R1", "expires_in": 3600}),
                        (200, {"access_token": "A2", "expires_in": 3600}),
                        (400, {"error": "invalid_grant"})])
    auth = sp.SpotifyAuth(fake_secrets, lambda: "cid", request=ep, clock=lambda: clock[0])
    url = auth.begin("http://127.0.0.1:47321/spotify/callback")
    state = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["state"][0]
    with pytest.raises(sp.SpotifyError):
        auth.finish("code", "forged-state")
    auth.finish("thecode", state)
    form = ep.forms[0]
    assert form["grant_type"] == "authorization_code" and form["code"] == "thecode" and form["client_id"] == "cid"
    assert sp.code_challenge(form["code_verifier"]) in url
    assert fake_secrets["spotify"]["refresh_token"] == "R1" and auth.signed_in()
    with pytest.raises(sp.SpotifyError):
        auth.finish("thecode", state)  # one-shot state
    assert auth.access_token() == "A1"
    clock[0] += 3600
    assert auth.access_token() == "A2" and fake_secrets["spotify"]["refresh_token"] == "R1"  # kept when not rotated
    assert ep.forms[1] == {"grant_type": "refresh_token", "refresh_token": "R1", "client_id": "cid"}
    clock[0] += 3600
    with pytest.raises(sp.SpotifyError, match="sign in again"):
        auth.access_token()
    assert not auth.signed_in()


def test_begin_needs_client_id(fake_secrets):
    with pytest.raises(sp.SpotifyError):
        sp.SpotifyAuth(fake_secrets, lambda: "").begin("http://x")


# ---- Spotify: player mapping + commands ------------------------------------------------------------

PLAYER = {
    "is_playing": True, "progress_ms": 61500, "shuffle_state": True, "repeat_state": "context",
    "device": {"name": "Kitchen speaker"},
    "item": {"type": "track", "name": "Copper Skies", "duration_ms": 734000,
             "artists": [{"name": "VELVET TIDE", "id": "art1"}, {"name": "Guest", "id": "art2"}],
             "album": {"name": "Harbor Lights", "images": [{"url": "https://i/640", "width": 640},
                                                            {"url": "https://i/300", "width": 300}]}},
}


def test_map_player():
    f = sp.map_player(PLAYER)
    assert f["title"] == "Copper Skies" and f["artist"] == "VELVET TIDE, Guest" and f["album"] == "Harbor Lights"
    assert f["pos"] == 61.5 and f["dur"] == 734.0 and f["shuffle"] == 1 and f["repeat"] == 2
    assert f["art_url"] == "https://i/640" and f["artist_id"] == "art1" and f["device"] == "Kitchen speaker"
    ep = sp.map_player({"is_playing": False, "item": {"type": "episode", "name": "Ep", "duration_ms": 1000,
                                                      "show": {"name": "Pod", "publisher": "Pub", "images": []}}})
    assert ep["artist"] == "Pod" and ep["art_url"] == "" and ep["repeat"] == 0
    assert sp.map_player({"item": None}) is None and sp.map_player(None) is None
    assert sp.pick_image([{"url": "a", "width": 64}, {"url": "b", "width": 300}], 640) == "b"


def png(color):
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(buf, "PNG")
    return buf.getvalue()


class FakeApi:
    """Spotify Web API + image CDN."""

    def __init__(self):
        self.calls = []
        self.player = (200, json.dumps(PLAYER).encode())
        self.command_status = 204

    def __call__(self, url, method="GET", headers=None, body=None, timeout=0, follow_redirects=True):
        self.calls.append((method, url))
        if url.startswith("https://i/"):
            return 200, {}, png((10, 20, 30))
        if "/me/player?" in url:
            return self.player[0], {}, self.player[1]
        if "/artists/" in url:
            return 200, {}, json.dumps({"images": [{"url": "https://i/artist", "width": 640}]}).encode()
        if url.endswith("/me"):
            return 200, {}, b'{"display_name":"Alex"}'
        return self.command_status, {"Retry-After": "7"}, b'{"error":{"reason":"NO_ACTIVE_DEVICE"}}' \
            if self.command_status == 404 else b""


class StaticAuth:
    def access_token(self):
        return "TOKEN"


def test_backend_now_playing_and_local_position():
    api, clock = FakeApi(), [100.0]
    be = sp.SpotifyBackend(StaticAuth(), want_artist=lambda: True, request=api, clock=lambda: clock[0])
    n = be.now_playing()
    assert n.title == "Copper Skies" and n.playing and n.pos == 61.5 and n.art is not None and n.art_key == "https://i/640"
    assert n.artist_image is not None and n.app == "Spotify"
    clock[0] += 1.0  # before the next poll: position advances locally, no new API call
    before = len(api.calls)
    assert be.now_playing().pos == pytest.approx(62.5) and len(api.calls) == before
    clock[0] += 5
    be.now_playing()
    assert sum(1 for m, u in api.calls if u.startswith("https://i/640")) == 1  # cover fetched once
    api.player = (204, b"")
    clock[0] += 10
    assert be.now_playing() is None
    assert be.profile() == "Alex"


def test_backend_commands_map_to_the_player_api():
    api = FakeApi()
    be = sp.SpotifyBackend(StaticAuth(), request=api, clock=lambda: 100.0)
    be.now_playing()
    for c in ("playpause", "next", "prev", "seek:90.5", "shuffle", "repeat"):
        be.command(c)
    cmds = [(m, u.replace(sp.API, "")) for m, u in api.calls if "/me/player/" in u]
    assert cmds == [("PUT", "/me/player/pause"), ("POST", "/me/player/next"), ("POST", "/me/player/previous"),
                    ("PUT", "/me/player/seek?position_ms=90500"), ("PUT", "/me/player/shuffle?state=false"),
                    ("PUT", "/me/player/repeat?state=track")]
    assert be.error == ""


def test_backend_reports_no_device_and_premium():
    api = FakeApi()
    be = sp.SpotifyBackend(StaticAuth(), request=api, clock=lambda: 100.0)
    be.now_playing()
    api.command_status = 404
    be.command("next")
    assert "No active Spotify device" in be.error
    api.command_status = 403
    be.command("next")
    assert "Premium" in be.error
    assert "slow down" in sp.describe_error(429, b"")


def test_backend_backs_off_on_429():
    api, clock = FakeApi(), [100.0]
    api.player = (429, b"")
    be = sp.SpotifyBackend(StaticAuth(), request=api, clock=lambda: clock[0])

    def counting(url, **kw):
        r = FakeApi.__call__(api, url, **kw)
        return (r[0], {"Retry-After": "30"}, r[2]) if "/me/player?" in url else r

    be._request = counting
    be.now_playing()
    n = len(api.calls)
    clock[0] += 10
    be.now_playing()
    assert len(api.calls) == n  # still waiting for Retry-After
