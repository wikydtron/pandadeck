"""Windows backend: pure parts (run on every OS)."""
import codecs
import datetime as dt
import json

import pytest
from PIL import Image

from pandadeck.platform import _win_media as wm
from pandadeck.platform import _win_shell as ws
from pandadeck.platform import windows as w
from pandadeck.platform.base import AppInfo


def ua_value(runs: int, focus_ms: int) -> bytes:
    """A 72-byte UserAssist v5 value."""
    v = bytearray(72)
    v[4:8] = runs.to_bytes(4, "little", signed=True)
    v[12:16] = focus_ms.to_bytes(4, "little", signed=True)
    return bytes(v)


class FakeRegistry:
    def __init__(self, values=None):
        self.data: dict[tuple[str, str], object] = {}
        self._values = values or []

    def get(self, key, name):
        return self.data.get((key, name))

    def set_str(self, key, name, value):
        self.data[(key, name)] = value

    def delete(self, key, name):
        self.data.pop((key, name), None)

    def values(self, key):
        return list(self._values) if key == w.USERASSIST else []


# ---- UserAssist ---------------------------------------------------------------------------------

def test_rot13_round_trip_and_known_value():
    assert w.rot13("Pynhqr_cmf8fkewkswwp!Pynhqr") == "Claude_pzs8sxrjxfjjc!Claude"
    assert w.rot13(w.rot13("{6D809377}\\7-Zip\\7zFM.exe")) == "{6D809377}\\7-Zip\\7zFM.exe"


def test_parse_userassist_value_reads_runs_and_focus_and_rejects_junk():
    assert w.parse_userassist_value(ua_value(7, 123_456)) == (123_456, 7)
    assert w.parse_userassist_value(ua_value(-1, -5)) == (0, 0)       # negatives clamp
    assert w.parse_userassist_value(b"\x00" * 8) is None              # old/short format
    assert w.parse_userassist_value("not bytes") is None


def test_decode_userassist_decodes_names_and_sums_duplicates():
    enc = codecs.encode
    values = [(enc("Chrome", "rot13"), ua_value(3, 1000)), (enc("Chrome", "rot13"), ua_value(1, 500)),
              (enc("UEME_CTLSESSION", "rot13"), b"\x01\x02"), (enc("Spotify.exe", "rot13"), ua_value(4, 0))]
    out = w.decode_userassist(values)
    assert out == {"Chrome": (1500, 4), "Spotify.exe": (0, 4)}


def test_usage_score_focus_dominates_runs():
    assert w.usage_score(900_000, 1) > w.usage_score(500_000, 999)
    assert w.usage_score(0, 4) > w.usage_score(0, 1) > w.usage_score(0, 0) == 0


# ---- Start menu ---------------------------------------------------------------------------------------

def test_parse_start_apps_handles_list_single_object_and_bad_rows():
    rows = [{"Name": "Calculator", "AppID": "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"},
            {"Name": "  Google\tChrome ", "AppID": "Chrome"}, {"Name": "", "AppID": "x"}, {"AppID": "y"}, 5]
    assert w.parse_start_apps(json.dumps(rows)) == {
        "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App": "Calculator", "Chrome": "Google Chrome"}
    assert w.parse_start_apps(json.dumps({"Name": "Solo", "AppID": "s"})) == {"s": "Solo"}
    assert w.parse_start_apps("") == {}


@pytest.mark.parametrize("app_id,name,ok", [
    ("Chrome", "Google Chrome", True),
    ("steam://rungameid/4242420", "Some Game", True),
    ("{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\services.msc", "Services", True),
    ("{6D809377-6AF0-444B-8957-A3773F02200E}\\7-Zip\\7-zip.chm", "7-Zip Help", False),
    ("https://nodejs.org/", "Node.js website", False),
    ("{6D809377}\\VideoLAN\\VLC\\Documentation.url", "Documentation", False),
    ("\\\\NAS\\share\\folder", "Folder", False),
    ("Microsoft.Windows.ShellExperienceHost_cw5n1h2txyewy!App", "Shell", False),
    ("{7C5A40EF}\\Foo\\unins000.exe", "Uninstall Foo", False),
])
def test_is_app_entry(app_id, name, ok):
    assert w.is_app_entry(app_id, name) is ok


