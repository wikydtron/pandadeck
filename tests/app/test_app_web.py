"""The local web server: DNS-rebinding/CSRF guards, hook endpoints, secrets
never read back, settings API, Spotify callback, port fallback."""
import http.client
import json
import socket
import threading

import pytest

from pandadeck.service import Service
from pandadeck.web import api
from pandadeck.web.server import bind_first_free, check_host, check_origin, make_server

TOK = "sk-ant-oat01-"  # a fake Claude token prefix


@pytest.fixture
def svc(fake_platform):
    s = Service(panel=False, platform=fake_platform)
    s.port = 0
    return s


@pytest.fixture
def server(svc):
    srv = make_server(svc, 0, api.ROUTES)
    svc.port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()
    srv.server_close()


def call(server, method, path, body=None, headers=None, host=None, raw=None):
    port = server.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    hdrs = {"Host": host or f"127.0.0.1:{port}"}
    data = None
    if raw is not None:
        data = raw
        hdrs["Content-Type"] = "application/octet-stream"
    elif body is not None:
        data = json.dumps(body).encode()
        hdrs["Content-Type"] = "application/json"
    hdrs.update(headers or {})
    conn.request(method, path, body=data, headers=hdrs)
    r = conn.getresponse()
    payload = r.read()
    conn.close()
    try:
        parsed = json.loads(payload) if payload else None
    except ValueError:
        parsed = payload
    return r.status, parsed, dict(r.getheaders())


GOOD = {"X-PandaDeck": "1"}


def test_host_and_origin_helpers():
    assert check_host("127.0.0.1:47321", 47321) and check_host("LOCALHOST:47321", 47321)
    assert not check_host("evil.com", 47321) and not check_host("127.0.0.1:1", 47321) and not check_host(None, 1)
    assert check_origin(None, 5) and check_origin("http://localhost:5", 5)
    assert not check_origin("http://evil.com", 5) and not check_origin("https://127.0.0.1:5", 5)


def test_rebinding_host_is_refused(server):
    status, body, _ = call(server, "GET", "/api/status", headers=GOOD, host="attacker.example:47321")
    assert status == 421
    status, _, _ = call(server, "GET", "/", host="attacker.example")
    assert status == 421


def test_api_needs_the_header(server):
    assert call(server, "GET", "/api/status")[0] == 403
    assert call(server, "POST", "/api/settings", {"startup": False})[0] == 403
    status, body, _ = call(server, "GET", "/api/status", headers=GOOD)
    assert status == 200 and body["panel"]["state"] == "off"


def test_ping_is_open_for_the_second_instance_check(server):
    status, body, _ = call(server, "GET", "/api/ping")
    assert status == 200 and body["app"] == "pandadeck-app"


def test_cross_origin_posts_are_refused(server):
    port = server.server_address[1]
    assert call(server, "POST", "/api/settings", {"startup": False}, {**GOOD, "Origin": "http://evil.com"})[0] == 403
    assert call(server, "POST", "/api/settings", {"startup": False},
                {**GOOD, "Origin": f"http://127.0.0.1:{port}", "Sec-Fetch-Site": "cross-site"})[0] == 403
    status, body, _ = call(server, "POST", "/api/settings", {"startup": False},
                           {**GOOD, "Origin": f"http://127.0.0.1:{port}", "Sec-Fetch-Site": "same-origin"})
    assert status == 200 and body["settings"]["startup"] is False


def test_json_only_and_size_limits(server):
    port = server.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("POST", "/api/settings", body=b"startup=false",
                 headers={"Host": f"127.0.0.1:{port}", "X-PandaDeck": "1", "Content-Type": "text/plain"})
    assert conn.getresponse().status == 415
    conn.close()
    big = {"x": "a" * 300_000}
    assert call(server, "POST", "/api/settings", big, GOOD)[0] == 413


def test_bad_settings_are_rejected_with_a_message(server):
    status, body, _ = call(server, "POST", "/api/settings", {"music": {"source": "radio"}}, GOOD)
    assert status == 400 and "source" in body["error"]


