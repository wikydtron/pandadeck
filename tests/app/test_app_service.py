"""Service wiring: reactions to settings changes, panel connect, single instance,
optional extensions and the CLI."""
import importlib
import json
import logging
import os
import sys
import threading

import pytest

import pandadeck
from pandadeck import __main__ as cli
from pandadeck import paths
from pandadeck.ai import hooks
from pandadeck.service import (NullPlatform, Service, autostart_argv, load_extensions, running_instance,
                               settings_url)

TOK = "sk-ant-oat01-"  # a fake Claude token prefix


@pytest.fixture
def svc(fake_platform):
    s = Service(panel=False, platform=fake_platform)
    s.port = 47555
    return s


def test_hooks_follow_the_toggles_and_only_remove_on_switch_off(svc, user_home):
    claude = hooks.claude_settings_path(user_home)
    claude.parent.mkdir(parents=True)
    old = {"hooks": {"Stop": [{"hooks": [{"type": "http", "url": "http://127.0.0.1:47321/pandadeck/OLDAGENT/hook"}]}]}}
    claude.write_text(json.dumps(old))
    svc._ensure_hooks()  # hooks off by default: hooks we didn't install are left alone
    assert json.loads(claude.read_text()) == old
    svc.settings.update({"ai": {"claude_hooks": True}})
    urls = hooks.installed_urls(claude)
    assert urls and all(u == f"http://127.0.0.1:47555/pandadeck/{svc.hook_secret}/hook" for u in urls)
    svc.settings.update({"ai": {"claude_hooks": False}})
    assert hooks.installed_urls(claude) == []
    assert not svc.hook_errors


def test_hooks_are_repaired_when_the_port_moves(svc, user_home):
    svc.settings.update({"ai": {"codex_hooks": True}})
    path = hooks.codex_hooks_path(user_home)
    assert hooks.installed_urls(path)[0].endswith(":47555/pandadeck/" + svc.hook_secret + "/codex")
    svc.port = 47556
    svc._ensure_hooks()
    assert all(":47556/" in u for u in hooks.installed_urls(path))


def test_hook_errors_are_reported(svc, user_home):
    claude = hooks.claude_settings_path(user_home)
    claude.parent.mkdir(parents=True)
    claude.write_text("{broken json")
    svc.settings.update({"ai": {"claude_hooks": True}})
    assert "claude" in svc.hook_errors and claude.read_text() == "{broken json"


def test_pages_are_pushed_on_connect_and_change(svc):
    class Link:
        kind = "usb"

        def __init__(self):
            self.configs = []

        def config(self, d):
            self.configs.append(d)
            return {"ok": True, "settings": {"pages": d["pages"]}}

    link = Link()
    svc.links.current = lambda: link
    svc._on_connect(link, {"settings": {"pages": ["deck"]}})
    svc.settings.update({"pages": ["ai", "deck"]})
    for _ in range(100):
        if len(link.configs) >= 2:
            break
        threading.Event().wait(0.02)
    assert link.configs == [{"pages": ["deck", "music", "hud"]}, {"pages": ["ai", "deck"]}]
    svc._on_connect(link, {"settings": {"pages": ["ai", "deck"]}})  # already right: nothing sent
    threading.Event().wait(0.1)
    assert len(link.configs) == 2


def test_status_is_complete_and_secret_free(svc):
    svc.secrets.set("claude_token", TOK + "SECRET")
    svc.secrets.set("panel_token", "PANELSECRET")
    st = svc.status()
    assert st["panel"]["state"] == "off" and st["platform"]["ok"] and st["startup"]["supported"]
    assert st["ai"]["usage"]["claude"]["state"] == "off" and st["panel"]["has_token"] is True
    text = json.dumps(st)
    assert "SECRET" not in text and svc.hook_secret not in text
    meta = svc.meta()
    assert meta["redirect_uri"] == "http://127.0.0.1:47555/spotify/callback" and "prev" in meta["icons"]
    assert meta["builtin_pages"]["windows"]["name"] == "WINDOWS"


def test_autostart_goes_through_the_platform(svc, fake_platform):
    svc.set_autostart(True)
    assert fake_platform.auto[0] is True and svc.autostart_enabled()
    svc.settings.update({"startup": True})
    svc.set_autostart(False)
    assert svc.settings.section("startup") is False and not svc.autostart_enabled()
    argv = autostart_argv()
    assert "--no-browser" in argv and ("-m" in argv or getattr(sys, "frozen", False))


def test_null_platform_keeps_the_app_usable():
    p = NullPlatform("linux", "no module")
    assert p.list_apps() == [] and p.media() is None and p.audio() is None and not p.autostart_enabled()
    with pytest.raises(NotImplementedError):
        p.set_autostart(True, [])


def test_missing_extensions_package_loads_quietly(caplog):
    with caplog.at_level(logging.DEBUG):
        assert load_extensions("no_such_extensions") == []
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_present_extensions_load_and_broken_ones_are_reported(tmp_path, monkeypatch, caplog):
    for name, code in (("broken_ext", "import pandadeck_no_such_dependency\n"),
                       ("good_ext", "class Ext:\n    registered = False\n\n    def register(self):\n"
                                    "        Ext.registered = True\n\n\nEXTENSIONS = [Ext]\n")):
        (tmp_path / name).mkdir()
        (tmp_path / name / "__init__.py").write_text(code)
    monkeypatch.setattr(pandadeck, "__path__", [*pandadeck.__path__, str(tmp_path)])
    importlib.invalidate_caches()
    try:
        with caplog.at_level(logging.WARNING):
            assert load_extensions("broken_ext") == []
        assert any("extensions failed to load" in r.getMessage() for r in caplog.records)
        exts = load_extensions("good_ext")
        assert len(exts) == 1 and type(exts[0]).registered
    finally:
        for name in ("broken_ext", "good_ext"):
            sys.modules.pop(f"pandadeck.{name}", None)
            if hasattr(pandadeck, name):
                delattr(pandadeck, name)


def test_start_stop_writes_runtime_file_and_answers_ping(fake_platform):
    s = Service(panel=False, platform=fake_platform, port=47390)
    s.start()
    try:
        rt = json.loads(paths.runtime_file().read_text())
        assert rt["port"] == s.port and rt["pid"] == os.getpid()
        inst = running_instance()
        assert inst and inst["port"] == s.port
        assert settings_url() == f"http://127.0.0.1:{s.port}/"
    finally:
        s.stop()
    assert not paths.runtime_file().exists() and running_instance() is None


def test_stale_runtime_file_is_ignored():
    paths.runtime_file().parent.mkdir(parents=True, exist_ok=True)
    paths.runtime_file().write_text(json.dumps({"port": 1, "pid": 1}))
    assert running_instance() is None


def test_cli_version(capsys):
    assert cli.main(["--version"]) == 0
    assert capsys.readouterr().out.startswith("PandaDeck ")


def test_tray_image():
    img = cli.tray_image()
    assert img.size == (64, 64) and img.getpixel((32, 32))[3] == 255


def test_paths_in_the_ui_hide_the_home_folder(svc):
    from pathlib import Path
    meta = svc.meta()
    st = svc.status()
    home = str(Path.home())
    for text in (meta["config_dir"], meta["log_dir"], json.dumps(st["ai"]["hooks"])):
        assert home not in text and home.replace("\\", "/") not in text
    assert paths.display(Path.home() / "Config" / "PandaDeck") == "~/Config/PandaDeck"
    assert paths.scrub(f"can't read {Path.home() / '.claude' / 'settings.json'}").startswith("can't read ~")
