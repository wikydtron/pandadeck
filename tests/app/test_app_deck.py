"""Deck: grammar, built-in pages per OS, APPS board, custom pages, launching."""
import json
import time

import pytest

from pandadeck.deck import board as bd
from pandadeck.deck import grammar as g
from pandadeck.platform.base import AppInfo

FIRMWARE_PAGE_FIELDS = {"name", "color", "cols", "buttons"}


# ---- grammar --------------------------------------------------------------------------------

@pytest.mark.parametrize("raw,norm", [
    ("keys:ctrl+shift+esc", "keys:CTRL+SHIFT+ESC"), ("keys: GUI + l ", "keys:GUI+L"), ("keys:f13", "keys:F13"),
    ("keys:CTRL+1", "keys:CTRL+1"), ("keys:CTRL+.", "keys:CTRL+."), ("media:PlayPause", "media:playpause"),
    ("app:Microsoft.WindowsTerminal_8wekyb3d8bbwe!App", "app:Microsoft.WindowsTerminal_8wekyb3d8bbwe!App"),
    ("agent:mic", "agent:mic"), ("KEYS:A", "keys:A")])
def test_validate_action_normalizes(raw, norm):
    assert g.validate_action(raw) == norm


@pytest.mark.parametrize("raw", ["", "keys:", "keys:CTRL+", "keys:FOO", "keys:F25", "keys:A+B+C+D+E+F+G",
                                 "media:louder", "app:", "agent:" + "x" * 40, "shell:rm -rf /", "launch:x", 5,
                                 "app:a\nb"])
def test_validate_action_rejects(raw):
    with pytest.raises(g.ActionError):
        g.validate_action(raw)


def test_labels_and_colors():
    assert g.ascii_label("Café ☃ Tools for Everyone!!") == "Cafe  Tools for Eve"
    assert g.ascii_label("Name™") == "NameTM"  # NFKD folds what it can
    assert g.validate_color("#d97757") == "#D97757" and g.validate_color("") == ""
    with pytest.raises(g.ActionError):
        g.validate_color("red")
    with pytest.raises(g.ActionError):
        g.validate_icon("unicorn")


# ---- built-in pages -------------------------------------------------------------------------

@pytest.mark.parametrize("os_name", ["windows", "macos", "linux"])
def test_builtin_pages_fit_the_firmware(os_name):
    defaults = bd.load_defaults()
    for pid in ("media", "windows", "dev", "macros"):
        page = bd.builtin_page(defaults, pid, os_name)
        assert set(page) >= {"name", "color", "buttons"} and len(page["name"]) <= g.PAGE_NAME_MAX
        assert len(page["buttons"]) <= g.MAX_BUTTONS
        for b in page["buttons"]:
            assert len(b["label"]) <= g.LABEL_MAX and b["icon"] in g.ICON_NAMES
            assert g.validate_action(b["action"]) == b["action"], b
            assert len(b["action"]) <= g.ACTION_MAX
    assert set(defaults["launch"][os_name]) >= {"spotify", "terminal", "files", "taskmgr", "settings"}


def test_macos_uses_cmd_and_mission_control():
    d = bd.load_defaults()
    mac = {b["label"]: b["action"] for b in bd.builtin_page(d, "windows", "macos")["buttons"]}
    assert mac["Mission"] == "keys:CTRL+UP" and mac["Quit App"] == "keys:GUI+Q"
    media = {b["label"]: b["action"] for b in bd.builtin_page(d, "media", "macos")["buttons"]}
    assert media["Lock"] == "keys:CTRL+GUI+Q"
    dev = {b["label"]: b["action"] for b in bd.builtin_page(d, "dev", "macos")["buttons"]}
    assert dev["Copy"] == "keys:GUI+C" and dev["Redo"] == "keys:GUI+SHIFT+Z"
    win = {b["label"]: b["action"] for b in bd.builtin_page(d, "dev", "windows")["buttons"]}
    assert win["Copy"] == "keys:CTRL+C" and win["Redo"] == "keys:CTRL+Y"
    assert bd.builtin_page(d, "macros", "linux")["buttons"][0]["action"] == "keys:F13"
    assert bd.builtin_page(d, "nope", "linux") is None


