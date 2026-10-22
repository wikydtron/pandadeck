"""SOUNDS: the soundboard page, sound files (defaults never overwritten, checked
uploads), per-OS playback commands, panel actions, settings and the web API."""
import io
import json
import struct
import threading
import wave

import pytest

from pandadeck import sounds as snd
from pandadeck.actions import Actions
from pandadeck.config import SettingsError, apply_patch, clean
from pandadeck.deck import board as bd
from pandadeck.sounds import player as pl
from pandadeck.sounds.store import MAX_BYTES, SoundError, SoundStore, sniff

LIB = {"airhorn": ("Air horn", "vol_up"), "vineboom": ("Boom", "bolt"), "tada": ("Ta-da!", "spark")}


def wav_bytes(n=800, rate=8000):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(struct.pack("<h", 0) * n)
    return buf.getvalue()


class FakeRender:
    def __init__(self):
        self.calls = []

    def __call__(self, sid):
        self.calls.append(sid)
        if sid not in LIB:
            raise KeyError(sid)
        return wav_bytes()


# ---- panel actions --------------------------------------------------------------------------------

def test_sound_actions():
    calls = []

    class Rec:
        def play(self, sid):
            calls.append(("play", sid))

        def stop_all(self):
            calls.append(("stop",))

    a = Actions(sounds=Rec())
    for act in ("sound:airhorn", "sound:stop", "sound:", "sound:s1abc"):
        a.run({"k": "cmd", "a": act})
    assert calls == [("play", "airhorn"), ("stop",), ("play", "s1abc")]
    Actions().run({"k": "cmd", "a": "sound:airhorn"})  # no sound player: ignored


# ---- the page --------------------------------------------------------------------------------------

def test_sounds_page_with_the_users_changes():
    deck = {"sounds": {"airhorn": {"label": "HORN", "icon": "", "color": "#60A5FA", "image": "a" * 40,
                                   "file": "", "volume": 40}}}
    page = snd.sounds_page(deck, LIB)
    assert page["name"] == "SOUNDS" and page["color"] == snd.PAGE_COLOR and page["layout"] == "keys"
    assert [b["action"] for b in page["buttons"]] == ["agent:sound:airhorn", "agent:sound:vineboom", "agent:sound:tada"]
    assert page["buttons"][0] == {"label": "HORN", "icon": "vol_up", "color": "#60A5FA", "image": "a" * 40,
                                  "action": "agent:sound:airhorn"}
    assert page["buttons"][1]["label"] == "Boom" and page["buttons"][1]["icon"] == "bolt"
    assert snd.entry(deck, "airhorn", LIB)["volume"] == 40 and snd.entry({}, "tada", LIB)["volume"] == snd.DEFAULT_VOLUME
    assert snd.entry({}, "mine", LIB) is None                                  # unknown, no file
    mine = snd.entry({"sounds": {"mine": {"label": "Mine", "file": "b" * 40 + ".mp3", "volume": 90}}}, "mine", LIB)
    assert mine["builtin"] is False and mine["icon"] == "audio" and mine["file"].endswith(".mp3")


def test_board_shows_the_sounds_page(settings, fake_platform, monkeypatch):
    monkeypatch.setattr(snd, "library", lambda: LIB)
    settings.update({"deck": {"pages": ["sounds", "c1"], "custom": [
        {"id": "c1", "name": "MINE", "buttons": [{"label": "Horn", "icon": "", "color": "", "image": "",
                                                   "action": "agent:sound:airhorn"},
                                                  {"label": "Stop", "icon": "close", "color": "", "image": "",
                                                   "action": "agent:sound:stop"}]}]}})
    b = bd.DeckBoard(settings, fake_platform, "windows")
    b._load_catalog()
    sounds, mine = b.current()["pages"]
    assert sounds["name"] == "SOUNDS" and sounds["cols"] == 4 and len(sounds["buttons"]) == 3
    assert sounds["buttons"][2] == {"label": "Ta-da!", "icon": "spark", "action": "agent:sound:tada"}
    assert [x["action"] for x in mine["buttons"]] == ["agent:sound:airhorn", "agent:sound:stop"]
    deck = {"pages": ["sounds"], "sounds": {"tada": {"image": "c" * 40}}}
    assert bd.icon_demand(deck) == 1                                           # a sound's own picture counts


