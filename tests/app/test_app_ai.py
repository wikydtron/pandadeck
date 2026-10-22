"""AI page plumbing: hooks install/remove, the bridge, usage parsers, Connect Claude."""
import json
import os
import stat
import time

import pytest

from pandadeck.ai import connect, hooks, usage
from pandadeck.ai.bridge import Bridge, decision_json

TOK = "sk-ant-oat01-"  # a fake Claude token prefix


# ---- hooks -------------------------------------------------------------------------------

def test_claude_hooks_merge_idempotent_and_remove_exactly_ours(tmp_path):
    path = tmp_path / ".claude" / "settings.json"
    path.parent.mkdir()
    other = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo hi"}]}]}, "model": "opus"}
    path.write_text(json.dumps(other))
    r = hooks.install_claude(47321, "s3cret", 600, path=path)
    data = json.loads(path.read_text())
    assert r["changed"] and r["backup"] and data["model"] == "opus"
    assert data["hooks"]["Stop"][0]["hooks"][0]["command"] == "echo hi"
    assert data["hooks"]["Stop"][1]["hooks"][0]["url"] == "http://127.0.0.1:47321/pandadeck/s3cret/hook"
    assert data["hooks"]["PermissionRequest"][0]["hooks"][0]["timeout"] == 615
    assert "PostToolUse" not in data["hooks"]
    assert not hooks.install_claude(47321, "s3cret", 600, path=path)["changed"]  # idempotent, no new backup
    hooks.install_claude(47322, "rotated", 600, path=path)  # port moved / secret rotated: replaced, not duplicated
    stop = json.loads(path.read_text())["hooks"]["Stop"]
    assert len(stop) == 2 and stop[1]["hooks"][0]["url"] == "http://127.0.0.1:47322/pandadeck/rotated/hook"
    hooks.install_claude(47322, "rotated", 600, uninstall=True, path=path)
    assert json.loads(path.read_text()) == other


def test_claude_hooks_create_missing_file_and_refuse_broken_json(tmp_path):
    path = tmp_path / ".claude" / "settings.json"
    hooks.install_claude(47321, "k", 60, path=path)
    assert "SessionStart" in json.loads(path.read_text())["hooks"]
    path.write_text("{broken")
    with pytest.raises(ValueError):
        hooks.install_claude(47321, "k", 60, path=path)
    assert path.read_text() == "{broken"  # the user's file is never clobbered


def test_backups_are_pruned(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{}")
    for i in range(8):
        hooks.install_claude(47321, f"s{i}", 60, path=path)
        # distinct timestamps for distinct backup names
        for b in tmp_path.glob("settings.json.bak-pandadeck-*"):
            if not b.name.endswith(f"-{i}"):
                b.rename(b.with_name(b.name + f"-{i}"))
    assert len(list(tmp_path.glob("settings.json.bak-pandadeck-*"))) <= hooks.KEEP_BACKUPS + 1


def test_codex_hooks_use_curl_for_every_os(tmp_path):
    path = tmp_path / "hooks.json"
    path.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "notify.exe"}]}]}}))
    hooks.install_codex(47321, "k", 600, path=path)
    data = json.loads(path.read_text())
    ours = data["hooks"]["PermissionRequest"][0]["hooks"][0]
    assert ours["command"].startswith("curl ") and ours["commandWindows"].startswith(hooks.WINDOWS_CURL)
    assert "/pandadeck/k/codex" in ours["command"] and '--data-binary "@-"' in ours["command"]
    assert ours["timeout"] == 615 and "-m 605" in ours["command"]
    assert data["hooks"]["SessionEnd"][0]["hooks"][0]["timeout"] <= 3
    assert data["hooks"]["Stop"][0]["hooks"][0]["command"] == "notify.exe"
    hooks.install_codex(47321, "k", 600, path=path)
    assert len(json.loads(path.read_text())["hooks"]["Stop"]) == 2
    hooks.install_codex(47321, "k", 600, uninstall=True, path=path)
    assert json.loads(path.read_text()) == {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "notify.exe"}]}]}}


