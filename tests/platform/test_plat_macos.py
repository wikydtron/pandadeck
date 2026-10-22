"""macOS backend: bundles, Spotlight, osascript parsing, audio, autostart (runs on every OS)."""
import datetime as dt
import io
import plistlib

import pytest
from PIL import Image

from pandadeck.platform import _mac_media as mm
from pandadeck.platform import macos as mac

SEP = mm.SEP


def make_bundle(root, file_name, info, icns_name=None, icon_px=128):
    bundle = root / file_name
    (bundle / "Contents" / "Resources").mkdir(parents=True)
    with open(bundle / "Contents" / "Info.plist", "wb") as f:
        plistlib.dump(info, f, fmt=plistlib.FMT_BINARY)
    if icns_name:
        img = Image.new("RGBA", (icon_px, icon_px), (30, 120, 220, 255))
        try:
            img.save(bundle / "Contents" / "Resources" / icns_name, format="ICNS")
        except Exception:  # noqa: BLE001 - old Pillow without ICNS writing
            pytest.skip("this Pillow can't write .icns")
    return bundle


# ---- bundles -------------------------------------------------------------------------------------

def test_scan_lists_bundles_with_finder_names_and_skips_agents(tmp_path):
    apps = tmp_path / "Applications"
    apps.mkdir()
    make_bundle(apps, "Visual Studio Code.app", {"CFBundleName": "Code", "CFBundleIdentifier": "com.microsoft.VSCode",
                                                 "CFBundlePackageType": "APPL", "CFBundleIconFile": "Code.icns"})
    make_bundle(apps, "Helper.app", {"CFBundleName": "Helper", "LSBackgroundOnly": True})
    make_bundle(apps, "Plugin.app", {"CFBundleName": "Plugin", "CFBundlePackageType": "BNDL"})
    (apps / "Adobe Thing").mkdir()
    make_bundle(apps / "Adobe Thing", "Adobe Thing 2026.app", {"CFBundleName": "Thing"})
    (apps / "Utilities").mkdir()
    make_bundle(apps / "Utilities", "Terminal.app", {"CFBundleName": "Terminal"})
    make_bundle(apps, ".Hidden.app", {"CFBundleName": "Hidden"})
    plat = mac.MacPlatform(home=tmp_path, roots=[apps, apps / "Utilities"], which=lambda _n: None)
    found = {a.name: a for a in plat.list_apps()}
    assert set(found) == {"Visual Studio Code", "Adobe Thing 2026", "Terminal"}   # Utilities once
    assert found["Visual Studio Code"].id == str(apps / "Visual Studio Code.app")
    assert all(a.score == 0 for a in found.values())          # no mdls here


def test_bundle_name_and_launchable_rules(tmp_path):
    assert mac.bundle_name(tmp_path / "Safari.app", {"CFBundleName": "Safari"}) == "Safari"
    assert mac.bundle_name(tmp_path / ".app", {"CFBundleDisplayName": "Shown"}) == "Shown"
    assert mac.is_launchable({}) and mac.is_launchable({"CFBundlePackageType": "APPL"})
    assert not mac.is_launchable({"LSBackgroundOnly": True}) and not mac.is_launchable({"CFBundlePackageType": "FMWK"})
    assert mac.read_info_plist(tmp_path / "Nope.app") == {}


def test_icns_lookup_and_icon_loading(tmp_path, monkeypatch):
    apps = tmp_path / "Applications"
    apps.mkdir()
    b = make_bundle(apps, "Paint.app", {"CFBundleIconFile": "PaintIcon"}, icns_name="PaintIcon.icns")
    assert mac.icns_path(b, {"CFBundleIconFile": "PaintIcon"}) == b / "Contents" / "Resources" / "PaintIcon.icns"
    assert mac.icns_path(b, {}) == b / "Contents" / "Resources" / "PaintIcon.icns"   # the only .icns
    plat = mac.MacPlatform(home=tmp_path, roots=[apps], which=lambda _n: None)
    img = plat.app_icon(str(b))
    assert img is not None and img.mode == "RGBA" and max(img.size) <= 256
    assert plat.app_icon(str(tmp_path / "Unknown.app")) is None