# ---- pure builders ------------------------------------------------------------------------------

def test_build_deck_matches_the_old_agent():
    app_list = [{"name": f"App {i}", "app_id": f"id{i}", "key": f"a{i}"} for i in range(20)]
    extra = [bd.builtin_page(bd.load_defaults(), p, "windows") for p in ("media", "windows", "dev", "macros")]
    deck = bd.build_deck(app_list, extra)
    first = deck["pages"][0]
    assert first["name"] == "APPS" and first["cols"] == 6 and len(first["buttons"]) == 18
    assert first["buttons"][3] == {"label": "App 3", "icon": "apps", "img": "a3", "action": "agent:launch:app:a3"}
    assert len(deck["pages"]) == 5 and deck["rev"] & 0x10000000
    assert bd.build_deck(app_list, extra)["rev"] == deck["rev"]
    assert bd.build_deck([], [])["pages"] == []


def test_pick_apps_respects_choice_then_falls_back_to_usage():
    cat = [AppInfo("a", "Alpha", 1), AppInfo("b", "Beta", 9), AppInfo("c", "Gamma", 0), AppInfo("d", "beta", 5)]
    assert [a.id for a in bd.pick_apps(["c", "zz", "a"], cat)] == ["c", "a"]
    assert [a.id for a in bd.pick_apps([], cat)] == ["b", "a", "c"]  # by usage, duplicate names dropped, then padded
    assert len(bd.pick_apps([], [AppInfo(str(i), f"A{i}", 1) for i in range(30)])) == 18


def test_short_name():
    assert bd.short_name("Outlook for Windows") == "Outlook" and bd.short_name("Microsoft Teams") == "Teams"
    assert len(bd.short_name("An Extremely Long Application Name Indeed")) <= 19 and bd.short_name("☃") == "App"


def test_custom_page_translates_app_actions():
    page = {"name": "MINE", "color": "#34D399", "buttons": [
        {"label": "Term", "icon": "", "color": "#60A5FA", "action": "app:Microsoft.WindowsTerminal_8wekyb3d8bbwe!App"},
        {"label": "Rec", "icon": "camera", "color": "", "action": "keys:F13"},
        {"label": "Gone", "icon": "", "color": "", "action": "app:uninstalled"}]}
    out = bd.custom_page(page, {"Microsoft.WindowsTerminal_8wekyb3d8bbwe!App": "b0"}, {"b0"})
    assert set(out) == FIRMWARE_PAGE_FIELDS and out["cols"] == 4
    t, r, gone = out["buttons"]
    assert t == {"label": "Term", "icon": "apps", "action": "agent:launch:app:b0", "img": "b0", "color": "#60A5FA"}
    assert r == {"label": "Rec", "icon": "camera", "action": "keys:F13"}
    assert gone["action"] == "agent:noop"
    assert all(len(b["action"]) <= g.ACTION_MAX for b in out["buttons"])


# ---- the board ----------------------------------------------------------------------------------

def wait_pool(board):
    board._pool.submit(lambda: None).result(5)


def test_board_builds_apps_icons_and_order(settings, fake_platform):
    custom = {"id": "c1", "name": "MINE", "color": "#34D399", "buttons": [
        {"label": "Calc", "icon": "", "color": "", "action": "app:id.calc"},
        {"label": "Code", "icon": "", "color": "", "action": "app:id.code"}]}
    settings.update({"deck": {"pages": ["media", "apps", "c1"], "custom": [custom]}})
    b = bd.DeckBoard(settings, fake_platform, "windows")
    b._load_catalog()
    deck = b.current()
    assert [p["name"] for p in deck["pages"]] == ["MEDIA", "APPS", "MINE"]  # APPS where the user put it
    apps = deck["pages"][1]["buttons"]
    assert [a["label"] for a in apps] == ["Visual Studio Code", "Google Chrome", "Notes", "Calculator"]  # by usage
    assert b.keymap == {"a0": "id.code", "a1": "id.chrome", "a2": "id.notes", "a3": "id.calc"}
    assert "img" not in apps[3]  # no icon: the panel shows the glyph
    mine = deck["pages"][2]["buttons"]
    assert mine[0]["action"] == "agent:launch:app:a3" and "img" not in mine[0]   # calc has no icon
    assert mine[1]["action"] == "agent:launch:app:a0" and mine[1]["img"] == "a0"  # reuses the APPS icon
    imgs = b.images()
    assert set(imgs) == {"icon:a0", "icon:a1", "icon:a2"}
    hdr, data = imgs["icon:a0"]
    assert hdr == {"kind": "icon", "key": "a0", "w": 56, "h": 56, "tag": ""} and len(data) == 56 * 56 * 3
    # the red square was trimmed out of its transparent frame: corner pixel opaque
    assert data[56 * 56 * 2] == 255
    json.dumps(deck)  # serializable as-is


