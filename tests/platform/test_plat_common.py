"""Every backend imports on every OS; shared helpers."""
import importlib
import inspect
import sys

import pytest

from pandadeck.platform import _util
from pandadeck.platform.base import Platform

MODULES = ["_util", "_desktop", "_icons", "_mpris", "_mac_media", "_win_shell", "_win_media", "_win_audio",
           "windows", "macos", "linux"]
CLASSES = [("windows", "WindowsPlatform", "windows"), ("macos", "MacPlatform", "macos"),
           ("linux", "LinuxPlatform", "linux")]


@pytest.mark.parametrize("name", MODULES)
def test_module_imports_on_this_os(name):
    importlib.import_module(f"pandadeck.platform.{name}")


@pytest.mark.parametrize("module,cls,os_name", CLASSES)
def test_platform_classes_implement_the_interface(module, cls, os_name):
    klass = getattr(importlib.import_module(f"pandadeck.platform.{module}"), cls)
    assert klass.name == os_name
    for meth in ("list_apps", "app_icon", "file_icon", "launch_app", "media", "audio", "set_autostart",
                 "autostart_enabled"):
        assert callable(getattr(klass, meth)), meth
        want = list(inspect.signature(getattr(Platform, meth)).parameters)
        have = list(inspect.signature(getattr(klass, meth)).parameters)
        assert have[:len(want)] == want, (cls, meth, have)


def test_current_picks_this_os():
    from pandadeck.platform import current
    expected = {"win32": "WindowsPlatform", "darwin": "MacPlatform"}.get(sys.platform, "LinuxPlatform")
    assert type(current()).__name__ == expected


@pytest.mark.parametrize("cmd,secs", [("seek:42.5", 42.5), ("seek:-3", 0.0), ("seek:0", 0.0), ("seek:nan", None),
                                      ("seek:inf", None), ("seek:", None), ("seek:abc", None), ("next", None)])
def test_parse_seek(cmd, secs):
    assert _util.parse_seek(cmd) == secs


def test_ttl_cache_reloads_after_ttl_and_keeps_last_good_value(monkeypatch):
    now = [100.0]

    class FakeTime:
        @staticmethod
        def monotonic():
            return now[0]
    monkeypatch.setattr(_util, "time", FakeTime)
    cache = _util.TTLCache(10, default=[], min_force_s=5)
    loads = []

    def load():
        loads.append(1)
        return [len(loads)]
    assert cache.get(load) == [1] and cache.get(load) == [1]
    now[0] += 11
    assert cache.get(load) == [2]

    assert cache.get(load, force=True) == [2]                      # forced too soon: no reload
    now[0] += 6

    def boom():
        raise RuntimeError("tool missing")
    assert cache.get(boom, force=True) == [2]                      # last good value survives
    assert len(loads) == 2
    assert _util.TTLCache(10, default=["d"]).get(boom) == ["d"]    # never loaded -> default


def test_validate_argv():
    assert _util.validate_argv(("a", "b")) == ["a", "b"]
    for bad in ([], "app.exe", [""], ["a", None], ["a\0b"], None):
        with pytest.raises(ValueError):
            _util.validate_argv(bad)


def test_fetch_url_refuses_non_http_schemes():
    for url in ("file:///etc/passwd", "ftp://x/y", "data:image/png;base64,AAAA", "javascript:alert(1)"):
        assert _util.fetch_url(url) is None
    assert _util.load_art_url("") is None


def test_file_url_path():
    assert _util.file_url_path("file:///tmp/firefox-mpris/a%20b.png") == "/tmp/firefox-mpris/a b.png"
    assert _util.file_url_path("file://remotehost/x") is None
    assert _util.file_url_path("https://x/y") is None


def test_spawn_rejects_bad_commands():
    for bad in ([], [""], ["ok\0"]):
        with pytest.raises(ValueError):
            _util.spawn(bad)


def test_clean_and_to_float():
    assert _util.clean("  a\n b\tc ") == "a b c" and _util.clean(None) == ""
    assert _util.to_float("1,5") == 1.5 and _util.to_float("x", -1.0) == -1.0


def test_art_cache_reloads_on_change_and_backs_off_after_failures(monkeypatch):
    import io

    from PIL import Image
    now = [0.0]

    class FakeTime:
        @staticmethod
        def monotonic():
            return now[0]
    monkeypatch.setattr(_util, "time", FakeTime)
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (9, 9, 9)).save(buf, "PNG")
    png = buf.getvalue()
    loads = []

    def ok():
        loads.append("ok")
        return png

    def fail():
        loads.append("fail")
        return None
    cache = _util.ArtCache(retry_s=30)
    cache.update("a", ok)
    cache.update("a", ok)
    assert loads == ["ok"] and cache.image.size == (8, 8) and cache.key == _util.art_key(png)
    cache.update("b", fail)
    assert cache.image is None and cache.key == ""
    now[0] = 10
    cache.update("b", fail)                     # inside the back-off window: no retry
    now[0] = 31
    cache.update("b", ok)                       # retried, now it works
    assert loads == ["ok", "fail", "ok"] and cache.image is not None
    cache.update("", ok)                        # no source -> no art, no load
    assert cache.image is None and loads[-1] == "ok" and len(loads) == 3
