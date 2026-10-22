"""HUD: this PC, the homelab collector and the pandadeck-lab server; panel actions."""
import http.client
import json
import sys
import threading
from collections import namedtuple
from pathlib import Path

import pytest

from pandadeck.actions import Actions
from pandadeck.hud import lab as labmod
from pandadeck.hud import pc as pcmod

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "app" / "lab_server"))
import pandadeck_lab as server  # noqa: E402

T = namedtuple("T", "label current")


def test_parse_nvidia_line():
    assert pcmod.parse_nvidia_line("4, 57, 29.80, 3200, 8192") == {"gpu": 4.0, "gpu_t": 57.0, "gpu_w": 29.8,
                                                                    "vram": 39.1}
    assert pcmod.parse_nvidia_line("4, 57, [N/A], 1, 0")["gpu_w"] == -1
    assert pcmod.parse_nvidia_line("garbage") is None and pcmod.parse_nvidia_line("a, b, c, d, e") is None


def test_cpu_temp_prefers_the_package_sensor():
    sensors = {"acpitz": [T("", 40.0)], "coretemp": [T("Core 0", 55.0), T("Package id 0", 61.0)]}
    assert pcmod.cpu_temp(sensors) == 61.0
    assert pcmod.cpu_temp({"k10temp": [T("Tctl", 70.5), T("Tccd1", 66)]}) == 70.5
    assert pcmod.cpu_temp({}) == -1


def test_pc_collector_snapshot_block():
    class Audio:
        def mic_muted(self):
            return True

        def volume(self):
            return 33

    c = pcmod.PcCollector(audio=Audio(), name="Desk")
    c.step()
    c.step()
    d = c.snapshot()
    assert d["name"] == "Desk" and 0 <= d["cpu"] <= 100 and 0 <= d["ram"] <= 100
    assert d["mic_muted"] is True and d["vol"] == 33
    assert set(d) >= {"cpu", "ram", "disk", "down", "up", "cpu_t", "gpu", "gpu_t", "gpu_w", "vram"}
    assert pcmod.host_name()


def test_clean_lab_matches_the_firmware_block():
    raw = {"cpu": "12.34", "ram": 50, "temp": None, "gpu": -1, "disk": 85, "run": 10.0, "total": 12, "healthy": 4,
           "backup_h": 7.7, "name": "serveré",
           "bad": [{"id": "d" * 64, "name": "flaky" * 10, "state": "running", "health": "unhealthy", "cpu": 5}] * 9,
           "top": [{"id": "e" * 12, "name": "frigate", "cpu": "35.1", "mem": 4.2}]}
    out = labmod.clean_lab(raw)
    assert out["ok"] and out["name"] == "servere" and out["cpu"] == 12.3 and out["temp"] == -1 and out["run"] == 10
    assert len(out["bad"]) == 6 and len(out["bad"][0]["id"]) == 12 and len(out["bad"][0]["name"]) <= 31
    assert out["top"][0] == {"id": "eeeeeeeeeeee", "name": "frigate", "state": "", "health": "", "cpu": 35.1, "mem": 4.2}
    assert labmod.clean_lab(raw, "Override")["name"] == "Override"
    with pytest.raises(ValueError):
        labmod.clean_lab(None)


def test_lab_collector_states(settings, fake_secrets):
    calls = []

    def get(url, **kw):
        calls.append((url, kw))
        return replies.pop(0)

    replies = [(200, {"cpu": 1, "name": "box"}), (401, None)]
    col = labmod.LabCollector(settings, fake_secrets, get=get)
    col.step()
    assert col.snapshot() is None and col.status["state"] == "off" and calls == []
    settings.update({"hud": {"lab_url": "http://box:47322/"}})
    fake_secrets.set("lab_token", "LT")
    col.step()
    assert col.snapshot()["name"] == "box" and calls[0][0] == "http://box:47322/api/lab"
    assert calls[0][1]["headers"] == {"Authorization": "Bearer LT"} and calls[0][1]["follow_redirects"] is False
    col.step()
    assert col.status["state"] == "error" and "token" in col.status["error"] and col.snapshot() == {"ok": False}


def test_lab_restart_validates_ids(settings, fake_secrets):
    calls = []
    col = labmod.LabCollector(settings, fake_secrets, get=lambda url, **kw: calls.append((url, kw)) or (200, {}))
    assert not col.restart("a" * 12)  # no URL yet
    settings.update({"hud": {"lab_url": "http://box:47322"}})
    assert not col.restart("../etc") and not col.restart("ABC")
    assert col.restart("abcdef012345") and calls[-1][0] == "http://box:47322/api/restart/abcdef012345"
    assert calls[-1][1]["method"] == "POST"


# ---- lab server --------------------------------------------------------------------------------

