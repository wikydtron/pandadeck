"""settings.json + secrets.json + per-OS paths."""
import json
import os
import stat
import sys

import pytest

from pandadeck import config, paths
from pandadeck.config import DEFAULTS, Settings, SettingsError, apply_patch, clean
from pandadeck.secrets import SecretStore, validate_user_secret

TOK = "sk-ant-oat01-"  # a fake Claude token prefix

posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")


# ---- defaults / cleaning -------------------------------------------------------------------

def test_defaults_keep_ai_off():
    s = clean({})
    assert "ai" not in s["pages"]
    assert s["ai"]["claude_hooks"] is False and s["ai"]["codex_hooks"] is False
    assert s["ai"]["claude_usage"] == "off" and s["ai"]["codex_usage"] == "off"
    assert s["server"]["port"] == 47321 and s["version"] == config.VERSION


def test_invalid_values_fall_back_to_defaults():
    raw = {"panel": {"link": "carrier-pigeon", "host": "ok.lan"}, "pages": ["nope"], "startup": "yes",
           "music": {"source": "spotify", "spotify_client_id": "not-hex"}, "server": {"port": 80},
           "deck": {"apps": "x", "pages": ["apps", "media", "c1"], "custom": []}, "unknown": 1}
    s = clean(raw)
    assert s["panel"] == {"link": "auto", "host": "ok.lan"}
    assert s["pages"] == DEFAULTS["pages"] and s["startup"] is True
    assert s["music"]["source"] == "spotify" and s["music"]["spotify_client_id"] == ""
    assert s["server"]["port"] == 47321 and "unknown" not in s
    assert s["deck"]["apps"] == [] and s["deck"]["pages"] == ["apps", "media"]  # c1 doesn't exist


def test_migration_from_draft_usage_key():
    s = clean({"ai": {"usage": "url", "claude_usage_url": "http://usage.example:9000/usage"}})
    assert s["ai"]["claude_usage"] == "url" and s["ai"]["codex_usage"] == "url"
    assert s["ai"]["claude_usage_url"] == "http://usage.example:9000/usage"


def test_newer_version_is_read_best_effort():
    assert clean({"version": 99, "startup": False})["startup"] is False


# ---- patches from the settings page ------------------------------------------------------------

def test_patch_validates_and_names_the_field():
    base = clean({})
    new = apply_patch(base, {"pages": ["ai", "deck"], "music": {"backdrop": "artist"}})
    assert new["pages"] == ["ai", "deck"] and new["music"]["backdrop"] == "artist"
    assert base["pages"] == DEFAULTS["pages"]  # original untouched
    for bad, word in [({"pages": []}, "pages"), ({"pages": ["tv"]}, "pages"), ({"music": {"source": "radio"}}, "source"),
                      ({"ai": {"claude_usage_url": "ftp://x"}}, "claude_usage_url"), ({"nope": 1}, "nope"),
                      ({"ai": {"perm_wait_s": 5}}, "perm_wait_s"), ({"ai": {"claude_hooks": "true"}}, "claude_hooks"),
                      ({"deck": {"apps": [f"a{i}" for i in range(19)]}}, "apps"), ({"panel": "x"}, "panel"),
                      ({"hud": {"lab_url": "javascript:alert(1)"}}, "lab_url"), ({"server": {"port": 70000}}, "port")]:
        with pytest.raises(SettingsError, match=word):
            apply_patch(base, bad)
    with pytest.raises(SettingsError):
        apply_patch(base, ["not", "an", "object"])


def test_pages_dedupe_and_deck_limits():
    s = apply_patch(clean({}), {"pages": ["deck", "deck", "hud"], "deck": {"apps": ["x", "x", "y"]}})
    assert s["pages"] == ["deck", "hud"] and s["deck"]["apps"] == ["x", "y"]
    with pytest.raises(SettingsError, match="6 pages"):
        apply_patch(s, {"deck": {"pages": ["apps", "media", "windows", "dev", "macros", "c1", "c2"]}})


