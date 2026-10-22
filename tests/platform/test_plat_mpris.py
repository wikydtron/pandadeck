"""Linux MPRIS mapping + backend logic with a fake bus (runs on every OS)."""
import os

import pytest
from PIL import Image

from pandadeck.platform import _mpris as mp
from pandadeck.platform._util import choose_player

SPOTIFY = "org.mpris.MediaPlayer2.spotify"
FIREFOX = "org.mpris.MediaPlayer2.firefox.instance_1_23"


def jeepney_props(status="Playing", pos_us=61_500_000, art="https://i.scdn.co/image/abc"):
    """What jeepney's Properties.get_all() body[0] looks like: values are (signature, value)."""
    return {
        "PlaybackStatus": ("s", status),
        "LoopStatus": ("s", "Playlist"),
        "Shuffle": ("b", True),
        "Position": ("x", pos_us),
        "Metadata": ("a{sv}", {
            "mpris:trackid": ("o", "/com/spotify/track/1a2b3c4d5e6f7g8h9i0jKL"),
            "mpris:length": ("t", 231_480_000),
            "mpris:artUrl": ("s", art),
            "xesam:title": ("s", "Copper Skies"),
            "xesam:artist": ("as", ["Velvet Tide", "Guest"]),
            "xesam:album": ("s", "Harbor Lights (Expanded Edition)"),
        }),
    }


def test_unwrap_strips_variants_recursively():
    props = mp.unwrap(jeepney_props())
    assert props["PlaybackStatus"] == "Playing"
    assert props["Metadata"]["xesam:artist"] == ["Velvet Tide", "Guest"]
    assert mp.unwrap(("s", "x")) == "x" and mp.unwrap(["a", ("i", 3)]) == ["a", 3]


def test_map_player_full_metadata():
    st = mp.map_player(mp.unwrap(jeepney_props()), "Spotify")
    assert st["app"] == "Spotify" and st["status"] == "Playing"
    assert st["title"] == "Copper Skies" and st["artist"] == "Velvet Tide, Guest"
    assert st["dur"] == pytest.approx(231.48) and st["pos"] == pytest.approx(61.5)
    assert st["shuffle"] == 1 and st["repeat"] == 2 and st["loop"] == "Playlist"
    assert st["trackid"].startswith("/com/spotify/track/")


def test_map_player_missing_fields_are_unknown_not_errors():
    st = mp.map_player({"PlaybackStatus": "Paused", "Metadata": {"xesam:title": "Only title"}})
    assert st["shuffle"] == -1 and st["repeat"] == -1 and st["dur"] == 0 and st["pos"] == 0
    assert st["artist"] == "" and st["art_url"] == ""
    assert mp.map_player({})["status"] == "Stopped"
    assert mp.map_player({"Position": 9_000_000, "Metadata": {"mpris:length": 5_000_000}})["pos"] == 5.0


def test_fix_art_url_rewrites_dead_spotify_host():
    assert mp.fix_art_url("https://open.spotify.com/image/ab67") == "https://i.scdn.co/image/ab67"
    assert mp.fix_art_url("file:///tmp/x.png") == "file:///tmp/x.png"


def test_choose_player():
    p = {"status": "Playing", "title": "a"}
    s = {"status": "Paused", "title": "b"}
    st = {"status": "Stopped", "title": ""}
    assert choose_player({"a": s, "b": p}, None) == "b"
    assert choose_player({"a": p, "b": p}, "b") == "b"                 # sticky while both play
    assert choose_player({"a": s, "b": s}, "b") == "b"                 # last active, now paused
    assert choose_player({"a": s, "c": st}, "c") == "a"                # last active stopped
    assert choose_player({"c": st}, None) is None and choose_player({}, None) is None


def test_label_from_bus_name():
    assert mp.label_from_bus_name(FIREFOX) == "Firefox"
    assert mp.label_from_bus_name(SPOTIFY) == "Spotify"


def test_parse_playerctl_output():
    sep = mp.SEP
    line1 = sep.join(["spotify", "spotify", "Playing", "Song", "Artist", "Album", "200000000", "50000000",
                      "true", "Track", "https://i.scdn.co/image/x"])
    line2 = sep.join(["firefox.instance_1_23", "firefox", "Paused", "Video", "", "", "", "1000000", "", "", ""])
    junk = "not enough fields"
    proxy = sep.join(["playerctld", "playerctld", "Playing", "Dup", "", "", "", "", "", "", ""])
    out = mp.parse_playerctl("\n".join([line1, line2, junk, proxy]))
    assert set(out) == {"spotify", "firefox.instance_1_23"}
    sp = out["spotify"]
    assert (sp["status"], sp["title"], sp["dur"], sp["pos"], sp["shuffle"], sp["repeat"]) == \
        ("Playing", "Song", 200.0, 50.0, 1, 1)
    ff = out["firefox.instance_1_23"]
    assert ff["app"] == "Firefox" and ff["shuffle"] == -1 and ff["repeat"] == -1 and ff["dur"] == 0


class FakeBus:
    def __init__(self, statuses):
        self.statuses = statuses
        self.calls = []

    def players(self):
        return list(self.statuses)

    def status(self, name):
        return self.statuses[name]

    def call(self, name, method, signature=None, body=()):
        self.calls.append((name, method, signature, body))

    def set_prop(self, name, prop, signature, value):
        self.calls.append((name, "set", prop, signature, value))


