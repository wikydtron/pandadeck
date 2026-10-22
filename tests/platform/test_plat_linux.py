"""Linux backend: usage scores, audio parsing, autostart, list/launch glue (runs on every OS)."""
import shutil
import sqlite3

import pytest

from pandadeck.platform import _desktop as desk
from pandadeck.platform import linux as lx

XBEL = """<?xml version="1.0" encoding="UTF-8"?>
<xbel version="1.0" xmlns:bookmark="http://www.freedesktop.org/standards/desktop-bookmarks"
      xmlns:mime="http://www.freedesktop.org/standards/shared-mime-info">
  <bookmark href="file:///home/u/a.txt" added="2026-10-01T10:00:00Z" modified="2026-10-01T10:00:00Z">
    <info><metadata owner="http://freedesktop.org"><mime:mime-type type="text/plain"/>
      <bookmark:applications>
        <bookmark:application name="Text Editor" exec="&apos;gnome-text-editor %u&apos;" count="5"/>
        <bookmark:application name="Firefox" exec="&apos;/snap/bin/firefox %u&apos;" count="2"/>
      </bookmark:applications></metadata></info>
  </bookmark>
  <bookmark href="file:///home/u/b.txt">
    <info><metadata owner="http://freedesktop.org"><bookmark:applications>
      <bookmark:application name="Text Editor" exec="&apos;gnome-text-editor %u&apos;" count="3"/>
    </bookmark:applications></metadata></info>
  </bookmark>
</xbel>
"""


def test_parse_gnome_app_state_real_file(fixtures):
    text = (fixtures / "linux" / "application_state").read_text(encoding="utf-8")
    scores = lx.parse_gnome_app_state(text)
    assert scores["firefox_firefox.desktop"] == pytest.approx(5000.5)
    assert scores["google-chrome.desktop"] == pytest.approx(12.25)
    assert lx.parse_gnome_app_state("<not xml") == {}


def test_parse_xbel_counts_by_program():
    assert lx.parse_xbel_apps(XBEL) == {"gnome-text-editor": 8, "firefox": 2}
    assert lx.parse_xbel_apps("garbage") == {}


@pytest.mark.parametrize("value,prog", [
    ("'gnome-text-editor %u'", "gnome-text-editor"), ("/usr/bin/google-chrome-stable %U", "google-chrome-stable"),
    ("env FOO=1 /opt/app/bin/app --x", "app"), ("", ""),
])
def test_exec_basename(value, prog):
    assert lx.exec_basename(value) == prog


def test_kde_scores_from_activity_db(tmp_path):
    db = tmp_path / "database"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE ResourceScoreCache (usedActivity TEXT, initiatingAgent TEXT, "
                 "targettedResource TEXT, scoreType INT, cachedScore REAL, firstUpdate INT, lastUpdate INT)")
    conn.executemany("INSERT INTO ResourceScoreCache VALUES (?,?,?,?,?,?,?)", [
        ("a1", "x", "applications:org.kde.dolphin", 0, 10.5, 0, 0),
        ("a2", "x", "applications:org.kde.dolphin", 0, 1.5, 0, 0),
        ("a1", "x", "applications:firefox.desktop", 0, 4.0, 0, 0),
        ("a1", "x", "file:///home/u/doc.txt", 0, 99.0, 0, 0),
    ])
    conn.commit()
    conn.close()
    assert lx.kde_scores(db) == {"org.kde.dolphin.desktop": 12.0, "firefox.desktop": 4.0}
    assert lx.kde_scores(tmp_path / "missing") == {}


def test_score_entries_sums_sources_and_sorts():
    entries = {
        "firefox_firefox.desktop": desk.DesktopEntry("firefox_firefox.desktop", "/f", "Firefox",
                                                     "/snap/bin/firefox %u"),
        "org.gnome.TextEditor.desktop": desk.DesktopEntry("org.gnome.TextEditor.desktop", "/t", "Text Editor",
                                                         "gnome-text-editor %U"),
        "obs.desktop": desk.DesktopEntry("obs.desktop", "/o", "OBS Studio", "obs"),
    }
    apps = lx.score_entries(entries, {"firefox_firefox.desktop": 100.0}, {"obs.desktop": 1.0},
                            {"gnome-text-editor": 8, "firefox": 2})
    assert [(a.id, a.score) for a in apps] == [("firefox_firefox.desktop", 102.0),
                                               ("org.gnome.TextEditor.desktop", 8.0), ("obs.desktop", 1.0)]