def test_parse_status_and_summarize():
    assert server.parse_status("Up 3 hours (healthy)") == ("running", "healthy", None)
    assert server.parse_status("Exited (137) 2 hours ago") == ("exited", "", 137)
    assert server.parse_status("Restarting (1) 5 seconds ago")[0] == "restarting"
    ps = [{"ID": "a" * 64, "Names": "web", "State": "running", "Status": "Up 1 hour (healthy)"},
          {"ID": "b" * 64, "Names": "db", "State": "exited", "Status": "Exited (1) 1 hour ago"},
          {"ID": "c" * 64, "Names": "parked", "State": "exited", "Status": "Exited (0) 3 days ago"},
          {"ID": "d" * 64, "Names": "flaky", "State": "running", "Status": "Up 2 min (unhealthy)"},
          {"ID": "e" * 64, "Names": "busy", "State": "running", "Status": "Up 1 day"}]
    stats = [{"ID": "a" * 12, "CPUPerc": "3.00%", "MemPerc": "1.5%"}, {"ID": "e" * 12, "CPUPerc": "80%", "MemPerc": "2%"},
             {"ID": "d" * 12, "CPUPerc": "50%", "MemPerc": "1%"}]
    out = server.summarize(ps, stats)
    assert [b["name"] for b in out["bad"]] == ["flaky", "db"] and out["bad"][0]["id"] == "d" * 12
    assert [t["name"] for t in out["top"]] == ["busy", "web"]
    assert out["run"] == 3 and out["total"] == 5 and out["healthy"] == 1


def test_token_check():
    assert server.token_ok("Bearer abcdefghijklmnop", "abcdefghijklmnop")
    assert not server.token_ok("Bearer wrong", "abcdefghijklmnop") and not server.token_ok("", "x")
    assert not server.token_ok("Basic abcdefghijklmnop", "abcdefghijklmnop") and not server.token_ok("Bearer x", "")


class FakeLab:
    def __init__(self):
        self.restarted = []

    def snapshot(self):
        return {"ok": True, "name": "box", "cpu": 1}

    def restart(self, cid):
        self.restarted.append(cid)
        return True, ""