def test_secrets_are_write_only(server, svc):
    status, body, _ = call(server, "POST", "/api/secret", {"name": "lab_token", "value": "SUPERSECRETLABTOKEN"}, GOOD)
    assert status == 200 and body["secrets"]["lab_token"] is True
    svc.secrets.set("claude_token", TOK + "NEVERSHOWN")
    svc.secrets.set("spotify", {"refresh_token": "SPOTIFYREFRESH", "access_token": "SPOTIFYACCESS"})
    for path in ("/api/settings", "/api/status", "/api/apps", "/api/deck"):
        status, body, _ = call(server, "GET", path, headers=GOOD)
        text = json.dumps(body)
        assert status == 200
        for secret in ("SUPERSECRETLABTOKEN", "NEVERSHOWN", "SPOTIFYREFRESH", "SPOTIFYACCESS", svc.hook_secret):
            assert secret not in text, (path, secret)
    assert call(server, "POST", "/api/secret", {"name": "hook_secret", "value": "x"}, GOOD)[0] == 400


def test_panel_token_reveal_needs_post_and_header(server, svc):
    assert call(server, "POST", "/api/panel/token", {}, GOOD)[0] == 404  # none yet
    svc.secrets.set("panel_token", "PANELTOK")
    assert call(server, "GET", "/api/panel/token", headers=GOOD)[0] in (404, 405)
    assert call(server, "POST", "/api/panel/token", {})[0] == 403
    status, body, _ = call(server, "POST", "/api/panel/token", {}, GOOD)
    assert status == 200 and body["token"] == "PANELTOK"


def test_hook_endpoint_requires_secret_and_no_origin(server, svc):
    path = f"/pandadeck/{svc.hook_secret}/hook"
    ev = {"hook_event_name": "UserPromptSubmit", "session_id": "s1", "cwd": "/x/Proj", "prompt": "hello"}
    assert call(server, "POST", "/pandadeck/wrong/hook", ev)[0] == 404
    assert call(server, "POST", path, ev, {"Origin": "http://evil.com"})[0] == 404
    status, _, _ = call(server, "POST", path, ev)
    assert status == 200
    sessions, _ = svc.bridge.snapshot()
    assert sessions[0]["proj"] == "Proj" and sessions[0]["act"] == "> hello"
    status, _, _ = call(server, "POST", f"/pandadeck/{svc.hook_secret}/codex", {**ev, "session_id": "c9"})
    assert status == 200 and any(s["src"] == "codex" for s in svc.bridge.snapshot()[0])
    assert call(server, "POST", path, raw=b"not json")[0] == 200  # never break an agent
    assert call(server, "GET", "/pandadeck/health")[1] == {"ok": True}


def test_permission_request_without_panel_returns_no_decision(server, svc):
    path = f"/pandadeck/{svc.hook_secret}/hook"
    ev = {"hook_event_name": "PermissionRequest", "session_id": "s1", "cwd": "/x/P", "tool_name": "Bash",
          "tool_input": {"command": "ls"}}
    status, body, _ = call(server, "POST", path, ev)
    assert status == 200 and body is None  # empty body: the PC's own prompt decides


def test_static_page_has_csp(server):
    status, body, headers = call(server, "GET", "/")
    assert status == 200 and b"PandaDeck" in body
    csp = headers.get("Content-Security-Policy", "")
    assert "default-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert headers.get("X-Frame-Options") == "DENY"
    assert call(server, "GET", "/app.js")[0] == 200 and call(server, "GET", "/../secrets.json")[0] == 404
    for js in ("/deck.js", "/lab.js", "/sounds.js", "/works.js"):
        assert call(server, "GET", js)[0] == 200, js


def test_apps_api_and_icons(server, svc):
    svc.board._load_catalog()
    status, body, _ = call(server, "GET", "/api/apps", headers=GOOD)
    assert status == 200 and body["ready"] and body["suggested"][0] == "id.code"
    status, png, headers = call(server, "GET", "/api/apps/icon?id=id.code")
    assert status == 200 and headers["Content-Type"] == "image/png" and png[:4] == b"\x89PNG"
    assert call(server, "GET", "/api/apps/icon?id=unknown")[0] == 404
    assert call(server, "GET", "/api/apps/icon?id=id.calc")[0] == 404  # no icon