# ---- audio -----------------------------------------------------------------------------------------------

PACTL_VOL = ("Volume: front-left: 65536 / 100% / 0.00 dB,   front-right: 52429 /  80% / -5.81 dB\n"
             "        balance -0.20\n")


def test_pactl_and_wpctl_parsers():
    assert lx.parse_pactl_volume(PACTL_VOL) == 90
    assert lx.parse_pactl_volume("") == -1 and lx.parse_pactl_volume(None) == -1
    assert lx.parse_pactl_mute("Mute: yes\n") is True and lx.parse_pactl_mute("Mute: no") is False
    assert lx.parse_pactl_mute("Stumm: ja") is None
    assert lx.parse_wpctl_volume("Volume: 1.00\n") == (100, False)
    assert lx.parse_wpctl_volume("Volume: 0.35 [MUTED]\n") == (35, True)
    assert lx.parse_wpctl_volume("error") is None


def test_pactl_audio_uses_c_locale_and_caches(monkeypatch):
    calls = []
    state = {"mute": "no"}

    def fake_run(args, timeout=5.0, c_locale=False, **kw):
        calls.append((tuple(args), c_locale))
        if args[1] == "get-sink-volume":
            return PACTL_VOL
        if args[1] == "get-source-mute":
            return f"Mute: {state['mute']}\n"
        state["mute"] = "yes" if state["mute"] == "no" else "no"
        return ""
    monkeypatch.setattr(lx, "run", fake_run)
    audio = lx.PactlAudio("/usr/bin/pactl")
    assert audio.volume() == 90 and audio.volume() == 90
    assert sum(1 for c in calls if c[0][1] == "get-sink-volume") == 1      # cached within the TTL
    assert audio.mic_muted() is False
    audio.toggle_mic()
    assert ("/usr/bin/pactl", "set-source-mute", "@DEFAULT_SOURCE@", "toggle") in [c[0] for c in calls]
    assert audio.mic_muted() is True                                       # cache cleared by the toggle
    assert all(c[1] for c in calls if "get" in c[0][1])


def test_wpctl_audio(monkeypatch):
    monkeypatch.setattr(lx, "run", lambda args, **kw: "Volume: 0.42 [MUTED]" if "SOURCE" in args[2]
                        else "Volume: 0.42")
    audio = lx.WpctlAudio("/usr/bin/wpctl")
    assert audio.volume() == 42 and audio.mic_muted() is True


def test_audio_picks_pactl_then_wpctl_then_none(tmp_path, monkeypatch, which_factory):
    monkeypatch.setattr(lx, "run", lambda args, **kw: "Volume: 0.5" if "wpctl" in args[0] else None)
    plat = lx.LinuxPlatform(home=tmp_path, env={}, which=which_factory("pactl", "wpctl"))
    assert isinstance(plat.audio(), lx.WpctlAudio)                  # pactl present but failing
    assert lx.LinuxPlatform(home=tmp_path, env={}, which=which_factory()).audio() is None


# ---- autostart -------------------------------------------------------------------------------------------------

def test_autostart_desktop_file_round_trip(tmp_path):
    plat = lx.LinuxPlatform(home=tmp_path, env={}, which=lambda _n: None)
    argv = ["/home/u/My Apps/venv/bin/python", "-m", "pandadeck", "--name=100%"]
    assert plat.autostart_enabled() is False
    plat.set_autostart(True, argv)
    path = tmp_path / ".config" / "autostart" / "pandadeck.desktop"
    text = path.read_text(encoding="utf-8")
    raw = desk.parse_group(text)
    assert raw["Type"] == "Application" and raw["X-GNOME-Autostart-enabled"] == "true"
    assert desk.expand_field_codes(desk.split_exec(desk.unescape(raw["Exec"]))) == argv
    assert plat.autostart_enabled() is True
    path.write_text(text.replace("Hidden=false", "Hidden=true"), encoding="utf-8")   # user disabled it
    assert plat.autostart_enabled() is False
    plat.set_autostart(False, argv)
    assert not path.exists() and plat.autostart_enabled() is False