def test_custom_pages_are_validated_and_referenced():
    page = {"id": "c1", "name": "stream keys", "color": "#34d399",
            "buttons": [{"label": "Rec", "icon": "camera", "color": "", "action": "keys:ctrl + shift+r"},
                        {"label": "Café", "icon": "", "action": "app:Microsoft.WindowsTerminal_8wekyb3d8bbwe!App"},
                        {"label": "Mute", "icon": "mute", "action": "media:MUTE"}]}
    s = apply_patch(clean({}), {"deck": {"custom": [page], "pages": ["apps", "c1"]}})
    c = s["deck"]["custom"][0]
    assert c["name"] == "STREAM KEYS" and c["color"] == "#34D399"
    assert [b["action"] for b in c["buttons"]] == ["keys:CTRL+SHIFT+R", "app:Microsoft.WindowsTerminal_8wekyb3d8bbwe!App",
                                                  "media:mute"]
    assert c["buttons"][1]["label"] == "Cafe"
    assert s["deck"]["pages"] == ["apps", "c1"]
    gone = apply_patch(s, {"deck": {"custom": []}})
    assert gone["deck"]["pages"] == ["apps"]  # deleting a page removes it from the tabs
    for bad in [{"id": "x1", "name": "A", "buttons": []}, {"id": "c1", "buttons": [{"action": "launch:rm -rf"}]},
                {"id": "c1", "buttons": [{"action": "keys:CTRL+FOO"}]}, {"id": "c1", "buttons": [{"action": "keys:A",
                                                                                                   "icon": "nope"}]},
                {"id": "c1", "buttons": [{"action": "keys:" + "+".join("ABCDEFG")}]}]:
        with pytest.raises(SettingsError):
            apply_patch(clean({}), {"deck": {"custom": [bad]}})


def test_settings_store_saves_atomically_and_notifies(isolated_home):
    path = isolated_home / "settings.json"
    s = Settings(path)
    assert s.first_run and path.exists()
    seen = []
    s.on_change(lambda old, new: seen.append((old["startup"], new["startup"])))
    s.update({"startup": False})
    assert seen == [(True, False)] and json.loads(path.read_text())["startup"] is False
    s.update({"startup": False})  # no change -> no event
    assert len(seen) == 1
    assert not Settings(path).first_run
    leftovers = [p for p in isolated_home.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_bad_listener_doesnt_break_saving(isolated_home):
    s = Settings(isolated_home / "settings.json")
    s.on_change(lambda o, n: 1 / 0)
    assert s.update({"startup": False})["startup"] is False


def test_settings_file_survives_garbage(isolated_home):
    path = isolated_home / "settings.json"
    path.write_text("{not json")
    assert Settings(path).get()["pages"] == DEFAULTS["pages"]


@posix_only
def test_settings_and_secrets_are_private(isolated_home):
    Settings(isolated_home / "settings.json")
    st = SecretStore(isolated_home / "secrets.json")
    st.set("claude_token", TOK + "x")
    for name in ("settings.json", "secrets.json"):
        assert stat.S_IMODE((isolated_home / name).stat().st_mode) == 0o600


# ---- secrets ----------------------------------------------------------------------------------

def test_secret_store_roundtrip_and_presence(isolated_home):
    path = isolated_home / "secrets.json"
    st = SecretStore(path)
    st.set("claude_token", TOK + "abc")
    st.set("spotify", {"refresh_token": "r", "access_token": "a"})
    again = SecretStore(path)
    assert again.get("claude_token") == TOK + "abc"
    pres = again.presence()
    assert pres == {"claude_token": True, "lab_token": False, "panel_token": False, "spotify": True}
    assert "sk-ant" not in json.dumps(pres)
    again.clear("claude_token")
    assert not SecretStore(path).has("claude_token")


def test_hook_secret_is_created_once(isolated_home):
    st = SecretStore(isolated_home / "secrets.json")
    a = st.hook_secret()
    assert len(a) == 24 and SecretStore(isolated_home / "secrets.json").hook_secret() == a


def test_validate_user_secret():
    assert validate_user_secret("lab_token", "  abc  ") == "abc"
    assert validate_user_secret("claude_token", "") == ""
    for name, value in [("hook_secret", "x"), ("spotify", "x"), ("lab_token", "has space"), ("lab_token", 5),
                        ("lab_token", "x" * 5000), ("lab_token", "line\nbreak")]:
        with pytest.raises(ValueError):
            validate_user_secret(name, value)


# ---- paths ----------------------------------------------------------------------------------

def test_paths_per_os(monkeypatch, tmp_path):
    monkeypatch.delenv("PANDADECK_HOME", raising=False)
    monkeypatch.setattr(paths, "_home", lambda: tmp_path)
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    assert paths.config_dir("win32") == tmp_path / "Roaming" / "PandaDeck"
    assert paths.log_dir("win32") == tmp_path / "Local" / "PandaDeck" / "Logs"
    assert paths.config_dir("darwin") == tmp_path / "Library" / "Application Support" / "PandaDeck"
    assert paths.log_dir("darwin") == tmp_path / "Library" / "Logs" / "PandaDeck"
    assert paths.config_dir("linux") == tmp_path / ".config" / "pandadeck"
    assert paths.cache_dir("linux") == tmp_path / ".cache" / "pandadeck"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert paths.config_dir("linux") == tmp_path / "xdg" / "pandadeck"


def test_pandadeck_home_overrides_everything(isolated_home):
    assert paths.config_dir() == isolated_home and paths.log_dir() == isolated_home / "logs"
    assert paths.runtime_file().parent == isolated_home
    assert sys.platform  # (paths.user_home is redirected by the fixture)
    assert paths.user_home().name == "user-home"
