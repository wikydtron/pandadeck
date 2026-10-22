"""Linux .desktop parsing / discovery / Exec handling (pure; runs on every OS).

Fixtures under fixtures/linux/ are real files from an Ubuntu 24.04 GNOME box."""
import shutil

import pytest

from pandadeck.platform import _desktop as d

GNOME = ["ubuntu", "GNOME"]


def test_parse_group_stops_at_actions_and_first_key_wins():
    text = ("# comment\n[Desktop Entry]\nName=App\nName=Dupe\nExec=app %U\n\n"
            "[Desktop Action new]\nName=New Window\nExec=app --new\n")
    raw = d.parse_group(text)
    assert raw == {"Name": "App", "Exec": "app %U"}


def test_unescape_escape_round_trip():
    assert d.unescape(r"a\sb\tc\\d\ne\x") == "a b\tc\\d\ne\\x"
    for s in ("plain", "back\\slash", "tab\tnew\nline"):
        assert d.unescape(d.escape(s)) == s


@pytest.mark.parametrize("env,expected", [
    ({"LANG": "fr_CA.UTF-8"}, ["fr_CA", "fr"]),
    ({"LANG": "sr_RS.UTF-8@latin"}, ["sr_RS@latin", "sr_RS", "sr@latin", "sr"]),
    ({"LC_ALL": "de", "LANG": "fr_CA.UTF-8"}, ["de"]),
    ({"LANG": "C.UTF-8"}, []),
    ({}, []),
])
def test_locale_candidates(env, expected):
    assert d.locale_candidates(env) == expected


def test_localized_name_prefers_most_specific():
    raw = {"Name": "Files", "Name[fr]": "Fichiers", "Name[fr_CA]": "Fichiers (CA)"}
    assert d.localized(raw, "Name", ["fr_CA", "fr"]) == "Fichiers (CA)"
    assert d.localized(raw, "Name", ["fr_BE", "fr"]) == "Fichiers"
    assert d.localized(raw, "Name", ["de"]) == "Files"


@pytest.mark.parametrize("value,args", [
    ("/usr/bin/google-chrome-stable %U", ["/usr/bin/google-chrome-stable", "%U"]),
    ('"/opt/My App/run" --flag "a \\"q\\" \\$HOME \\\\ b" ""', ["/opt/My App/run", "--flag", 'a "q" $HOME \\ b', ""]),
    ("  spaced   out  ", ["spaced", "out"]),
])
def test_split_exec(value, args):
    assert d.split_exec(value) == args


def test_split_exec_rejects_unterminated_quote():
    with pytest.raises(ValueError):
        d.split_exec('app "oops')


def test_field_codes_are_stripped_or_expanded():
    args = ["app", "%U", "--icon-arg", "%i", "--name=%c", "--file=%f", "100%%", "%k", "%F"]
    out = d.expand_field_codes(args, name="My App", icon="my-icon", location="/x/app.desktop")
    assert out == ["app", "--icon-arg", "--icon", "my-icon", "--name=My App", "--file=", "100%", "/x/app.desktop"]
    assert d.expand_field_codes(["app", "%i"]) == ["app"]          # no Icon= -> %i vanishes


def test_exec_value_round_trips_through_the_parser():
    for argv in (["/usr/bin/python3", "-m", "pandadeck"],
                 ["/home/me/My Apps/pandadeck", "--flag=100%", 'say "hi"', "$HOME", "back\\slash", "tab\tx"]):
        value = d.exec_value(argv)
        parsed = d.expand_field_codes(d.split_exec(d.unescape(value)))
        assert parsed == argv


def test_terminal_command_uses_first_available_terminal(which_factory):
    assert d.terminal_command(["vim"], which_factory("gnome-terminal", "xterm")) == \
        ["/usr/bin/gnome-terminal", "--", "vim"]
    assert d.terminal_command(["vim"], which_factory("x-terminal-emulator")) == \
        ["/usr/bin/x-terminal-emulator", "-e", "vim"]
    assert d.terminal_command(["vim"], which_factory()) == ["vim"]   # no terminal: run bare


def test_data_dirs_defaults_env_and_store_exports(tmp_path):
    dirs = d.data_dirs({}, tmp_path)
    assert dirs[0] == tmp_path / ".local" / "share"
    names = [p.as_posix() for p in dirs]
    assert "/usr/local/share" in names and "/usr/share" in names
    assert names[-1] == "/var/lib/snapd/desktop" and "/var/lib/flatpak/exports/share" in names
    custom = d.data_dirs({"XDG_DATA_HOME": "/h", "XDG_DATA_DIRS": "/a:/b:/a"}, tmp_path)
    assert [p.as_posix() for p in custom[:3]] == ["/h", "/a", "/b"]


