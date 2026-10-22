"""Failure modes a 1 Hz poll loop must survive (from the code review)."""
import datetime as dt
import io
import plistlib
from concurrent.futures import Future

from PIL import Image

from pandadeck.platform import _mpris as mp
from pandadeck.platform import _util
from pandadeck.platform import _win_media as wm
from pandadeck.platform import _win_shell as ws
from pandadeck.platform import linux as lx
from pandadeck.platform import macos as mac


class FakeTime:
    now = 0.0

    @classmethod
    def monotonic(cls):
        return cls.now


# ---- URLs / files / images -------------------------------------------------------------------------

def test_only_public_http_urls_are_fetchable():
    for url in ("http://127.0.0.1/x.png", "http://localhost:8080/a", "http://192.168.1.50/api/icon",
                "http://10.0.0.1/", "http://169.254.169.254/latest/meta-data/", "http://[::1]/x",
                "ftp://8.8.8.8/x", "file:///etc/passwd", "https:///nohost", "not a url"):
        assert _util.public_http_url(url) is False, url
        assert _util.fetch_url(url) is None
    assert _util.public_http_url("https://8.8.8.8/image.png") is True


def test_fetch_url_never_raises_on_odd_urls(monkeypatch):
    monkeypatch.setattr(_util, "public_http_url", lambda url: True)
    # A space makes http.client raise InvalidURL (not an OSError); port 9 is closed anyway.
    assert _util.fetch_url("http://127.0.0.1:9/a b.png", timeout=0.5) is None


def test_read_limited_refuses_non_files_and_bad_paths(tmp_path):
    f = tmp_path / "a.bin"
    f.write_bytes(b"x" * 10)
    assert _util.read_limited(f) == b"x" * 10
    assert _util.read_limited(f, limit=5) is None
    assert _util.read_limited(tmp_path) is None                  # a directory
    assert _util.read_limited(str(tmp_path / "nul\x00byte")) is None
    assert _util.load_art_url("file:///tmp/a%00b.png") is None


def test_huge_images_are_refused_before_decoding():
    buf = io.BytesIO()
    Image.new("1", (5000, 4000)).save(buf, "PNG")              # 20 MP, tiny file
    assert _util.image_from_bytes(buf.getvalue()) is None


def test_art_cache_survives_a_raising_loader(monkeypatch):
    monkeypatch.setattr(_util, "time", FakeTime)
    calls = []

    def boom():
        calls.append(1)
        raise RuntimeError("http.client.IncompleteRead")
    cache = _util.ArtCache(retry_s=30)
    FakeTime.now = 100.0
    cache.update("u", boom)
    cache.update("u", boom)
    assert cache.image is None and len(calls) == 1               # backed off, no exception


def test_ttl_cache_backs_off_after_a_failed_load(monkeypatch):
    monkeypatch.setattr(_util, "time", FakeTime)
    FakeTime.now = 1000.0
    calls = []

    def boom():
        calls.append(1)
        raise RuntimeError("Get-StartApps timed out")
    cache = _util.TTLCache(300, default=[], retry_s=30)
    assert cache.get(boom) == [] and cache.get(boom) == [] and len(calls) == 1
    FakeTime.now += 31
    assert cache.get(lambda: ["ok"]) == ["ok"]
    assert cache.peek() == ["ok"]


# ---- Windows ---------------------------------------------------------------------------------------------

def test_live_position_ignores_epoch_timestamps():
    now = dt.datetime(2026, 10, 21, 12, 0, tzinfo=dt.timezone.utc)
    epoch = dt.datetime(1601, 1, 1, tzinfo=dt.timezone.utc)
    assert wm.live_position(12.0, epoch, now, True, 0) == 12.0
    assert wm.live_position(12.0, epoch, now, True, 200) == 12.0
    assert wm.live_position(12.0, now + dt.timedelta(seconds=5), now, True, 200) == 12.0   # clock skew


def test_shell_icons_replace_a_hung_thread_then_give_up(monkeypatch):
    made = []

    class HungWorker:
        def __init__(self):
            made.append(self)

        def submit(self, fn):
            return Future()                                      # never completes

    monkeypatch.setattr(ws, "_StaWorker", HungWorker)
    monkeypatch.setattr(ws, "ICON_TIMEOUT_S", 0.01)
    icons = ws.ShellIcons()
    for _ in range(ws.MAX_RESTARTS + 2):
        assert icons.icon("Some.App!Id") is None
    assert len(made) == 1 + ws.MAX_RESTARTS and icons._worker is None
    assert icons.icon("Another!App") is None                     # disabled: returns at once


# ---- Linux -------------------------------------------------------------------------------------------------

def test_boosted_volume_is_clamped_to_100():
    assert lx.parse_pactl_volume("Volume: front-left: 98304 / 150% / 10.57 dB") == 100
    assert lx.parse_wpctl_volume("Volume: 1.50") == (100, False)


def test_playerctl_not_spawned_when_the_bus_works_but_nothing_plays(monkeypatch):
    class EmptyBus:
        def players(self):
            return []

    calls = []
    monkeypatch.setattr(mp, "run", lambda args, **kw: calls.append(args))
    media = mp.MprisMedia(EmptyBus(), "/usr/bin/playerctl")
    assert media.now_playing() is None and calls == []


def test_playerctl_used_when_the_bus_fails(monkeypatch):
    class BrokenBus:
        def players(self):
            raise ConnectionError("bus gone")

    calls = []
    monkeypatch.setattr(mp, "run", lambda args, **kw: calls.append(args) or "")
    mp.MprisMedia(BrokenBus(), "/usr/bin/playerctl").now_playing()
    assert calls and calls[0][0] == "/usr/bin/playerctl"


# ---- macOS ---------------------------------------------------------------------------------------------------

def test_malformed_plists_do_not_break_the_scan_or_autostart(tmp_path):
    apps = tmp_path / "Applications"
    bad = apps / "Broken.app" / "Contents"
    bad.mkdir(parents=True)
    (bad / "Info.plist").write_bytes(b"<?xml version='1.0'?><plist><dict><key>CFBundleName</key>")
    good = apps / "Good.app" / "Contents"
    good.mkdir(parents=True)
    (good / "Info.plist").write_bytes(plistlib.dumps({"CFBundleName": "Good"}))
    plat = mac.MacPlatform(home=tmp_path, roots=[apps], which=lambda _n: None)
    assert sorted(a.name for a in plat.list_apps()) == ["Broken", "Good"]
    agent = tmp_path / "Library" / "LaunchAgents" / "com.pandadeck.app.plist"
    agent.parent.mkdir(parents=True)
    agent.write_bytes(b"<?xml version='1.0'?><plist><dict><key>Label")
    assert plat.autostart_enabled() is False