def test_board_uses_the_chosen_apps(settings, fake_platform):
    settings.update({"deck": {"apps": ["id.calc", "id.chrome"]}})
    b = bd.DeckBoard(settings, fake_platform, "windows")
    b._load_catalog()
    assert [x["label"] for x in b.current()["pages"][0]["buttons"]] == ["Calculator", "Google Chrome"]
    cat = b.catalog()
    assert cat[0]["id"] == "id.code" and {c["id"]: c["chosen"] for c in cat}["id.calc"] is True
    assert b.suggested() == ["id.code", "id.chrome", "id.notes", "id.calc"]  # padded past the used ones


def test_board_launches_through_the_platform(settings, fake_platform):
    b = bd.DeckBoard(settings, fake_platform, "windows")
    b._load_catalog()
    assert b.launch("a1") and not b.launch("zz")
    wait_pool(b)
    assert fake_platform.launched == ["id.chrome"]


def test_board_survives_platform_failures(settings, fake_platform):
    def boom():
        raise OSError("powershell missing")
    fake_platform.list_apps = boom
    b = bd.DeckBoard(settings, fake_platform, "windows")
    b._load_catalog()
    assert b.catalog() == [] and "powershell" in b.error and b.catalog_ready()
    names = [p["name"] for p in b.current()["pages"]]
    assert names == ["MEDIA", "WINDOWS", "SOUNDS"]  # no apps -> no APPS page, built-ins still there


def test_icon_png_is_cached(settings, fake_platform):
    calls = []
    orig = fake_platform.app_icon
    fake_platform.app_icon = lambda i: calls.append(i) or orig(i)
    b = bd.DeckBoard(settings, fake_platform, "windows")
    assert b.icon_png("id.code")[:4] == b"\x89PNG" and b.icon_png("id.code") and calls == ["id.code"]
    assert b.icon_png("id.calc") is None


def test_launch_named_targets(settings, fake_platform, monkeypatch):
    from pandadeck import osutil
    seen = []
    monkeypatch.setattr(osutil, "open_uri", lambda u: seen.append(("uri", u)) or True)
    monkeypatch.setattr(osutil, "spawn", lambda e: seen.append(("exe", e)) or True)
    b = bd.DeckBoard(settings, fake_platform, "windows")
    assert b.launch_named("spotify") and b.launch_named("terminal") and not b.launch_named("nope")
    assert seen == [("uri", "spotify:"), ("exe", "wt.exe|powershell.exe|cmd.exe")]
    mac = bd.DeckBoard(settings, fake_platform, "macos")
    launched = []
    mac._launch_app = launched.append
    assert mac.launch_named("terminal")
    wait_pool(mac)
    time.sleep(0.05)
    assert launched == ["/System/Applications/Utilities/Terminal.app"]


def test_osutil_launch_target(monkeypatch):
    from pandadeck import osutil
    seen = []
    monkeypatch.setattr(osutil, "open_uri", lambda u: seen.append(u) or True)
    assert osutil.launch_target("dir:~", lambda a: None) and seen[-1] != "~"
    assert osutil.launch_target("app:x", seen.append) and seen[-1] == "x"
    assert not osutil.launch_target("bogus:x", seen.append)
    assert not osutil.spawn("definitely-not-a-real-program-xyz")