def real_tree(tmp_path, fixtures):
    """Copy the real Ubuntu files into an XDG layout: usr/share + snap + a user override dir."""
    usr = tmp_path / "usr-share"
    shutil.copytree(fixtures / "linux" / "usr-share", usr)
    snap = tmp_path / "snap"
    shutil.copytree(fixtures / "linux" / "snap-desktop", snap)
    user = tmp_path / "home-share"
    (user / "applications").mkdir(parents=True)
    return user, usr, snap


def test_scan_real_ubuntu_files(tmp_path, fixtures, which_factory):
    user, usr, snap = real_tree(tmp_path, fixtures)
    which = which_factory("gnome-terminal", "vim")
    found = d.scan([user, usr, snap], GNOME, ["fr_CA", "fr"], which)
    assert set(found) == {"google-chrome.desktop", "org.gnome.Calculator.desktop", "org.gnome.Terminal.desktop",
                          "com.obsproject.Studio.desktop", "vim.desktop", "firefox_firefox.desktop"}
    # NoDisplay (python3.12, nm-applet, gnome-sound-panel) are gone.
    chrome = found["google-chrome.desktop"]
    assert chrome.name == "Google Chrome"                      # actions' Name[fr] doesn't leak in
    assert chrome.command() == ["/usr/bin/google-chrome-stable"]
    assert chrome.icon == "google-chrome"
    ff = found["firefox_firefox.desktop"]
    assert ff.icon == "/snap/firefox/current/default256.png" and ff.command() == ["/snap/bin/firefox"]
    vim = found["vim.desktop"]
    assert vim.terminal and vim.command(which_factory("gnome-terminal")) == ["/usr/bin/gnome-terminal", "--", "vim"]


def test_scan_honours_only_show_in_and_try_exec(tmp_path, fixtures, which_factory):
    user, usr, snap = real_tree(tmp_path, fixtures)
    found = d.scan([user, usr, snap], [], [], which_factory())   # unknown desktop, nothing installed
    assert "org.gnome.Terminal.desktop" not in found               # OnlyShowIn=GNOME;Unity
    assert "vim.desktop" not in found                              # TryExec=vim missing
    assert "google-chrome.desktop" in found
    kde = d.scan([user, usr, snap], ["KDE"], [], which_factory("gnome-terminal"))
    assert "org.gnome.Terminal.desktop" not in kde


def test_user_override_and_hidden_mask_lower_dirs(tmp_path, fixtures, which_factory):
    user, usr, snap = real_tree(tmp_path, fixtures)
    apps = user / "applications"
    (apps / "google-chrome.desktop").write_text(
        "[Desktop Entry]\nType=Application\nName=Chrome (mine)\nExec=chrome --mine\n", encoding="utf-8")
    (apps / "org.gnome.Calculator.desktop").write_text("[Desktop Entry]\nHidden=true\n", encoding="utf-8")
    (apps / "kde4").mkdir()
    (apps / "kde4" / "kate.desktop").write_text("[Desktop Entry]\nType=Application\nName=Kate\nExec=kate %U\n",
                                                encoding="utf-8")
    found = d.scan([user, usr, snap], GNOME, [], which_factory())
    assert found["google-chrome.desktop"].name == "Chrome (mine)"
    assert "org.gnome.Calculator.desktop" not in found               # hidden by the user copy
    assert found["kde4-kate.desktop"].command() == ["kate"]          # sub-dir -> dashed desktop id


@pytest.mark.parametrize("text,visible", [
    ("[Desktop Entry]\nType=Link\nName=L\nURL=https://x\n", False),
    ("[Desktop Entry]\nType=Application\nName=NoExec\n", False),
    ("[Desktop Entry]\nType=Application\nName=Bus\nDBusActivatable=true\n", True),
    ("[Desktop Entry]\nType=Application\nName=X\nExec=x\nNotShowIn=GNOME;\n", False),
    ("[Desktop Entry]\nType=Application\nExec=x\n", False),            # no Name
])
def test_visibility_rules(text, visible, which_factory):
    entry = d.parse_entry(text, "x.desktop", "/x.desktop")
    assert entry.visible(GNOME, which_factory()) is visible


def test_command_rejects_empty_exec():
    entry = d.parse_entry("[Desktop Entry]\nType=Application\nName=X\nExec=%U\n", "x.desktop", "/x.desktop")
    with pytest.raises(ValueError):
        entry.command()