def test_panel_calls_without_a_panel(server):
    assert call(server, "POST", "/api/panel/config", {"brightness": 50}, GOOD)[0] == 409
    assert call(server, "POST", "/api/panel/wifi", {"ssid": "x", "pass": "12345678"}, GOOD)[0] == 409
    assert call(server, "POST", "/api/firmware/update", {"source": "latest"}, GOOD)[0] == 409


def test_firmware_file_is_validated_and_staged(server, svc):
    status, body, _ = call(server, "POST", "/api/firmware/file", headers=GOOD, raw=b"\x00" * 600)
    assert status == 400
    img = bytearray(600)
    img[0] = 0xE9
    status, body, _ = call(server, "POST", "/api/firmware/file", headers=GOOD, raw=bytes(img))
    assert status == 200 and body["staged"] in svc.staged and body["info"]["size"] == 600


def test_install_check_rejects_bad_hosts(server):
    assert call(server, "POST", "/api/install/check", {"host": "x;rm -rf /"}, GOOD)[0] == 400


def test_spotify_login_and_callback_state(server, svc):
    assert call(server, "POST", "/api/spotify/login", {}, GOOD)[0] == 400  # no client id yet
    svc.settings.update({"music": {"spotify_client_id": "0123456789abcdef0123456789abcdef"}})
    status, body, _ = call(server, "POST", "/api/spotify/login", {}, GOOD)
    assert status == 200 and body["url"].startswith("https://accounts.spotify.com/authorize?")
    assert "code_challenge_method=S256" in body["url"] and "redirect_uri=http%3A%2F%2F127.0.0.1" in body["url"]
    status, _, headers = call(server, "GET", "/spotify/callback?code=abc&state=forged")
    assert status == 303 and headers["Location"] == "/?spotify=error#music"
    status, _, headers = call(server, "GET", "/spotify/callback?error=access_denied&state=x")
    assert headers["Location"] == "/?spotify=denied#music"


def test_autostart_and_claude_token(server, svc, fake_platform):
    status, body, _ = call(server, "POST", "/api/autostart", {"enabled": True}, GOOD)
    assert status == 200 and body["enabled"] and fake_platform.auto[0] is True
    assert "--no-browser" in fake_platform.auto[1]
    assert call(server, "POST", "/api/claude/token", {"token": "not-a-claude-token"}, GOOD)[0] == 400
    assert call(server, "POST", "/api/claude/token", {"token": TOK + "xyz"}, GOOD)[0] == 200
    assert svc.secrets.get("claude_token") == TOK + "xyz" and svc.settings.section("ai")["claude_usage"] == "token"
    call(server, "POST", "/api/claude/token", {"token": ""}, GOOD)
    assert not svc.secrets.has("claude_token") and svc.settings.section("ai")["claude_usage"] == "off"


def test_codex_connect_reads_auth_file(server, svc, user_home):
    assert call(server, "POST", "/api/codex/connect", {}, GOOD)[1] == {"found": False}
    (user_home / ".codex").mkdir()
    (user_home / ".codex" / "auth.json").write_text(json.dumps({"tokens": {"access_token": "a", "account_id": "b"}}))
    assert call(server, "POST", "/api/codex/connect", {}, GOOD)[1] == {"found": True}
    assert svc.settings.section("ai")["codex_usage"] == "auth"


def test_unknown_routes(server):
    assert call(server, "GET", "/api/nope", headers=GOOD)[0] == 404
    assert call(server, "DELETE", "/api/settings", headers=GOOD)[0] == 405


def test_bind_skips_a_busy_port(svc):
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    busy = blocker.getsockname()[1]
    try:
        srv = bind_first_free(svc, busy, 5, api.ROUTES)
        assert srv.server_address[1] != busy
        srv.server_close()
    finally:
        blocker.close()


# ---- panel endpoints with a connected (fake) panel --------------------------------------------

class FakePanelLink:
    def __init__(self, kind="usb", unsupported=False):
        self.kind, self.where, self.unsupported = kind, "COM5", unsupported
        self.configs = []

    def config(self, d):
        from pandadeck.link.base import Unsupported
        if self.unsupported:
            raise Unsupported("old firmware")
        self.configs.append(d)
        return {"ok": True, "settings": {"brightness": d.get("brightness", 50)}, "reboot": False}

    def close(self):
        pass