def test_build_app_list_ranks_by_usage_dedupes_names_and_keeps_unused_apps():
    catalog = {"Claude_x!Claude": "Claude", "Chrome": "Google Chrome", "chrome-dup": "Google Chrome",
               "Spotify.exe": "Spotify", "Calc!App": "Calculator", "https://x.example/": "Docs"}
    usage = {"Claude_x!Claude": (900_000, 3), "Chrome": (500_000, 9), "chrome-dup": (1, 0),
             "Spotify.exe": (0, 4), "UEME_CTLSESSION": (99_999_999, 1)}
    apps = w.build_app_list(catalog, usage)
    assert [a.name for a in apps] == ["Claude", "Google Chrome", "Spotify", "Calculator"]
    assert apps[1].id == "Chrome"                     # the most used of the duplicate names
    assert apps[-1] == AppInfo("Calc!App", "Calculator", 0.0)
    assert all(isinstance(a.score, float) for a in apps)


def test_list_apps_and_launch_validation_without_touching_the_os(monkeypatch):
    enc = codecs.encode
    reg = FakeRegistry([(enc("Chrome", "rot13"), ua_value(2, 60_000))])
    calls = []
    monkeypatch.setattr(w, "start_apps", lambda: {"Chrome": "Google Chrome", "Calc!App": "Calculator"})
    monkeypatch.setattr(w, "spawn", lambda argv, **kw: calls.append(argv))
    plat = w.WindowsPlatform(registry=reg)
    apps = plat.list_apps()
    assert [a.id for a in apps] == ["Chrome", "Calc!App"] and apps[0].score == 60.002
    plat.launch_app("Calc!App")
    assert calls[-1][0].lower().endswith("explorer.exe") and calls[-1][1] == "shell:AppsFolder\\Calc!App"
    with pytest.raises(ValueError):
        plat.launch_app("Evil.exe & calc")
    assert len(calls) == 1


def test_list_apps_keeps_last_good_catalog_when_powershell_fails(monkeypatch):
    plat = w.WindowsPlatform(registry=FakeRegistry())
    plat._apps._min_force_s = 0
    monkeypatch.setattr(w, "start_apps", lambda: {"Chrome": "Google Chrome"})
    assert [a.id for a in plat.list_apps()] == ["Chrome"]

    def boom():
        raise RuntimeError("Get-StartApps failed")
    monkeypatch.setattr(w, "start_apps", boom)
    assert [a.id for a in plat._apps.get(plat._load_apps, force=True)] == ["Chrome"]


# ---- autostart ---------------------------------------------------------------------------------------------

def test_autostart_writes_quoted_run_value_and_clears_task_manager_disable():
    reg = FakeRegistry()
    plat = w.WindowsPlatform(registry=reg)
    assert plat.autostart_enabled() is False
    reg.data[(w.APPROVED_KEY, w.RUN_VALUE)] = b"\x03" + b"\x00" * 11   # disabled in Task Manager
    plat.set_autostart(True, [r"C:\Program Files\PandaDeck\pythonw.exe", "-m", "pandadeck"])
    assert reg.data[(w.RUN_KEY, w.RUN_VALUE)] == '"C:\\Program Files\\PandaDeck\\pythonw.exe" -m pandadeck'
    assert (w.APPROVED_KEY, w.RUN_VALUE) not in reg.data
    assert plat.autostart_enabled() is True
    reg.data[(w.APPROVED_KEY, w.RUN_VALUE)] = b"\x03" + b"\x00" * 11
    assert plat.autostart_enabled() is False                            # user disabled it again
    plat.set_autostart(False, [])
    assert reg.data == {} and plat.autostart_enabled() is False


def test_autostart_rejects_bad_argv():
    plat = w.WindowsPlatform(registry=FakeRegistry())
    for bad in ([], [""], ["ok", "nul\0byte"], "pandadeck.exe"):
        with pytest.raises(ValueError):
            plat.set_autostart(True, bad)


def test_approved_disabled_flag():
    assert w.approved_disabled(b"\x03\x00") and w.approved_disabled(b"\x07")
    assert not w.approved_disabled(b"\x02\x00") and not w.approved_disabled(b"\x06")
    assert not w.approved_disabled(None) and not w.approved_disabled(b"")


# ---- shell icons / media helpers ----------------------------------------------------------------------------

def test_premultiplied_bgra_is_unpremultiplied():
    # one half-transparent pixel (B=10 G=20 R=30 premultiplied, A=128), one transparent, one opaque white
    data = bytes([10, 20, 30, 128, 0, 0, 0, 0, 255, 255, 255, 255, 0, 0, 0, 0])
    img = ws.premultiplied_bgra_to_rgba(data, 2, 2)
    assert img.mode == "RGBA" and img.size == (2, 2)
    r, g, b, a = img.getpixel((0, 0))
    assert a == 128 and abs(r - 59) <= 1 and abs(g - 39) <= 1 and abs(b - 19) <= 1
    assert img.getpixel((0, 1)) == (255, 255, 255, 255)