def test_autostart_respects_xdg_config_home(tmp_path):
    cfg = tmp_path / "cfg"
    plat = lx.LinuxPlatform(home=tmp_path, env={"XDG_CONFIG_HOME": str(cfg)}, which=lambda _n: None)
    plat.set_autostart(True, ["pandadeck"])
    assert (cfg / "autostart" / "pandadeck.desktop").is_file()
    assert lx.autostart_active("[Desktop Entry]\nExec=x\nX-GNOME-Autostart-enabled=false\n") is False


# ---- list / launch glue ---------------------------------------------------------------------------------------

def linux_home(tmp_path, fixtures):
    home = tmp_path / "home"
    share = home / ".local" / "share"
    (share / "gnome-shell").mkdir(parents=True)
    shutil.copy(fixtures / "linux" / "application_state", share / "gnome-shell" / "application_state")
    sysdir = tmp_path / "usr-share"
    shutil.copytree(fixtures / "linux" / "usr-share", sysdir)
    snap = tmp_path / "snap"
    shutil.copytree(fixtures / "linux" / "snap-desktop", snap)
    env = {"XDG_CURRENT_DESKTOP": "ubuntu:GNOME", "LANG": "fr_CA.UTF-8"}
    return home, env


@pytest.fixture
def fake_dirs(tmp_path, monkeypatch):
    """Only our fixture dirs (no real /usr/share, flatpak or snap dirs of the test machine;
    also sidesteps ':' in Windows paths for XDG_DATA_DIRS)."""
    monkeypatch.setattr(desk, "data_dirs", lambda env, home: [home / ".local" / "share", tmp_path / "usr-share",
                                                              tmp_path / "snap"])


def test_list_apps_scores_from_gnome_shell(tmp_path, fixtures, which_factory, fake_dirs):
    home, env = linux_home(tmp_path, fixtures)
    plat = lx.LinuxPlatform(home=home, env=env, which=which_factory("gnome-terminal"))
    apps = plat.list_apps()
    ids = [a.id for a in apps]
    assert ids[:3] == ["firefox_firefox.desktop", "google-chrome.desktop", "com.obsproject.Studio.desktop"]
    assert "org.gnome.Terminal.desktop" in ids and "vim.desktop" not in ids and "python3.12.desktop" not in ids


def test_launch_validates_and_falls_back_from_gtk_launch(tmp_path, fixtures, which_factory, monkeypatch,
                                                        fake_dirs):
    home, env = linux_home(tmp_path, fixtures)
    spawned = []

    class Proc:
        def __init__(self, rc):
            self.rc = rc

        def wait(self, timeout=None):
            return self.rc

    rc = {"gtk": 1}

    def fake_spawn(argv, cwd=None, env=None):
        spawned.append(argv)
        return Proc(rc["gtk"] if argv[0].endswith("gtk-launch") else 0)
    monkeypatch.setattr(lx, "spawn", fake_spawn)

    plat = lx.LinuxPlatform(home=home, env=env, which=which_factory("gtk-launch"))
    with pytest.raises(ValueError):
        plat.launch_app("../../etc/passwd")
    assert spawned == []
    plat.launch_app("google-chrome.desktop")              # gtk-launch fails -> parsed Exec
    assert spawned == [["/usr/bin/gtk-launch", "google-chrome.desktop"], ["/usr/bin/google-chrome-stable"]]
    rc["gtk"] = 0
    spawned.clear()
    plat.launch_app("org.gnome.Calculator.desktop")      # gtk-launch succeeds -> done
    assert spawned == [["/usr/bin/gtk-launch", "org.gnome.Calculator.desktop"]]
    spawned.clear()
    nogtk = lx.LinuxPlatform(home=home, env=env, which=which_factory())
    nogtk.launch_app("firefox_firefox.desktop")
    assert spawned == [["/snap/bin/firefox"]]


def test_app_icon_unknown_id_is_none(tmp_path):
    plat = lx.LinuxPlatform(home=tmp_path, env={"XDG_DATA_DIRS": "/nonexistent"}, which=lambda _n: None)
    assert plat.app_icon("nope.desktop") is None