def test_now_playing_picks_the_playing_player_and_loads_file_art(tmp_path):
    cover = tmp_path / "cover.png"
    Image.new("RGB", (64, 64), (10, 200, 10)).save(cover)
    url = cover.resolve().as_uri()
    bus = FakeBus({
        FIREFOX: mp.map_player(mp.unwrap(jeepney_props(status="Paused")), "Firefox"),
        SPOTIFY: {**mp.map_player(mp.unwrap(jeepney_props()), "Spotify"), "art_url": url},
    })
    media = mp.MprisMedia(bus, None)
    np = media.now_playing()
    assert np.app == "Spotify" and np.playing and np.repeat == 2 and np.extra["source"] == SPOTIFY
    if os.name == "posix":   # file:// art is a Linux-only path shape
        assert np.art is not None and np.art.size == (64, 64) and np.art_key
        first = np.art_key
        Image.new("RGB", (64, 64), (200, 10, 10)).save(cover)
        os.utime(cover, (1, 1))
        assert media.now_playing().art_key != first          # file rewritten -> new key


def test_commands_go_to_the_shown_player():
    bus = FakeBus({SPOTIFY: mp.map_player(mp.unwrap(jeepney_props(art="")), "Spotify")})
    media = mp.MprisMedia(bus, None)
    media.now_playing()
    for cmd in ("playpause", "next", "prev", "seek:42.5", "shuffle", "repeat", "bogus"):
        media.command(cmd)
    assert bus.calls == [
        (SPOTIFY, "PlayPause", None, ()), (SPOTIFY, "Next", None, ()), (SPOTIFY, "Previous", None, ()),
        (SPOTIFY, "SetPosition", "ox", ("/com/spotify/track/1a2b3c4d5e6f7g8h9i0jKL", 42_500_000)),
        (SPOTIFY, "set", "Shuffle", "b", False),
        (SPOTIFY, "set", "LoopStatus", "s", "Track"),     # Playlist -> Track (all -> one)
    ]


def test_seek_without_trackid_uses_relative_seek():
    st = mp.map_player({"PlaybackStatus": "Playing", "Position": 10_000_000,
                        "Metadata": {"xesam:title": "t"}}, "Vlc")
    bus = FakeBus({"org.mpris.MediaPlayer2.vlc": st})
    media = mp.MprisMedia(bus, None)
    media.now_playing()
    media.command("seek:15")
    assert bus.calls == [("org.mpris.MediaPlayer2.vlc", "Seek", "x", (5_000_000,))]


def test_playerctl_fallback(monkeypatch):
    sep = mp.SEP
    line = sep.join(["spotify", "spotify", "Paused", "Song", "Artist", "Album", "200000000", "5000000",
                     "false", "None", ""])
    calls = []

    def fake_run(args, timeout=5.0, **kw):
        calls.append(args)
        return line + "\n" if "metadata" in args else ""
    monkeypatch.setattr(mp, "run", fake_run)
    media = mp.MprisMedia(None, "/usr/bin/playerctl")
    np = media.now_playing()
    assert np.title == "Song" and not np.playing and np.shuffle == 0 and np.repeat == 0
    media.command("repeat")
    media.command("seek:3")
    assert calls[-2] == ["/usr/bin/playerctl", "-p", "spotify", "loop", "Playlist"]
    assert calls[-1] == ["/usr/bin/playerctl", "-p", "spotify", "position", "3.000"]


def test_no_players_means_none():
    assert mp.MprisMedia(FakeBus({}), None).now_playing() is None


def test_jeepney_messages_encode():
    jeepney = pytest.importorskip("jeepney")
    from jeepney import DBusAddress, Properties, new_method_call
    addr = DBusAddress(mp.OBJ_PATH, bus_name=SPOTIFY, interface=mp.PLAYER_IFACE)
    for msg in (new_method_call(addr, "SetPosition", "ox", ("/com/spotify/track/x", 1_000_000)),
                Properties(addr).set("Shuffle", "b", True), Properties(addr).get_all(),
                new_method_call(addr, "Seek", "x", (-5,))):
        assert isinstance(msg.serialise(serial=1), bytes)
    assert jeepney


def test_unwrap_matches_a_real_jeepney_reply_body():
    """Round-trip a GetAll-shaped a{sv} body through jeepney's own (de)serialiser."""
    pytest.importorskip("jeepney")
    from jeepney.low_level import Message, Parser
    from jeepney.wrappers import new_method_return

    from jeepney import DBusAddress, new_method_call
    call = new_method_call(DBusAddress(mp.OBJ_PATH, bus_name=SPOTIFY, interface=mp.PLAYER_IFACE), "GetAll")
    call.header.serial = 1           # as if it had been sent
    reply = new_method_return(call, "a{sv}", (jeepney_props(),))
    parser = Parser()
    parser.add_data(reply.serialise(serial=2))
    msg = parser.get_next_message()
    assert isinstance(msg, Message)
    st = mp.map_player(mp.unwrap(msg.body[0]), "Spotify")
    assert st["artist"] == "Velvet Tide, Guest" and st["dur"] == pytest.approx(231.48) and st["repeat"] == 2
