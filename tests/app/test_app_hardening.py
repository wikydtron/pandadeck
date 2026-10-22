"""Regression tests for the security/robustness review findings."""
import json
import socket
import threading
import time

import pytest

from pandadeck import paths, util
from pandadeck.ai import hooks
from pandadeck.ai.bridge import Bridge
from pandadeck.config import Settings
from pandadeck.secrets import SecretStore
from pandadeck.service import Service
from pandadeck.sync import Syncer
from pandadeck.web import api
from pandadeck.web.server import make_server, redact


@pytest.fixture
def served(fake_platform):
    svc = Service(panel=False, platform=fake_platform)
    srv = make_server(svc, 0, api.ROUTES)
    svc.port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield svc, srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def raw_http(port: int, data: bytes) -> bytes:
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    s.sendall(data)
    out = b""
    try:
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            out += chunk
    except socket.timeout:
        pass
    s.close()
    return out


def test_refused_requests_cannot_smuggle_a_second_one(served):
    svc, port = served
    inner = json.dumps({"startup": False}).encode()
    smuggled = (f"POST /api/settings HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nX-PandaDeck: 1\r\n"
                f"Content-Type: application/json\r\nContent-Length: {len(inner)}\r\n\r\n").encode() + inner
    outer = (f"POST /api/settings HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nContent-Type: text/plain\r\n"
             f"Content-Length: {len(smuggled)}\r\n\r\n").encode() + smuggled
    reply = raw_http(port, outer)
    assert reply.count(b"HTTP/1.") == 1 and b" 403 " in reply.split(b"\r\n")[0]
    time.sleep(0.2)
    assert svc.settings.section("startup") is True  # the inner request never ran


def test_chunked_bodies_are_refused(served):
    _, port = served
    reply = raw_http(port, (f"POST /api/settings HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nX-PandaDeck: 1\r\n"
                            "Transfer-Encoding: chunked\r\n\r\n0\r\n\r\n").encode())
    assert b" 400 " in reply.split(b"\r\n")[0]


def test_logs_never_contain_the_hook_secret():
    assert redact("/pandadeck/abcdef123456/hook?x=1") == "/pandadeck/<secret>/hook"
    assert redact("/spotify/callback?code=SECRET") == "/spotify/callback"
    assert redact("/pandadeck/health") == "/pandadeck/health"


# ---- untrusted Wi-Fi links never carry approvals ---------------------------------------------------

class UntrustedLink:
    kind, where, trusted = "wifi", "10.0.0.66", False

    def __init__(self):
        self.sent = []

    def sync(self, snap):
        self.sent.append(snap)
        return {"ok": True, "boot": 1, "events": [{"seq": 1, "k": "perm", "id": "p1", "a": "allow"}]}


class Links:
    busy = ""

    def __init__(self, link):
        self.link = link

    def current(self):
        return self.link

    def failed(self, e):
        pass

    def succeeded(self):
        pass


def test_untrusted_wifi_gets_no_perms_and_its_approvals_are_ignored():
    b = Bridge("Desk")
    b._perms["p1"] = type("P", (), {"pid": "p1", "sid": "s", "proj": "x", "host": "h", "tool": "Bash",
                                    "detail": "rm", "created": time.time(), "src": "claude", "verdict": None})()
    decided = []
    b.decide = lambda pid, v: decided.append((pid, v)) or True

    class Act:
        def run(self, ev):
            if ev["k"] == "perm":
                b.decide(ev["id"], ev["a"])

    link = UntrustedLink()
    s = Syncer(Links(link), b, Act())
    assert s.once()
    assert link.sent[0]["perms"] == [] and decided == []
    assert not s.approvals_possible()


# ---- files -------------------------------------------------------------------------------------------

def test_corrupt_settings_are_kept_aside_not_lost(isolated_home):
    path = isolated_home / "settings.json"
    path.write_text('{"deck": {"apps": ["a", "b"]},}')  # trailing comma
    s = Settings(path)
    assert s.get()["deck"]["apps"] == []
    bad = list(isolated_home.glob("settings.json.bad-*"))
    assert len(bad) == 1 and '"apps": ["a", "b"]' in bad[0].read_text()


def test_corrupt_secrets_are_kept_aside(isolated_home):
    path = isolated_home / "secrets.json"
    path.write_text('{"spotify": {"refresh_token": "R"')  # truncated
    st = SecretStore(path)
    assert st.get("spotify") is None
    assert len(list(isolated_home.glob("secrets.json.bad-*"))) == 1


def test_hooks_refuse_unexpected_structures(tmp_path):
    path = tmp_path / "settings.json"
    for bad in ({"hooks": []}, {"hooks": {"Stop": {"not": "a list"}}}):
        path.write_text(json.dumps(bad))
        with pytest.raises(ValueError):
            hooks.install_claude(47321, "k", 60, path=path)
        assert json.loads(path.read_text()) == bad


def test_hooks_keep_unicode_and_end_with_newline(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"statusLine": "café →"}, ensure_ascii=False), encoding="utf-8")
    hooks.install_claude(47321, "k", 60, path=path)
    text = path.read_text(encoding="utf-8")
    assert "café →" in text and text.endswith("\n")


def test_claude_and_codex_config_dirs_follow_env(monkeypatch, tmp_path):
    monkeypatch.delenv("PANDADECK_USER_HOME", raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cc"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "cx"))
    assert hooks.claude_settings_path() == tmp_path / "cc" / "settings.json"
    assert hooks.codex_hooks_path() == tmp_path / "cx" / "hooks.json"
    monkeypatch.setenv("PANDADECK_USER_HOME", str(tmp_path / "u"))  # tests / dry runs win
    assert hooks.claude_settings_path() == tmp_path / "u" / ".claude" / "settings.json"


def test_lab_token_never_follows_the_url_to_another_server(fake_platform):
    svc = Service(panel=False, platform=fake_platform)
    svc.settings.update({"hud": {"lab_url": "http://box:47322"}})
    svc.secrets.set("lab_token", "LT")
    svc.settings.update({"hud": {"lab_url": "http://box:47322/"}})  # same server
    assert svc.secrets.get("lab_token") == "LT"
    svc.settings.update({"hud": {"lab_url": "http://elsewhere:47322"}})
    assert svc.secrets.get("lab_token") is None


def test_display_paths_respect_separators(monkeypatch, tmp_path):
    home = tmp_path / "alex"
    monkeypatch.setattr(paths, "_home", lambda: home)
    monkeypatch.setenv("PANDADECK_USER_HOME", str(home))
    assert paths.display(home / "x") == "~/x"
    assert not paths.display(str(home) + "ie/x").startswith("~")
    assert paths.scrub(f"open {home}ie/a and {home}/b") == f"open {home}ie/a and ~/b"


def test_local_hosts_skip_proxies():
    for host in ("127.0.0.1", "localhost", "10.0.0.5", "192.168.0.9", "pandadeck.local", "homeserver", "::1"):
        assert util.is_local_host(host), host
    for host in ("api.anthropic.com", "8.8.8.8", "github.com"):
        assert not util.is_local_host(host), host