# ---- files -----------------------------------------------------------------------------------------

def test_defaults_are_written_once_and_never_overwritten(tmp_path):
    render = FakeRender()
    store = SoundStore(tmp_path / "sounds", render=render)
    (tmp_path / "sounds").mkdir()
    (tmp_path / "sounds" / "airhorn.wav").write_bytes(b"my own horn")       # the user's replacement
    assert store.ensure_defaults(list(LIB)) == 2
    assert sorted(render.calls) == ["tada", "vineboom"]
    assert (tmp_path / "sounds" / "airhorn.wav").read_bytes() == b"my own horn"
    assert sniff((tmp_path / "sounds" / "tada.wav").read_bytes()) == "wav"
    assert store.ensure_defaults(list(LIB)) == 0 and len(render.calls) == 2  # nothing left to make
    assert store.ensure_default("nope") is None                              # unknown sound: no file
    assert store.ensure_default("../../evil") is None and render.calls[-1] == "nope"
    assert not list(tmp_path.glob("sounds/*.tmp")) and not list(tmp_path.glob("sounds/.*"))


def test_uploads_are_checked(tmp_path):
    store = SoundStore(tmp_path, render=FakeRender())
    w = wav_bytes()
    name = store.save_upload(w, "My Horn.WAV")
    assert name.endswith(".wav") and len(name) == 44 and store.upload_path(name).read_bytes() == w
    assert store.save_upload(w) == name                                      # same bytes, same file
    assert store.save_upload(b"ID3\x04\x00" + b"\0" * 100, "x.mp3").endswith(".mp3")
    assert store.save_upload(b"\xff\xfb\x90\x00" + b"\0" * 100, "x.mp3").endswith(".mp3")  # bare MPEG frame
    assert store.save_upload(b"OggS" + b"\0" * 100, "x.ogg").endswith(".ogg")
    for data, fname, msg in ((b"", "a.wav", "empty"), (b"MZ\x90\x00" * 10, "a.wav", "WAV, MP3, OGG or M4A"),
                             (w, "a.exe", "WAV, MP3, OGG or M4A"), (b"\xff\xf1" + b"\0" * 50, "a.mp3", "WAV, MP3, OGG or M4A"),
                             (b"RIFF" + b"\0" * (MAX_BYTES + 1), "a.wav", "5 MB")):
        with pytest.raises(SoundError, match=msg):
            store.save_upload(data, fname)
    assert store.upload_path("../../settings.json") is None and store.upload_path("a" * 40 + ".exe") is None
    assert store.upload_path("b" * 40 + ".wav") is None                     # well-formed but missing


def test_resolve_prefers_the_users_file(tmp_path, settings):
    store = SoundStore(tmp_path, render=FakeRender())
    mine = store.save_upload(b"OggS" + b"\1" * 64, "mine.ogg")
    played = []

    class P:
        def play(self, path, volume, **kw):
            played.append((path.name, volume))
            return True

        def stop_all(self):
            played.append("stop")

    s = snd.Sounds(settings, store=store, player=P(), lib=LIB)
    settings.update({"deck": {"sounds": {"airhorn": {"file": mine, "volume": 30},
                                         "s1": {"label": "Mine", "file": mine, "volume": 70}}}})
    assert s.resolve("airhorn") == (tmp_path / mine, 30)
    assert s.resolve("tada")[0].name == "tada.wav"                            # default, made on demand
    assert s.resolve("s1")[1] == 70 and s.resolve("ghost") == (None, snd.DEFAULT_VOLUME)
    settings.update({"deck": {"sounds": {"airhorn": {"file": "c" * 40 + ".mp3", "volume": 30}}}})
    assert s.resolve("airhorn")[0].name == "airhorn.wav"                      # file gone: the default again
    assert s.preview("airhorn", default=True, volume=10) and played[-1] == ("airhorn.wav", 10)
    assert s.preview(file=mine) and played[-1] == (mine, snd.DEFAULT_VOLUME)
    assert not s.preview(file="d" * 40 + ".wav")
    done = threading.Event()
    s.player.play = lambda path, volume, **kw: played.append((path.name, volume)) or done.set()
    s.play("vineboom")
    assert done.wait(5) and played[-1] == ("vineboom.wav", snd.DEFAULT_VOLUME)
    s.stop_all()
    assert played[-1] == "stop"