def test_lab_server_endpoints():
    lab = FakeLab()
    srv = server.make_server(lab, "t" * 20, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]

    def req(method, path, token=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request(method, path, body=b"{}" if method == "POST" else None,
                  headers={"Authorization": f"Bearer {token}"} if token else {})
        r = c.getresponse()
        out = r.status, json.loads(r.read() or b"null")
        c.close()
        return out

    try:
        assert req("GET", "/health")[0] == 200
        assert req("GET", "/api/lab")[0] == 401 and req("GET", "/api/lab", "nope")[0] == 401
        assert req("GET", "/api/lab", "t" * 20) == (200, {"ok": True, "name": "box", "cpu": 1})
        assert req("POST", "/api/restart/abcdef012345")[0] == 401 and lab.restarted == []
        assert req("POST", "/api/restart/abcdef012345", "t" * 20)[0] == 200 and lab.restarted == ["abcdef012345"]
        assert req("POST", "/api/restart/;reboot", "t" * 20)[0] == 404
    finally:
        srv.shutdown()
        srv.server_close()


def test_lab_server_refuses_short_tokens(monkeypatch):
    monkeypatch.setenv("PANDADECK_LAB_TOKEN", "short")
    assert server.main(["--port", "0"]) == 2


# ---- actions ------------------------------------------------------------------------------------

class Rec:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        return lambda *a: self.calls.append((name, *a)) or True


def test_actions_dispatch():
    bridge, audio, media, board, lab = Rec(), Rec(), Rec(), Rec(), Rec()
    a = Actions(bridge=bridge, audio=audio, media=media, board=board, lab=lab)
    extra = []
    a.handlers["demo"] = extra.append
    for ev in [{"k": "perm", "id": "p1", "a": "deny"}, {"k": "cmd", "a": "mic"}, {"k": "cmd", "a": "media:seek:42"},
               {"k": "cmd", "a": "launch:app:a3"}, {"k": "cmd", "a": "launch:terminal"}, {"k": "cmd", "a": "noop"},
               {"k": "cmd", "a": "demo:open"}, {"k": "cmd", "a": "unknown:x"}, {"k": "other"}]:
        a.run(ev)
    a.run({"k": "cmd", "a": "restart:abcdef012345"})
    assert bridge.calls == [("decide", "p1", "deny")] and audio.calls == [("toggle_mic",)]
    assert media.calls == [("command", "seek:42")]
    assert board.calls == [("launch", "a3"), ("launch_named", "terminal")]
    assert extra == ["open"]
    for _ in range(50):
        if lab.calls:
            break
        threading.Event().wait(0.02)
    assert lab.calls == [("restart", "abcdef012345")]


# ---- names and models (HUD contract: name, cpu_model, gpu_model) --------------------------------

from pandadeck.hud import hw  # noqa: E402


def test_short_model_names():
    cases = {"Intel(R) Core(TM) i5-10400 CPU @ 2.90GHz": "i5-10400", "AMD Ryzen 7 7700X 8-Core Processor": "Ryzen 7 7700X",
             "Apple M3 Pro": "M3 Pro", "Intel(R) Core(TM) Ultra 7 155H": "Ultra 7 155H",
             "Intel(R) Xeon(R) CPU E5-2680 v4 @ 2.40GHz": "Xeon E5-2680 v4", "AMD EPYC 7543 32-Core Processor": "EPYC 7543",
             "AMD Ryzen 5 7600 6-Core Processor": "Ryzen 5 7600",
             "AMD Ryzen Threadripper 3970X 32-Core Processor": "Threadripper 3970X",
             "Raspberry Pi 4 Model B Rev 1.4": "Raspberry Pi 4 Model B", "Intel(R) N100": "N100", "": ""}
    for brand, short in cases.items():
        assert hw.short_cpu(brand) == short, brand
        assert server.short_cpu(brand) == short, brand  # the standalone lab server agrees
    gpus = {"NVIDIA GeForce RTX 4060 Laptop GPU": "RTX 4060", "AMD Radeon RX 7800 XT": "RX 7800 XT",
            "NVIDIA GeForce RTX 4070 Ti SUPER": "RTX 4070 Ti SUPER", "Intel(R) Arc(TM) A770 Graphics": "Arc A770", "": ""}
    for name, short in gpus.items():
        assert hw.short_gpu(name) == short and server.short_gpu(name) == short, name
    assert all(len(hw.short_cpu(b)) <= hw.MODEL_MAX for b in cases)


def test_host_names_are_short():
    assert hw.clean_host_name("Desk-PC.corp.example.com") == "Desk-PC"
    assert hw.clean_host_name("") == "This PC" and len(hw.clean_host_name("x" * 80)) == 31


def test_nvidia_line_carries_the_model():
    d = pcmod.parse_nvidia_line("4, 57, 29.80, 3200, 8192, NVIDIA GeForce RTX 4060 Laptop GPU")
    assert d["gpu_model"] == "RTX 4060" and d["gpu"] == 4.0


def test_pc_block_name_and_models(settings):
    c = pcmod.PcCollector(settings=settings)
    c.step()
    d = c.snapshot()
    assert d["name"] == c.hostname and "cpu_model" in d and d["gpu_model"] == ""
    assert "\u2022" not in d["name"]  # no more "CPU • GPU" in the name
    settings.update({"hud": {"pc_name": "Studio Caf\u00e9"}})
    c.step()
    assert c.snapshot()["name"] == "Studio Cafe"


def test_lab_block_names_and_models():
    out = labmod.clean_lab({"name": "nas", "cpu_model": "Ryzen 7 5700G", "gpu_model": "RTX 3060"})
    assert out["name"] == "nas" and out["cpu_model"] == "Ryzen 7 5700G" and out["gpu_model"] == "RTX 3060"
    assert labmod.clean_lab({"name": "nas"}, "Basement")["name"] == "Basement"
    assert labmod.clean_lab({})["cpu_model"] == ""


def test_lhm_cpu_temp_reads_package_sensor_by_type():
    from pandadeck.hud.pc import lhm_cpu_temp
    tree = {"Text": "Sensor", "Children": [{"Text": "PC", "Children": [{"Text": "CPU", "Children": [
        {"Text": "Powers", "Children": [{"Text": "CPU Package", "Type": "Power", "Value": "34.9 W"}]},
        {"Text": "Temperatures", "Children": [
            {"Text": "CPU Package", "Type": "Temperature", "Value": "88.0 \ufffdC"}]}]}]}]}
    assert lhm_cpu_temp(tree) == 88.0
    assert lhm_cpu_temp({}) == -1


# ---- round 2: the panel's gear, a cross-platform lab server with its own token ----------------

def test_settings_command_opens_the_page_rate_limited():
    now = [100.0]
    opened = threading.Event()
    calls = []

    def open_settings():
        calls.append(now[0])
        opened.set()

    a = Actions(open_settings=open_settings, clock=lambda: now[0])
    a.run({"k": "cmd", "a": "settings"})
    assert opened.wait(2)
    a.run({"k": "cmd", "a": "settings"})        # a second tap right away: ignored
    now[0] += 2.9
    a.run({"k": "cmd", "a": "settings"})
    now[0] += 0.2                                # 3.1 s after the first: opens again
    a.run({"k": "cmd", "a": "settings"})
    for _ in range(100):
        if len(calls) == 2:
            break
        threading.Event().wait(0.01)
    assert calls == [100.0, pytest.approx(103.1)]
    Actions().run({"k": "cmd", "a": "settings"})  # nothing to open with: no error


def test_service_opens_its_own_settings_url(fake_platform, monkeypatch):
    from pandadeck import service as svcmod
    opened = []
    monkeypatch.setattr(svcmod.webbrowser, "open", opened.append)
    svc = svcmod.Service(panel=False, platform=fake_platform)
    svc.open_settings_page()                     # not serving yet: nothing to open
    svc.port = 47399
    svc.open_settings_page()
    assert opened == ["http://127.0.0.1:47399/"]
    assert svc.actions.open_settings == svc.open_settings_page


def test_lab_token_is_made_once_and_kept(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("PANDADECK_LAB_TOKEN", raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    args = server.argparse.Namespace(token="", token_file=None, new_token=False)
    paths = server.token_paths(tmp_path / "script")
    tok, where = server.resolve_token(args, paths)
    assert where.startswith("new:") and len(tok) >= 32 and (tmp_path / "script" / server.TOKEN_FILE).exists()
    if sys.platform != "win32":
        assert ((tmp_path / "script" / server.TOKEN_FILE).stat().st_mode & 0o777) == 0o600
    assert server.resolve_token(args, paths) == (tok, str(paths[0]))           # reused, not regenerated
    assert server.main(["--show-token"], script_dir=tmp_path / "script") == 0
    assert capsys.readouterr().out.strip() == tok
    new, _ = server.resolve_token(server.argparse.Namespace(token="", token_file=None, new_token=True), paths)
    assert new != tok and server.stored_token(paths)[0] == new
    assert server.resolve_token(server.argparse.Namespace(token="x" * 20, token_file=None, new_token=False),
                                paths) == ("x" * 20, "--token")
    monkeypatch.setenv("PANDADECK_LAB_TOKEN", "y" * 20)
    assert server.resolve_token(args, paths)[0] == "y" * 20
    assert server.main(["--show-token"], script_dir=tmp_path / "empty") == 1   # no token yet


def test_lab_token_falls_back_to_the_config_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_writable", lambda folder: folder != tmp_path / "readonly")
    monkeypatch.setattr(server, "config_dir", lambda platform=sys.platform: tmp_path / "cfg")
    args = server.argparse.Namespace(token="", token_file=None, new_token=False)
    tok, where = server.resolve_token(args, server.token_paths(tmp_path / "readonly"))
    assert where == "new:" + str(tmp_path / "cfg" / server.TOKEN_FILE) and len(tok) >= 32


def test_lab_server_per_os_details(monkeypatch):
    assert server.DEFAULT_PORT == 47380 and server.DEFAULT_HOST == "0.0.0.0"
    assert server.disk_path("linux") == "/" and server.disk_path("darwin") == "/"
    monkeypatch.setenv("SystemDrive", "D:")
    assert server.disk_path("win32") == "D:\\"
    assert server.config_dir("darwin").parts[-3:] == ("Library", "Application Support", "pandadeck-lab")
    monkeypatch.setenv("XDG_CONFIG_HOME", "/cfg")
    assert server.config_dir("linux") == Path("/cfg") / "pandadeck-lab"
    lhm = {"Children": [{"Text": "CPU", "Children": [{"Text": "CPU Package", "Type": "Temperature", "Value": "61,5 \ufffdC"}]}]}
    assert server.lhm_cpu_temp(lhm) == 61.5 and server.lhm_cpu_temp({}) == -1
    runs = []
    monkeypatch.setattr(server, "_run", lambda argv, timeout: runs.append(argv) or
                        server.subprocess.CompletedProcess(argv, 0, "Apple M2 Pro\n", ""))
    assert server.cpu_brand("darwin") == "Apple M2 Pro" and runs[-1][-1] == "machdep.cpu.brand_string"
    assert server.short_cpu("Apple M2 Pro") == "M2 Pro"
    monkeypatch.setattr(server, "_lhm_temp", lambda: 58.25)
    assert server.cpu_temp("win32") == 58.2


def test_lab_status_names_the_connected_server(settings, fake_secrets):
    settings.update({"hud": {"lab_url": "http://box:47380"}})
    col = labmod.LabCollector(settings, fake_secrets,
                              get=lambda url, **kw: (200, {"ok": True, "name": "nas-01", "cpu": 3}))
    col.step()
    assert col.status == {"state": "ok", "error": "", "host": "nas-01"}


def test_settings_is_a_deck_button_action():
    from pandadeck.deck import grammar
    assert grammar.validate_action("agent:settings") == "agent:settings"