@pytest.fixture
def panel_link(svc):
    link = FakePanelLink()
    svc.links.enabled = True  # the thread never starts in tests: just pretend it found this panel
    with svc.links._lock:
        svc.links.link, svc.links.info = link, {"fw": "1.0.0", "settings": {}}
    return link


def test_panel_config_validates_and_forwards(server, svc, panel_link):
    assert call(server, "POST", "/api/panel/config", {"brightness": 200}, GOOD)[0] == 400
    assert call(server, "POST", "/api/panel/config", {"flip": "yes"}, GOOD)[0] == 400
    assert call(server, "POST", "/api/panel/config", {"name": "x" * 40}, GOOD)[0] == 400
    assert call(server, "POST", "/api/panel/config", {}, GOOD)[0] == 400
    status, body, _ = call(server, "POST", "/api/panel/config", {"brightness": 60, "flip": True, "tflip": False,
                                                                 "name": "Desk"}, GOOD)
    assert status == 200 and panel_link.configs[-1] == {"brightness": 60, "flip": True, "tflip": False, "name": "Desk"}
    assert svc.links.status()["info"]["settings"] == {"brightness": 60}


def test_wifi_reset_bootloader(server, svc, panel_link):
    assert call(server, "POST", "/api/panel/wifi", {"ssid": "Home", "pass": "short"}, GOOD)[0] == 400
    assert call(server, "POST", "/api/panel/wifi", {"ssid": "Home", "pass": "longenough"}, GOOD)[0] == 200
    assert panel_link.configs[-1] == {"wifi": {"ssid": "Home", "pass": "longenough"}}
    assert call(server, "POST", "/api/panel/wifi", {"ssid": "", "pass": ""}, GOOD)[0] == 200  # forget
    assert call(server, "POST", "/api/panel/reset", {}, GOOD)[0] == 200 and panel_link.configs[-1] == {"reset": True}
    assert call(server, "POST", "/api/panel/bootloader", {}, GOOD)[0] == 200
    assert panel_link.configs[-1] == {"bootloader": True} and svc.links.current() is None  # dropped: it reboots


def test_wifi_details_only_over_usb(server, svc):
    link = FakePanelLink(kind="wifi")
    with svc.links._lock:
        svc.links.link = link
    status, body, _ = call(server, "POST", "/api/panel/wifi", {"ssid": "Home", "pass": "longenough"}, GOOD)
    assert status == 409 and "USB" in body["error"] and link.configs == []


def test_old_firmware_is_explained(server, svc):
    with svc.links._lock:
        svc.links.link = FakePanelLink(unsupported=True)
    status, body, _ = call(server, "POST", "/api/panel/reset", {}, GOOD)
    assert status == 501 and "Update" in body["error"]


def test_firmware_update_from_a_staged_file(server, svc, panel_link, monkeypatch):
    from pandadeck import firmware
    flashed = []
    monkeypatch.setattr(firmware, "update_panel", lambda job, links, sec, st, data: flashed.append(data))
    import pandadeck.web.api as apimod
    monkeypatch.setattr(apimod, "update_panel", lambda job, links, sec, st, data: flashed.append(data))
    img = bytes([0xE9]) + bytes(599)
    staged = call(server, "POST", "/api/firmware/file", headers=GOOD, raw=img)[1]["staged"]
    assert call(server, "POST", "/api/firmware/update", {"staged": staged}, GOOD)[0] == 200
    for _ in range(100):
        if flashed:
            break
        threading.Event().wait(0.02)
    assert flashed == [img]
    status, body, _ = call(server, "GET", "/api/firmware/job", headers=GOOD)
    assert status == 200 and body["target"] == "panel"