# ---- playback per OS --------------------------------------------------------------------------------

def test_player_commands_per_os():
    have = lambda *names: (lambda n: f"/usr/bin/{n}" if n in names else None)  # noqa: E731
    assert pl.command("darwin", "/s/a.wav", 80, have("afplay")) == ["afplay", "-v", "0.80", "/s/a.wav"]
    assert pl.command("darwin", "/s/a.ogg", 80, have("afplay")) is None       # CoreAudio has no Vorbis
    assert pl.command("darwin", "/s/a.ogg", 50, have("afplay", "mpv"))[:3] == ["mpv", "--no-video", "--really-quiet"]
    assert pl.command("linux", "/s/a.wav", 80, have("paplay", "aplay")) == ["paplay", "--volume=52429", "/s/a.wav"]
    assert pl.command("linux", "/s/a.wav", 80, have("aplay")) == ["aplay", "-q", "/s/a.wav"]
    assert pl.command("linux", "/s/a.mp3", 25, have("paplay", "ffplay")) == \
        ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", "-volume", "25", "/s/a.mp3"]
    assert pl.command("linux", "/s/a.ogg", 100, have("paplay")) == ["paplay", "--volume=65536", "/s/a.ogg"]
    assert pl.command("linux", "/s/a.mp3", 100, have()) is None
    assert pl.command("linux", "/s/a.wav", 250, have("paplay"))[1] == "--volume=65536"  # clamped
    assert pl.mci_open("pdsnd1", "C:\\Sounds\\a b.mp3", 80) == [
        'open "C:\\Sounds\\a b.mp3" type mpegvideo alias pdsnd1', "setaudio pdsnd1 volume to 800", "play pdsnd1"]


def test_posix_player_caps_and_stops(monkeypatch):
    started = []

    class Proc:
        def __init__(self, argv, **kw):
            self.argv, self.alive = argv, True
            assert kw["start_new_session"] and kw["stdin"] is pl.subprocess.DEVNULL
            started.append(self)

        def poll(self):
            return None if self.alive else 0

        def terminate(self):
            self.alive = False

    p = pl.Player("linux", which=lambda n: "/usr/bin/" + n if n == "paplay" else None, popen=Proc)
    for i in range(5):
        assert p.play(f"/s/{i}.wav", 100)
    assert not started[0].alive and all(x.alive for x in started[1:])          # 5th stopped the oldest
    p.stop_all()
    assert not any(x.alive for x in started)
    assert not pl.Player("linux", which=lambda n: None, popen=Proc).play("/s/x.mp3", 50)  # nothing to play with


class Proc:
    started: list = []

    def __init__(self, argv, **kw):
        self.argv, self.alive = argv, True
        Proc.started.append(self)

    def poll(self):
        return None if self.alive else 0

    def terminate(self):
        self.alive = False