def test_launch_only_discovered_bundles(tmp_path, monkeypatch):
    apps = tmp_path / "Applications"
    apps.mkdir()
    b = make_bundle(apps, "Notes.app", {"CFBundleName": "Notes"})
    calls = []
    monkeypatch.setattr(mac, "spawn", lambda argv, **kw: calls.append(argv))
    plat = mac.MacPlatform(home=tmp_path, roots=[apps], which=lambda _n: None)
    plat.launch_app(str(b))
    assert calls == [["open", "-a", str(b)]]
    with pytest.raises(ValueError):
        plat.launch_app("/Applications/Calculator.app; rm -rf ~")


# ---- Spotlight -------------------------------------------------------------------------------------

MDLS = """kMDItemLastUsedDate = 2026-10-19 08:30:00 +0000
kMDItemUseCount     = 42
kMDItemLastUsedDate = (null)
kMDItemUseCount     = (null)
kMDItemLastUsedDate = 2026-01-01 00:00:00 +0000
kMDItemUseCount     = 3
"""


def test_parse_mdls_and_score():
    rows = mac.parse_mdls(MDLS, 4)
    assert rows[0] == (42, dt.datetime(2026, 10, 19, 8, 30, tzinfo=dt.timezone.utc))
    assert rows[1] == (None, None) and rows[3] == (None, None)       # padded
    now = dt.datetime(2026, 10, 20, 8, 30, tzinfo=dt.timezone.utc)
    s0, s1, s2 = (mac.usage_score(c, d, now) for c, d in rows[:3])
    assert s0 == pytest.approx(42 + 1 - 1 / 90, abs=1e-3) and s1 == 0 and s2 == 3.0   # old -> no bonus


# ---- osascript media ---------------------------------------------------------------------------------

def spotify_line(state="playing", pos_ms="61500", art="https://i.scdn.co/image/ab67"):
    return SEP.join([state, "Song", "Artist", "missing value", "231480", pos_ms, "true", "false", art,
                     "spotify:track:1a2b"]) + "\n"


def test_parse_status_spotify_music_and_stopped():
    sp = mm.parse_status("Spotify", spotify_line())
    assert (sp["status"], sp["title"], sp["album"], sp["dur"], sp["pos"], sp["shuffle"], sp["repeat"]) == \
        ("Playing", "Song", "", 231.48, 61.5, 1, 0)
    music = mm.parse_status("Music", SEP.join(["paused", "Track", "A", "B", "200000", "1000", "false", "one", "",
                                               "ABCDEF0123"]))
    assert music["status"] == "Paused" and music["repeat"] == 1 and music["shuffle"] == 0
    assert music["track_id"] == "ABCDEF0123"
    assert mm.parse_status("Music", "stopped\n")["status"] == "Stopped"
    assert mm.parse_status("Spotify", None) is None
    assert mm.parse_status("Spotify", spotify_line(pos_ms="1,5e3"))["pos"] == 1.5     # decimal comma locale


def test_scripts_never_launch_or_assume_a_track():
    for app, script in mm.SCRIPTS.items():
        assert f'tell application "{app}"' in script and 'if st is "stopped" then return st' in script
        guard = script.index(f'if not (application "{app}" is running) then return "stopped"')
        assert guard < script.index(f'tell application "{app}"')      # checked before any tell
        assert "try" in script and "character id 31" in script
    assert 'if not (application "Music" is running)' in mm.MUSIC_ART_SCRIPT
    assert "artwork url" in mm.SCRIPTS["Spotify"] and "song repeat" in mm.SCRIPTS["Music"]


def test_command_scripts():
    assert mm.command_script("Spotify", "next") == \
        'if application "Spotify" is running then\ntell application "Spotify"\nnext track\nend tell\nend if'
    assert "set player position to 42.500" in mm.command_script("Music", "seek:42.5")
    assert "set song repeat to all" in mm.command_script("Music", "repeat")
    assert mm.command_script("Spotify", "seek:nan") is None and mm.command_script("Spotify", "seek:x") is None
    assert mm.command_script("Spotify", "rm -rf") is None


def test_parse_pgrep():
    assert mm.parse_pgrep("123 Spotify\n456 Music\n") == {"Spotify", "Music"}
    assert mm.parse_pgrep(None) == set()