def test_old_agent_hooks_count_as_ours(tmp_path):
    path = tmp_path / "settings.json"
    old = {"hooks": {"Stop": [{"hooks": [{"type": "http", "url": "http://127.0.0.1:47321/pandadeck/OLD/hook"}]}]}}
    path.write_text(json.dumps(old))
    assert hooks.installed_urls(path) == ["http://127.0.0.1:47321/pandadeck/OLD/hook"]
    hooks.install_claude(47321, "NEW", 60, path=path)
    assert all("/NEW/" in u for u in hooks.installed_urls(path))


def test_hook_status(user_home):
    st = hooks.status(47321, "abc", home=user_home)
    assert st["claude"]["installed"] is False and st["codex"]["installed"] is False
    hooks.install_claude(47321, "abc", 60, path=hooks.claude_settings_path(user_home))
    st = hooks.status(47321, "abc", home=user_home)
    assert st["claude"]["installed"] and st["claude"]["current"]
    assert not hooks.status(47399, "abc", home=user_home)["claude"]["current"]


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_hooks_keep_the_files_mode(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{}")
    os.chmod(path, 0o644)
    hooks.install_claude(47321, "k", 60, path=path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o644


# ---- bridge ---------------------------------------------------------------------------------

def test_handle_hook_events_and_codex_namespace():
    b = Bridge("Desk")
    assert b.handle_hook({"hook_event_name": "UserPromptSubmit", "session_id": "s1", "cwd": "/p/X", "prompt": "a"},
                         "claude", lambda: True, 1, lambda: True) == b""
    b.handle_hook({"hook_event_name": "UserPromptSubmit", "session_id": "s1", "cwd": "/p/Y", "prompt": "b"},
                  "codex", lambda: True, 1, lambda: True)
    sessions, _ = b.snapshot()
    assert {s["src"] for s in sessions} == {"claude", "codex"} and b.last_event_at["codex"] > 0
    assert b.handle_hook(["not", "a", "dict"], "claude", lambda: True, 1, lambda: True) == b""


def test_permission_flow_through_handle_hook():
    import threading
    b = Bridge("Desk")
    ev = {"hook_event_name": "PermissionRequest", "session_id": "s1", "cwd": "/p/X", "tool_name": "Bash",
          "tool_input": {"command": "ls -la"}}
    out = {}
    t = threading.Thread(target=lambda: out.update(r=b.handle_hook(dict(ev), "claude", lambda: True, 5, lambda: True)))
    t.start()
    for _ in range(100):
        _, perms = b.snapshot()
        if perms:
            break
        time.sleep(0.02)
    assert perms[0]["detail"] == "ls -la" and b.has_pending_perms()
    assert b.decide(perms[0]["id"], "allow")
    t.join(3)
    assert json.loads(out["r"])["hookSpecificOutput"]["decision"]["behavior"] == "allow"
    # panel down: the card is closed at once and the PC decides
    assert b.handle_hook(dict(ev), "claude", lambda: True, 5, lambda: False) == b""
    assert not b.has_pending_perms()


def test_interactive_tools_never_take_over():
    b = Bridge("Desk")
    r = b.handle_hook({"hook_event_name": "PermissionRequest", "session_id": "s1", "tool_name": "AskUserQuestion",
                       "tool_input": {}}, "claude", lambda: True, 5, lambda: True)
    assert r == b"" and b.snapshot()[0][0]["st"] == "wait"


def test_decision_json_shapes():
    assert json.loads(decision_json("deny"))["hookSpecificOutput"]["decision"]["behavior"] == "deny"


# ---- usage: Claude --------------------------------------------------------------------------------

NOW = 1_800_000_000.0


def test_parse_claude_headers_fractions_to_percent():
    h = {"anthropic-ratelimit-unified-5h-utilization": "0.153", "anthropic-ratelimit-unified-5h-reset": str(NOW + 3600),
         "anthropic-ratelimit-unified-7d-utilization": "0.68", "anthropic-ratelimit-unified-7d-reset": str(NOW + 86400)}
    assert usage.parse_claude_headers(h, NOW) == {"s5h": 15.3, "s5h_reset": 3600, "week": 68.0, "week_reset": 86400}
    partial = usage.parse_claude_headers({"anthropic-ratelimit-unified-7d-utilization": "1"}, NOW)
    assert partial == {"s5h": -1, "s5h_reset": -1, "week": 100.0, "week_reset": -1}
    with pytest.raises(ValueError):
        usage.parse_claude_headers({}, NOW)


def test_fetch_claude_sends_the_right_request():
    seen = {}

    def request(url, **kw):
        seen.update(url=url, **kw)
        return 429, {"anthropic-ratelimit-unified-5h-utilization": "1.0",
                     "anthropic-ratelimit-unified-5h-reset": str(NOW + 60)}, b'{"error":{"message":"rate"}}'

    out = usage.fetch_claude(TOK + "T", now=lambda: NOW, request=request)
    assert out["s5h"] == 100.0 and out["s5h_reset"] == 60   # a 429 still carries the numbers
    h = seen["headers"]
    assert seen["url"] == "https://api.anthropic.com/v1/messages" and seen["method"] == "POST"
    assert h["Authorization"] == "Bearer " + TOK + "T" and h["anthropic-version"] == "2023-06-01"
    assert h["anthropic-beta"] == "oauth-2025-04-20" and h["User-Agent"] == "claude-code/2.1.5"
    body = json.loads(seen["body"])
    assert body["model"] == "claude-haiku-4-5" and body["max_tokens"] == 1
    assert seen["follow_redirects"] is False


def test_fetch_claude_errors():
    with pytest.raises(PermissionError):
        usage.fetch_claude("t", request=lambda url, **kw: (401, {}, b""))
    with pytest.raises(ValueError, match="overloaded"):
        usage.fetch_claude("t", request=lambda url, **kw: (529, {}, b'{"error":{"message":"overloaded"}}'))


# ---- usage: Codex ------------------------------------------------------------------------------------

def test_parse_codex_windows():
    data = {"rate_limit": {
        "primary_window": {"used_percent": 12, "limit_window_seconds": 18000, "reset_after_seconds": 900},
        "secondary_window": {"used_percent": 2.5, "limit_window_seconds": 604800, "reset_at": NOW + 568552}}}
    assert usage.parse_codex_usage(data, NOW) == {"codex_week": 2.5, "codex_reset": 568552}
    single = {"rate_limit": {"primary_window": {"used_percent": 40, "limit_window_seconds": 18000,
                                                "reset_after_seconds": 100}}}
    assert usage.parse_codex_usage(single, NOW) == {"codex_week": 40.0, "codex_reset": 100}
    with pytest.raises(ValueError):
        usage.parse_codex_usage({"rate_limit": {}}, NOW)


def test_fetch_codex_reads_auth_and_refuses_redirects(user_home):
    with pytest.raises(FileNotFoundError):
        usage.fetch_codex()
    (user_home / ".codex").mkdir()
    (user_home / ".codex" / "auth.json").write_text(json.dumps({"tokens": {"access_token": "AT", "account_id": "AC"}}))
    seen = {}

    def get(url, **kw):
        seen.update(url=url, **kw)
        return 200, {"rate_limit": {"secondary_window": {"used_percent": 7, "limit_window_seconds": 604800,
                                                         "reset_after_seconds": 10}}}

    assert usage.fetch_codex(get=get) == {"codex_week": 7.0, "codex_reset": 10}
    assert seen["url"] == "https://chatgpt.com/backend-api/wham/usage" and seen["follow_redirects"] is False
    assert seen["headers"]["Authorization"] == "Bearer AT" and seen["headers"]["ChatGPT-Account-Id"] == "AC"
    with pytest.raises(PermissionError):
        usage.fetch_codex(get=lambda url, **kw: (401, None))


def test_no_redirect_handler_refuses():
    import urllib.error

    from pandadeck.util import NoRedirect
    with pytest.raises(urllib.error.HTTPError):
        NoRedirect().redirect_request(type("R", (), {"full_url": "https://a"})(), None, 302, "Found", {}, "https://b")


def test_daemon_formats():
    assert usage.parse_claude_daemon({"ok": True, "session_pct": 15, "session_reset_s": 6696, "weekly_pct": 68,
                                      "weekly_reset_s": 431496}) == {"s5h": 15, "s5h_reset": 6696, "week": 68,
                                                                     "week_reset": 431496}
    assert usage.parse_codex_daemon({"seven_day": {"used": 2, "reset_s": 5}}) == {"codex_week": 2, "codex_reset": 5}
    with pytest.raises(ValueError):
        usage.parse_claude_daemon({"ok": False})


def test_collector_modes(settings, fake_secrets):
    col = usage.UsageCollector(settings, fake_secrets, claude=lambda t: {"s5h": 1, "s5h_reset": 2, "week": 3,
                                                                          "week_reset": 4},
                               codex=lambda: {"codex_week": 5, "codex_reset": 6}, clock=lambda: NOW)
    col.step()
    assert col.snapshot() is None and col.status["claude"]["state"] == "off"
    settings.update({"ai": {"claude_usage": "token", "codex_usage": "auth"}})
    col.step()
    assert col.status["claude"]["state"] == "error" and "Connect Claude" in col.status["claude"]["error"]
    assert col.snapshot() == {**usage.EMPTY, "codex_week": 5, "codex_reset": 6}
    fake_secrets.set("claude_token", TOK + "x")
    col.step()
    snap = col.snapshot()
    assert snap["s5h"] == 1 and snap["week"] == 3 and snap["codex_week"] == 5 and snap["ok"] is True
    assert set(snap) == set(usage.EMPTY)  # exactly the keys model.cpp parse_usage reads
    assert col.next_interval() == 60


def test_collector_keeps_last_good_values_through_a_blip(settings, fake_secrets):
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] > 1:
            raise OSError("network down")
        return {"codex_week": 9, "codex_reset": 1}

    settings.update({"ai": {"codex_usage": "auth"}})
    col = usage.UsageCollector(settings, fake_secrets, codex=flaky, clock=lambda: NOW)
    col.step()
    col.step()
    assert col.status["codex"]["state"] == "error" and col.snapshot()["codex_week"] == 9


def test_url_mode(settings, fake_secrets):
    settings.update({"ai": {"claude_usage": "url", "claude_usage_url": "http://usage.example:9000/usage"}})
    col = usage.UsageCollector(settings, fake_secrets, get=lambda url, **kw: (
        200, {"ok": True, "session_pct": 50, "session_reset_s": 1, "weekly_pct": 60, "weekly_reset_s": 2}))
    col.step()
    assert col.snapshot()["s5h"] == 50 and col.snapshot()["week"] == 60


# ---- Connect Claude ------------------------------------------------------------------------------

def test_setup_token_opens_a_terminal_per_os():
    calls = []

    def popen(argv, **kw):
        calls.append(argv)

    def which(name):
        return {"claude": "/usr/bin/claude", "gnome-terminal": "/usr/bin/gnome-terminal"}.get(name)

    assert connect.start_setup_token(which=which, popen=popen, platform="linux") == {"started": True}
    assert calls[-1][0] == "gnome-terminal" and "setup-token" in calls[-1][-1]
    assert connect.start_setup_token(which=which, popen=popen, platform="win32")["started"]
    assert calls[-1][:2] == ["cmd.exe", "/k"] and calls[-1][-1] == "setup-token"
    assert connect.start_setup_token(which=which, popen=popen, platform="darwin")["started"]
    assert calls[-1][0] == "osascript" and "setup-token" in calls[-1][2]
    r = connect.start_setup_token(which=lambda n: None, popen=popen, platform="linux")
    assert not r["started"] and "isn't installed" in r["reason"]
