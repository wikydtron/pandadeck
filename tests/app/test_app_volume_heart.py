"""MUSIC page: the system volume from the panel's slider, and the ❤ only when Spotify plays."""
from types import SimpleNamespace

from pandadeck.actions import Actions
from pandadeck.music.collector import MediaCollector
from pandadeck.platform import linux as lx
from pandadeck.platform import macos as mac


class Audio:
    def __init__(self):
        self.levels = []

    def set_volume(self, level):
        self.levels.append(level)


def test_panel_volume_command_sets_the_system_volume():
    audio = Audio()
    actions = Actions(audio=audio)
    for arg in ("volume:37", "volume:150", "volume:-5", "volume:abc", "volume:"):
        actions.run({"k": "cmd", "a": arg})
    assert audio.levels == [37, 100, 0]                          # clamped; junk ignored


def test_pactl_and_wpctl_set_the_default_output(monkeypatch):
    calls = []
    monkeypatch.setattr(lx, "run", lambda args, **kw: calls.append(tuple(args)) or "")
    lx.PactlAudio("/usr/bin/pactl").set_volume(42)
    lx.WpctlAudio("/usr/bin/wpctl").set_volume(7)
    assert ("/usr/bin/pactl", "set-sink-volume", "@DEFAULT_SINK@", "42%") in calls
    assert ("/usr/bin/wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "0.07") in calls


def test_mac_sets_the_output_volume_and_rereads(monkeypatch):
    sent, state = [], {"out": 30}

    def fake_run(args, **kw):
        script = args[2]
        if script.startswith("set volume output volume"):
            state["out"] = int(script.rsplit(" ", 1)[1])
            sent.append(state["out"])
            return ""
        return f"{state['out']},50,false"
    monkeypatch.setattr(mac, "run", fake_run)
    audio = mac.MacAudio()
    assert audio.volume() == 30
    audio.set_volume(64)
    assert sent == [64] and audio.volume() == 64                   # not the cached 30


class Likes:
    def state(self, title, artist, hint=""):
        return 1


def liked(app, source="system", likes=Likes()):
    c = MediaCollector.__new__(MediaCollector)
    c.likes = likes
    np = SimpleNamespace(app=app, title="Song", artist="Band", extra={})
    return c._liked(source, np, None)


def test_heart_only_for_spotify():
    assert liked("Spotify") == 1                                     # Spotify desktop app (system media)
    assert liked("", source="spotify") == 1                          # Spotify Web API source
    assert liked("Google Chrome") == -1                              # YouTube & co: no heart
    assert liked("VLC media player") == -1
    assert liked("Spotify", likes=None) == -1                        # not signed in to Spotify