def png_bytes(color):
    buf = io.BytesIO()
    Image.new("RGB", (32, 32), color).save(buf, "PNG")
    return buf.getvalue()


def test_mac_media_prefers_the_playing_app_and_fetches_spotify_art(monkeypatch):
    outputs = {"pgrep": "1 Spotify\n2 Music\n", "Spotify": spotify_line(),
               "Music": SEP.join(["paused", "Other", "", "", "0", "0", "false", "off", "", "ID1"])}
    calls = []

    def fake_run(args, timeout=5.0, **kw):
        calls.append(args)
        if args[0] == "pgrep":
            return outputs["pgrep"]
        script = args[2]
        if "next track" in script:
            return ""
        return outputs["Spotify"] if 'application "Spotify"' in script else outputs["Music"]

    fetched = []
    monkeypatch.setattr(mm, "run", fake_run)
    monkeypatch.setattr(mm, "fetch_url", lambda url, **kw: fetched.append(url) or png_bytes((1, 2, 3)))
    media = mm.MacMedia()
    np = media.now_playing()
    assert np.app == "Spotify" and np.playing and np.title == "Song" and np.extra["track_id"] == "spotify:track:1a2b"
    assert np.art is not None and np.art_key and fetched == ["https://i.scdn.co/image/ab67"]
    media.now_playing()
    assert fetched == ["https://i.scdn.co/image/ab67"]          # cached per URL
    media.command("next")
    assert calls[-1] == ["osascript", "-e", mm.command_script("Spotify", "next")]


def test_mac_media_nothing_running(monkeypatch):
    monkeypatch.setattr(mm, "run", lambda args, **kw: None)
    assert mm.MacMedia().now_playing() is None


def test_failing_osascript_backs_off(monkeypatch):
    calls = []

    def fake_run(args, timeout=5.0, **kw):
        calls.append(args[0])
        return "1 Spotify\n" if args[0] == "pgrep" else None     # Automation denied / hung
    monkeypatch.setattr(mm, "run", fake_run)
    media = mm.MacMedia()
    assert media.now_playing() is None and media.now_playing() is None
    assert calls.count("osascript") == 1                            # second poll skipped it


# ---- audio ------------------------------------------------------------------------------------------------

def test_parse_volume_settings():
    assert mac.parse_volume_settings("50,75,false\n") == (50, 75, False)
    assert mac.parse_volume_settings("missing value,missing value,missing value") == (-1, None, None)
    assert mac.parse_volume_settings(None) == (-1, None, None)


def test_mac_audio_mute_restores_previous_input_level(monkeypatch):
    state = {"in": 60}
    sent = []

    def fake_run(args, timeout=5.0, **kw):
        script = args[2]
        if script.startswith("set volume input volume"):
            state["in"] = int(script.rsplit(" ", 1)[1])
            sent.append(state["in"])
            return ""
        return f"30,{state['in']},false"
    monkeypatch.setattr(mac, "run", fake_run)
    audio = mac.MacAudio()
    assert audio.volume() == 30 and audio.mic_muted() is False
    audio.toggle_mic()
    assert audio.mic_muted() is True and sent == [0]
    audio.toggle_mic()
    assert audio.mic_muted() is False and sent == [0, 60]


# ---- autostart ------------------------------------------------------------------------------------------------

def test_launch_agent_written_to_home_and_removed(tmp_path):
    plat = mac.MacPlatform(home=tmp_path, roots=[], which=lambda _n: None)
    assert plat.autostart_enabled() is False
    argv = ["/Applications/PandaDeck.app/Contents/MacOS/PandaDeck", "--headless"]
    plat.set_autostart(True, argv)
    path = tmp_path / "Library" / "LaunchAgents" / "com.pandadeck.app.plist"
    data = plistlib.loads(path.read_bytes())
    assert data["Label"] == "com.pandadeck.app" and data["ProgramArguments"] == argv
    assert data["RunAtLoad"] is True and data["KeepAlive"] is False
    assert plat.autostart_enabled() is True
    plat.set_autostart(False, argv)
    assert not path.exists() and plat.autostart_enabled() is False
    plat.set_autostart(False, argv)                        # idempotent
    with pytest.raises(ValueError):
        plat.set_autostart(True, [])