def test_install_start_runs_the_job(server, svc, monkeypatch):
    import pandadeck.web.api as apimod
    seen = []
    monkeypatch.setattr(apimod, "stock_updater_present", lambda host: True)
    monkeypatch.setattr(apimod, "install_on_stock", lambda job, host, data, token="": seen.append((host, data)))
    img = bytes([0xE9]) + bytes(599)
    staged = call(server, "POST", "/api/firmware/file", headers=GOOD, raw=img)[1]["staged"]
    assert call(server, "POST", "/api/install/check", {"host": "10.0.0.57"}, GOOD)[1] == {"host": "10.0.0.57",
                                                                                           "updater": True}
    assert call(server, "POST", "/api/install/start", {"host": "10.0.0.57", "staged": staged}, GOOD)[0] == 200
    for _ in range(100):
        if seen:
            break
        threading.Event().wait(0.02)
    assert seen == [("10.0.0.57", img)]


def test_release_check_is_cached(server, svc, monkeypatch):
    import pandadeck.web.api as apimod
    calls = []
    monkeypatch.setattr(apimod, "latest_release", lambda: calls.append(1) or {"version": "9.9.9", "url": "https://x"})
    svc.links.enabled = True
    with svc.links._lock:
        svc.links.link, svc.links.info = FakePanelLink(), {"fw": "1.0.0"}
    body = call(server, "GET", "/api/firmware/latest", headers=GOOD)[1]
    assert body["newer"] and body["current"] == "1.0.0"
    call(server, "GET", "/api/firmware/latest", headers=GOOD)
    assert len(calls) == 1
    call(server, "GET", "/api/firmware/latest?force=1", headers=GOOD)
    assert len(calls) == 2


def test_misc_endpoints(server, svc, monkeypatch):
    monkeypatch.setattr(svc, "open_folder", lambda what: None)
    assert call(server, "POST", "/api/open", {"what": "config"}, GOOD)[0] == 200
    assert call(server, "POST", "/api/open", {"what": "/etc"}, GOOD)[0] == 400
    monkeypatch.setattr(svc, "wifi_suggestions", lambda: ["Home"])
    assert call(server, "GET", "/api/wifi/ssids", headers=GOOD)[1] == {"ssids": ["Home"]}
    assert call(server, "POST", "/api/apps/refresh", {}, GOOD)[0] == 200
    assert call(server, "GET", "/api/deck", headers=GOOD)[0] == 200
    assert call(server, "GET", "/api/music/cover")[0] == 404
    assert call(server, "GET", "/api/extensions", headers=GOOD)[0] == 200
    assert call(server, "POST", "/api/spotify/logout", {}, GOOD)[0] == 200
    assert call(server, "POST", "/api/autostart", {"enabled": "yes"}, GOOD)[0] == 400


def test_picture_upload_and_target_icons(server, svc, monkeypatch):
    import io

    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGBA", (40, 40), (255, 0, 0, 255)).save(buf, "PNG")
    assert call(server, "POST", "/api/icons", headers={}, raw=buf.getvalue())[0] == 403  # needs the header
    status, body, _ = call(server, "POST", "/api/icons", headers=GOOD, raw=buf.getvalue())
    assert status == 200 and len(body["id"]) == 40
    status, png, headers = call(server, "GET", "/api/icons/get?id=" + body["id"])
    assert status == 200 and headers["Content-Type"] == "image/png" and png[:4] == b"\x89PNG"
    assert call(server, "GET", "/api/icons/get?id=..%2F..%2Fsecrets.json")[0] == 404
    status, body, _ = call(server, "POST", "/api/icons", headers=GOOD, raw=b"<svg onload=alert(1)>")
    assert status == 400 and "picture" in body["error"].lower()
    assert call(server, "POST", "/api/icons", headers=GOOD, raw=b"\0" * (2 * 1024 * 1024 + 10))[0] == 400
    monkeypatch.setattr(svc.board, "target_png", lambda tid: b"\x89PNGfake" if tid == "t1" else None)
    assert call(server, "GET", "/api/targets/icon?id=t1")[1] == b"\x89PNGfake"
    assert call(server, "GET", "/api/targets/icon?id=t9")[0] == 404


def test_many_connections_at_once_are_not_refused(server):
    # The settings page opens a dozen connections at once (scripts, styles, icons).
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(24) as pool:
        results = list(pool.map(lambda _: call(server, "GET", "/app.js")[0], range(48)))
    assert results == [200] * 48