def test_posix_soundboard_plays_one_at_a_time_and_a_second_tap_stops():
    Proc.started = []
    st = Proc.started
    p = pl.Player("linux", which=lambda n: "/usr/bin/" + n if n == "paplay" else None, popen=Proc)
    assert p.play("/s/a.wav", 100, key="a", mode="toggle")
    assert p.play("/s/b.wav", 100, key="b", mode="toggle")
    assert not st[0].alive and st[1].alive                                     # another sound cuts the first
    assert p.play("/s/b.wav", 100, key="b", mode="toggle")
    assert not st[1].alive and len(st) == 2                                    # same one again: stopped
    assert p.play("/s/b.wav", 100, key="b", mode="toggle") and len(st) == 3 and st[2].alive
    st[2].alive = False                                                        # it finished by itself...
    assert p.play("/s/b.wav", 100, key="b", mode="toggle") and len(st) == 4    # ...so a tap plays it again
    assert p.play("/s/b.wav", 100, key="preview", mode="solo") and len(st) == 5
    assert not st[3].alive and st[4].alive                                     # preview cuts too, never toggles
    assert p.play("/s/b.wav", 100, key="preview", mode="solo") and len(st) == 6 and st[5].alive
    assert p.play("/s/c.wav", 100) and st[5].alive and st[6].alive             # "mix" still layers


def test_windows_soundboard_plays_one_at_a_time_and_a_second_tap_stops(monkeypatch):
    sent, status = [], {}

    def send(cmd):
        sent.append(cmd)
        if cmd.startswith("status"):
            return 0, status.get(cmd.split()[1], "playing")
        return 0, ""

    def settle(what):
        for _ in range(300):
            if what in sent:
                return True
            threading.Event().wait(0.01)
        return False

    monkeypatch.setattr(pl, "MCI_POLL_S", 0.01)
    p = pl.Player("win32", mci_send=send)
    p.play("C:\\s\\a.wav", 50, key="a", mode="toggle")
    assert settle("play pdsnd1")
    p.play("C:\\s\\b.wav", 50, key="b", mode="toggle")
    assert settle("play pdsnd2") and sent.index("close pdsnd1") < sent.index("play pdsnd2")   # cut first
    p.play("C:\\s\\b.wav", 50, key="b", mode="toggle")                      # tapped again while playing
    assert settle("close pdsnd2")
    p.play("C:\\s\\b.wav", 50, key="b", mode="toggle")
    assert settle("play pdsnd3")                                               # and once more: plays again
    assert not any(c.startswith("open") and "pdsnd4" in c for c in sent)


def test_panel_taps_toggle_and_previews_cut_in(tmp_path, settings):
    calls = []

    class FakePlayer:
        def play(self, path, volume=100, key="", mode="mix"):
            calls.append((key, mode))
            return True

    store = snd.SoundStore(tmp_path)
    s = snd.Sounds(settings, store=store, player=FakePlayer(), lib={"boing": ("Boing", "smile")})
    store.ensure_default = lambda sid: tmp_path / f"{sid}.wav"
    s.resolve = lambda sid: (tmp_path / f"{sid}.wav", 80)
    s._play("boing")
    s.preview("boing")
    assert calls == [("boing", "toggle"), ("preview", "solo")]


def test_windows_player_uses_mci(monkeypatch):
    sent = []
    status = {}
    done = threading.Event()

    def send(cmd):
        sent.append(cmd)
        if cmd.startswith("status"):
            alias = cmd.split()[1]
            return 0, status.get(alias, "playing")
        if cmd.startswith("close") and sum(c.startswith("close") for c in sent) >= 2:
            done.set()
        return 0, ""

    monkeypatch.setattr(pl, "MCI_POLL_S", 0.01)
    p = pl.Player("win32", mci_send=send)
    p.play("C:\\s\\a.wav", 50)
    for _ in range(200):
        if "play pdsnd1" in sent:
            break
        threading.Event().wait(0.01)
    assert sent[:3] == ['open "C:\\s\\a.wav" type mpegvideo alias pdsnd1', "setaudio pdsnd1 volume to 500", "play pdsnd1"]
    status["pdsnd1"] = "stopped"                                               # finished: closed by the poller
    p.play("C:\\s\\b.wav", 50)
    for _ in range(200):
        if "close pdsnd1" in sent:
            break
        threading.Event().wait(0.01)
    assert "close pdsnd1" in sent
    p.stop_all()
    assert done.wait(5) and "close pdsnd2" in sent


# ---- settings ----------------------------------------------------------------------------------------