def test_bitmap_without_alpha_is_opaque():
    data = bytes([0, 0, 255, 0] * 4)            # pure red BGRX, alpha all zero
    img = ws.premultiplied_bgra_to_rgba(data, 2, 2)
    assert img.getpixel((1, 1)) == (255, 0, 0, 255)


def test_live_position_advances_only_while_playing():
    t0 = dt.datetime(2026, 10, 20, 12, 0, 0, tzinfo=dt.timezone.utc)
    later = t0 + dt.timedelta(seconds=7)
    assert wm.live_position(30, t0, later, True, 200) == 37
    assert wm.live_position(30, t0, later, False, 200) == 30
    assert wm.live_position(198, t0, later, True, 200) == 200      # clamped to the end
    assert wm.live_position(30, None, later, True, 0) == 30        # unknown update time / duration
    assert wm.live_position(30, dt.datetime(2026, 1, 1), later, True, 0) == 30   # naive vs aware


def test_app_label_and_flag():
    assert wm.app_label("Spotify.exe") == "Spotify"
    assert wm.app_label("Microsoft.ZuneMusic_8wekyb3d8bbwe!Microsoft.ZuneMusic") == "Media Player"
    assert wm.app_label("Chromium.Q2W3E4R5T6Y7U8I9O0PASDFGHJ") == "Chromium"
    assert wm.app_label("") == "Media"
    assert wm.flag(None) == -1 and wm.flag(True) == 1 and wm.flag(2) == 2 and wm.flag("x") == -1


def test_image_from_bytes_caps_size_and_keeps_alpha():
    import io

    from pandadeck.platform._util import ART_MAX_PX, art_key, image_from_bytes
    buf = io.BytesIO()
    Image.new("RGBA", (1200, 900), (1, 2, 3, 100)).save(buf, "PNG")
    img = image_from_bytes(buf.getvalue())
    assert img.mode == "RGBA" and max(img.size) == ART_MAX_PX
    assert image_from_bytes(b"not an image") is None and image_from_bytes(None) is None
    assert art_key(b"a") != art_key(b"b") and art_key(None) == ""


def test_pick_session_prefers_current_unless_something_else_plays():
    playing = {"spotify", "chrome"}

    def is_playing(s):
        return s in playing
    assert wm.pick_session("chrome", ["spotify", "chrome"], is_playing) == "chrome"
    playing.discard("chrome")
    assert wm.pick_session("chrome", ["chrome", "spotify"], is_playing) == "spotify"
    playing.clear()
    assert wm.pick_session("chrome", ["chrome", "spotify"], is_playing) == "chrome"
    assert wm.pick_session(None, ["spotify"], is_playing) == "spotify"
    assert wm.pick_session(None, [], is_playing) is None


def test_media_app_name_from_start_menu():
    apps = [AppInfo("Vivaldi.Q2W3E4R5T6Y7U8I9O0PASDFGHJ", "Vivaldi"),
            AppInfo("Chrome._crx_abcdefghijklmnopabcdefghijklmnop", "Notes"),
            AppInfo("Microsoft.ZuneMusic_8wekyb3d8bbwe!Microsoft.ZuneMusic", "Media Player"),
            AppInfo("A.SAMESUFFIX12345", "One"), AppInfo("B.SAMESUFFIX12345", "Two")]
    assert w.media_app_name("Chrome._crx_abcdefghijklmnopabcdefghijklmnop", apps) == "Notes"
    assert w.media_app_name("Chromium.Q2W3E4R5T6Y7U8I9O0PASDFGHJ", apps) == "Vivaldi"      # fork, same hash
    assert w.media_app_name("C.SAMESUFFIX12345", apps) is None                          # ambiguous
    assert w.media_app_name("Spotify.exe", apps) is None and w.media_app_name("", apps) is None


def test_media_label_prefers_start_menu_name_and_never_fails():
    media = object.__new__(wm.WindowsMedia)
    media._name_for = lambda aumid: "Vivaldi" if aumid.startswith("Chromium.") else None
    assert media._label("Chromium.Q2W3E4R5T6Y7U8I9O0PASDFGHJ") == "Vivaldi"
    assert media._label("Spotify.exe") == "Spotify"

    def broken(_aumid):
        raise RuntimeError("boom")
    media._name_for = broken
    assert media._label("Spotify.exe") == "Spotify"