def test_sound_settings_and_the_one_time_page_migration():
    s = apply_patch(clean({}), {"deck": {"sounds": {"airhorn": {"label": "Hörn", "volume": 30},
                                                    "s1x": {"file": "a" * 40 + ".mp3", "volume": 100}}}})
    assert s["deck"]["sounds"]["airhorn"] == {"label": "Horn", "icon": "", "color": "", "image": "", "file": "",
                                              "volume": 30, "image_auto": False, "image_origin": "", "image_src": "",
                                              "link": ""}
    for bad in ({"stop": {}}, {"airhorn": {"volume": 101}}, {"airhorn": {"volume": True}},
                {"airhorn": {"file": "../x.wav"}}, {"Bad Id": {}}, {"airhorn": {"icon": "nope"}}):
        with pytest.raises(SettingsError):
            apply_patch(clean({}), {"deck": {"sounds": bad}})
    assert "sounds" in clean({})["deck"]["pages"]                               # new installs
    five = ["apps", "media", "windows", "c1", "macros"]
    old = {"version": 1, "deck": {"pages": five, "custom": [{"id": "c1", "name": "X", "buttons": []}]}}
    assert clean(old)["deck"]["pages"] == five + ["sounds"]                     # existing decks with room, once
    full = {"version": 1, "deck": {"pages": five + ["dev"], "custom": [{"id": "c1", "name": "X", "buttons": []}]}}
    assert "sounds" not in clean(full)["deck"]["pages"]                         # 6 tabs already: left alone
    assert "sounds" not in clean({"version": 2, "deck": {"pages": ["apps"]}})["deck"]["pages"]  # turned off: stays off


# ---- web API ------------------------------------------------------------------------------------------

def test_sound_api(fake_platform, monkeypatch, tmp_path):
    import http.client

    from pandadeck.service import Service
    from pandadeck.web import api
    from pandadeck.web.server import make_server
    svc = Service(panel=False, platform=fake_platform)
    svc.sounds.store = SoundStore(tmp_path, render=FakeRender())
    svc.sounds._lib = LIB
    played = []
    svc.sounds.player.play = lambda path, volume, **kw: played.append((path.name, volume)) or True
    svc.sounds.player.stop_all = lambda: played.append("stop")
    srv = make_server(svc, 0, api.ROUTES)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def call(path, body=None, raw=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        data = raw if raw is not None else json.dumps(body or {}).encode()
        c.request("POST", path, body=data, headers={"Host": f"127.0.0.1:{port}", "Content-Type":
                  "application/octet-stream" if raw is not None else "application/json", **(headers or {})})
        r = c.getresponse()
        out = r.status, json.loads(r.read() or b"null")
        c.close()
        return out

    good = {"X-PandaDeck": "1"}
    try:
        assert call("/api/sounds/upload?name=horn.wav", raw=wav_bytes())[0] == 403   # needs the header
        status, body = call("/api/sounds/upload?name=horn.wav", raw=wav_bytes(), headers=good)
        assert status == 200 and body["file"].endswith(".wav")
        assert call("/api/sounds/upload?name=evil.exe", raw=b"MZ" * 40, headers=good)[0] == 400
        assert call("/api/sounds/play", {"id": "tada", "volume": 40}, headers=good)[0] == 200
        assert played[-1] == ("tada.wav", 40)
        assert call("/api/sounds/play", {"file": body["file"]}, headers=good)[0] == 200
        assert call("/api/sounds/play", {"id": "ghost"}, headers=good)[0] == 404
        assert call("/api/sounds/play", {"id": "../x"}, headers=good)[0] == 400
        assert call("/api/sounds/play", {"id": "tada", "volume": 300}, headers=good)[0] == 400
        assert call("/api/sounds/stop", {}, headers=good) == (200, {"ok": True}) and played[-1] == "stop"
        assert svc.meta()["sound_library"]["airhorn"] == {"label": "Air horn", "icon": "vol_up"}
    finally:
        srv.shutdown()
        srv.server_close()
