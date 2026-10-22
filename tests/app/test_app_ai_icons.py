"""✨ Generate with AI: Codex pictures (image generation, mocked), Claude vector
icons (SVGs out of model replies, the allowlist, the renderer fallback chain),
the CLI commands, the generation job, and the hook bridge ignoring PandaDeck's
own CLI runs."""
import io
import json
import subprocess
import threading

from pathlib import Path

import pytest
from PIL import Image

from pandadeck.ai import icons as ai
from pandadeck.ai.bridge import Bridge

SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256">'
       '<rect width="256" height="256" rx="48" fill="#D97757"/><circle cx="128" cy="128" r="60" fill="#fff"/></svg>')


def png(color=(200, 30, 30), px=256) -> bytes:
    img = Image.new("RGB", (px, px), (20, 20, 20))
    img.paste(Image.new("RGB", (px // 2, px // 2), color), (px // 4, px // 4))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


# ---- the reply ----------------------------------------------------------------------------------------------

def test_svgs_are_found_in_messy_replies():
    reply = ("Sure! Here are three ideas:\n\n```svg\n" + SVG + "\n```\nSecond one:\n```xml\n"
             + SVG.replace("#D97757", "#60A5FA") + "\n```\nand inline <SVG viewBox='0 0 256 256'><path d='M0 0h10v10z'/></SVG> done."
             + "\n```svg\n" + SVG + "\n```")  # a duplicate
    found = ai.extract_svgs(reply)
    assert len(found) == 3 and found[0] == SVG and "#60A5FA" in found[1] and found[2].startswith("<SVG")
    assert ai.extract_svgs("no pictures, sorry") == []
    huge = "<svg>" + "<path d='M0 0'/>" * 20000 + "</svg>"
    assert ai.extract_svgs(huge) == []                                            # over the size cap


# ---- the allowlist ------------------------------------------------------------------------------------------------

def test_sanitizer_keeps_shapes_and_drops_everything_risky():
    dirty = """<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 256 256"
        onload="alert(1)" width="9999">
      <script>alert(1)</script>
      <style>@import url(https://evil.example/x.css);</style>
      <defs><linearGradient id="g"><stop offset="0" stop-color="#f00"/></linearGradient>
            <linearGradient id="h" xlink:href="https://evil.example/g.svg#x"/></defs>
      <rect width="256" height="256" fill="url(#g)" onclick="steal()" style="fill:url(https://evil.example/a);stroke:#000"/>
      <image href="https://evil.example/track.png" width="10" height="10"/>
      <use xlink:href="https://evil.example/sprite.svg#a"/>
      <foreignObject><div xmlns="http://www.w3.org/1999/xhtml">hi</div></foreignObject>
      <a href="javascript:alert(1)"><circle r="10"/></a>
      <path d="M10 10 L 200 200" stroke="#fff" filter="url(#f)" transform="rotate(10 128 128)"/>
    </svg>"""
    out = ai.sanitize_svg(dirty)
    for bad in ("script", "onload", "onclick", "evil.example", "<image", "<use", "foreignObject", "javascript", "<style",
                "@import", "<a ", "filter="):
        assert bad not in out, bad
    for good in ('fill="url(#g)"', "stroke:#000", "rotate(10 128 128)", "M10 10 L 200 200", 'width="256"', "stop-color"):
        assert good in out, good
    assert ai.sanitize_svg(out) == out                                            # stable


@pytest.mark.parametrize("bad", [
    '<?xml version="1.0"?><!DOCTYPE svg [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;">]><svg>&b;</svg>',
    '<!DOCTYPE svg SYSTEM "file:///etc/passwd"><svg/>',
    "<svg>" + "<rect/>" * 40000 + "</svg>",
    "<html><body>not svg</body></html>",
    "<svg><unclosed></svg>",
    '<svg xmlns="http://www.w3.org/2000/svg"><title>empty</title></svg>',
    "",
], ids=["entity-bomb", "external-doctype", "huge", "not-svg", "broken", "nothing-to-draw", "empty"])
def test_sanitizer_refuses_dangerous_or_useless_input(bad):
    with pytest.raises(ai.IconError):
        ai.sanitize_svg(bad)


# ---- drawing -----------------------------------------------------------------------------------------------------

def test_renderer_fallback_chain():
    calls = []

    def missing(svg, px):
        calls.append("missing")
        raise ImportError("no module named resvg_py")

    def garbage(svg, px):
        calls.append("garbage")
        return b"not a png"

    def blank(svg, px):
        calls.append("blank")
        buf = io.BytesIO()
        Image.new("RGB", (px, px), (0, 0, 0)).save(buf, "PNG")
        return buf.getvalue()

    def good(svg, px):
        calls.append("good")
        return png(px=px * 2)                                                   # wrong size: resized

    out = ai.render_png(SVG, 256, renderers=[("a", missing), ("b", garbage), ("c", blank), ("d", good)])
    assert calls == ["missing", "garbage", "blank", "good"] and Image.open(io.BytesIO(out)).size == (256, 256)
    with pytest.raises(ai.IconError, match="Couldn't draw"):
        ai.render_png(SVG, 256, renderers=[("a", missing), ("b", garbage)])
    assert ai.renderer_available([("nope", missing)]) == ""


# ---- the CLIs -----------------------------------------------------------------------------------------------------

def test_cli_commands(tmp_path):
    claude = ai.build_command("claude", ["claude"], "PROMPT", tmp_path)
    assert claude[:2] == ["claude", "-p"] and claude[-1] == "PROMPT"
    assert claude[claude.index("--tools") + 1] == "" and claude[claude.index("--permission-mode") + 1] == "dontAsk"
    codex = ai.build_command("codex", ["node", "codex.js"], "PROMPT", tmp_path)
    assert codex[:4] == ["node", "codex.js", "--search", "exec"] and codex[-1] == "PROMPT"
    assert codex[codex.index("--sandbox") + 1] == "read-only" and "--skip-git-repo-check" in codex
    assert codex[codex.index("-C") + 1] == str(tmp_path)
    # lean and predictable: not the user's coding setup (model, plugins, MCP, memories, hooks)
    assert "--ignore-user-config" in codex and "--json" in codex and "--ephemeral" in codex
    assert codex[codex.index("--enable") + 1] == "image_generation"
    disabled = {codex[i + 1] for i, a in enumerate(codex) if a == "--disable"}
    assert {"plugins", "apps", "memories", "hooks", "shell_tool"} <= disabled   # web search + images only
    assert codex.index("--search") < codex.index("exec") and "-i" not in codex   # --search is a global flag
    ref = tmp_path / "reference.png"
    withref = ai.build_command("codex", ["codex"], "PROMPT", tmp_path, reference=ref)
    i = withref.index("-i")
    assert withref[i - 1] == "exec" and withref[i + 1] == str(ref) and withref[i + 2].startswith("--")
    assert withref[-1] == "PROMPT"                        # -i takes several files: the prompt must not follow it
    for argv in (claude, codex):
        assert not any("bypass" in a or "dangerously" in a or "yolo" in a or "full-auto" in a for a in argv)
    with pytest.raises(ai.IconError):
        ai.build_command("gpt", ["x"], "p", tmp_path)


def test_engines_found_without_running_cmd_shims(tmp_path):
    shim_dir = tmp_path / "npm"
    (shim_dir / "node_modules" / "@openai" / "codex" / "bin").mkdir(parents=True)
    (shim_dir / "node_modules" / "@openai" / "codex" / "bin" / "codex.js").write_text("// cli")
    (shim_dir / "node.exe").write_text("")
    (shim_dir / "codex.CMD").write_text('@ECHO off\r\n"%_prog%"  "%dp0%\\node_modules\\@openai\\codex\\bin\\codex.js" %*\r\n')
    (shim_dir / "weird.cmd").write_text("@echo off\r\ncall something %*\r\n")
    found = ai.detect_engines(lambda n: {"claude": r"C:\bin\claude.EXE", "codex": str(shim_dir / "codex.CMD")}.get(n))
    assert found["claude"] == [r"C:\bin\claude.EXE"]
    assert found["codex"][0].endswith("node.exe") and found["codex"][1].endswith("codex.js")    # never through cmd.exe
    assert ai.detect_engines(lambda n: str(shim_dir / "weird.cmd") if n == "codex" else None) == {}
    assert ai.detect_engines(lambda n: None) == {}


def test_picture_prompt_has_what_the_image_needs():
    p = ai.make_image_prompt({"label": "Steel Drum", "kind": "sound", "sound": "Steel drum roll",
                              "page_title": "Steel Drum Roll Sound Effect"}, "pixel art", "a playful cartoon take")
    for part in ("image generation tool", "one square picture", '"Steel Drum"', "plays a sound", "Steel drum roll",
                 "Steel Drum Roll Sound Effect", "pixel art", "a playful cartoon take", "90 pixels", "no text",
                 "Don't run commands"):
        assert part in p, part
    assert "glossy modern app icon" in ai.make_image_prompt({"label": "x"}, "", ai.IMAGE_DIRECTIONS[0])
    assert "look it up on the web" in p and "attached" not in p
    with_ref = ai.make_image_prompt({"label": "Big Horn", "kind": "sound"}, "", ai.IMAGE_DIRECTIONS[0], reference=True)
    assert "attached image" in with_ref and "reference" in with_ref


def test_prompt_has_what_the_model_needs():
    p = ai.make_prompt({"label": 'Deep "Thud"\n<x>', "kind": "sound", "sound": "Deep thud", "color": "#60A5FA",
                        "page_title": "Deep Thud Sound Effect"}, style="pixel art")
    for part in ('"Deep "Thud" <x>"', "plays a sound", "Deep thud", "#60A5FA", "pixel art", 'viewBox="0 0 256 256"',
                 "exactly 3 SVG"):
        assert part in p, part
    assert "\n<x>" not in p and "#D97757" in ai.make_prompt({"label": "x", "color": "red"})


# ---- the job --------------------------------------------------------------------------------------------------------

class FakeRun:
    def __init__(self, stdout="", code=0, stderr="", raise_=None, write_last=None):
        self.stdout, self.code, self.stderr, self.raise_, self.write_last = stdout, code, stderr, raise_, write_last
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        assert kw["cwd"] and kw["timeout"] == ai.TIMEOUT_S and kw.get("shell") is None and kw["stdin"] is subprocess.DEVNULL
        if self.raise_:
            raise self.raise_
        if self.write_last is not None:
            (__import__("pathlib").Path(kw["cwd"]) / "last-message.txt").write_text(self.write_last, encoding="utf-8")
        return subprocess.CompletedProcess(argv, self.code, self.stdout, self.stderr)


def run_job(tmp_path, engine, run, engines=None):
    colors = iter([(200, 0, 0), (0, 200, 0), (0, 0, 200), (200, 200, 0)])
    job = ai.IconJob(tmp_path, engines=lambda: engines or {"claude": ["claude"], "codex": ["codex"]}, run=run,
                     render=lambda svg: png(next(colors)), codex_home=tmp_path / "codex-home")
    job.start(engine, {"label": "Clang", "kind": "sound"}, "cartoon")
    for _ in range(300):
        if job.status()["state"] != "running":
            break
        threading.Event().wait(0.01)
    return job, job.status()


def test_job_gets_three_pictures(tmp_path):
    reply = "Here you go:\n" + "\n".join(f"```svg\n{SVG.replace('#fff', c)}\n```" for c in ("#fff", "#000", "#f0f", "#0ff"))
    job, st = run_job(tmp_path, "claude", FakeRun(stdout=reply))
    assert st["state"] == "done" and len(st["previews"]) == 3
    assert job.png(st["previews"][0]["cid"])[:4] == b"\x89PNG" and job.png("../../x") is None


class FakeCodex:
    """codex exec --json: prints thread.started and saves its picture where Codex
    does ($CODEX_HOME/generated_images/<thread>/exec-*.png)."""

    def __init__(self, home, size=(1254, 1254), fail_on=(), thread=None, code=0, stderr="", no_image=False):
        self.home, self.size, self.fail_on, self.thread, self.code = home, size, fail_on, thread, code
        self.stderr, self.no_image = stderr, no_image
        self.prompts, self.cwds, self.lock, self.n = [], [], threading.Lock(), 0

    def __call__(self, argv, **kw):
        with self.lock:
            self.n += 1
            n = self.n
            self.prompts.append(argv[-1])
            self.cwds.append(kw["cwd"])
        assert kw.get("shell") is None and kw["stdin"] is subprocess.DEVNULL and kw["timeout"] == ai.IMAGE_TIMEOUT_S
        if n in self.fail_on or self.code:
            return subprocess.CompletedProcess(argv, self.code or 1, "", self.stderr or "boom")
        thread = self.thread or f"01a15fab-0000-7000-8000-00000000000{n}"
        if not self.no_image:
            d = self.home / "generated_images" / thread
            d.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", self.size, (40 * n, 90, 200)).save(d / f"exec-{n}.png")
        out = "\n".join([json.dumps({"type": "thread.started", "thread_id": thread}), "not json",
                         json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "DONE"}})])
        return subprocess.CompletedProcess(argv, 0, out, "")


def wait(job):
    for _ in range(500):
        if job.status()["state"] != "running":
            break
        threading.Event().wait(0.01)
    return job.status()


def test_codex_makes_three_pictures_in_parallel_and_tidies_up(tmp_path):
    fake = FakeCodex(tmp_path / "codex-home", size=(1254, 836))                    # not square: cropped
    registered = []
    job = ai.IconJob(tmp_path / "cache", engines=lambda: {"codex": ["codex"]}, run=fake, render=lambda s: png(),
                     on_workdir=registered.append, codex_home=tmp_path / "codex-home")
    job.start("codex", {"label": "Steel Drum", "kind": "sound"}, "")
    st = wait(job)
    assert st["state"] == "done" and len(st["previews"]) == 3
    for p in st["previews"]:
        assert Image.open(io.BytesIO(job.png(p["cid"]))).size == (ai.PNG_PX, ai.PNG_PX)
    assert sorted(any(d in p for p in fake.prompts) for d in ai.IMAGE_DIRECTIONS) == [True] * 3   # an idea per run
    assert sorted(fake.cwds) == sorted(registered) and len(set(registered)) == 3
    assert not list((tmp_path / "codex-home" / "generated_images").iterdir())     # Codex's copies removed


def test_codex_runs_get_the_reference_picture(tmp_path):
    seen = []
    fake = FakeCodex(tmp_path / "codex-home")
    orig = fake.__call__

    def spy(argv, **kw):
        ref = Path(argv[argv.index("-i") + 1])
        seen.append((ref.parent == Path(kw["cwd"]), ref.read_bytes(), "attached image" in argv[-1]))
        return orig(argv, **kw)
    job = ai.IconJob(tmp_path / "cache", engines=lambda: {"codex": ["codex"]}, run=spy, render=lambda s: png(),
                     codex_home=tmp_path / "codex-home")
    ref = png((10, 200, 10))
    job.start("codex", {"label": "Big Horn", "kind": "sound"}, "", reference=ref)
    assert wait(job)["state"] == "done"
    assert seen == [(True, ref, True)] * 3                  # each run: its own copy, in its own folder


def test_codex_shows_what_it_got_and_explains_when_nothing_came(tmp_path):
    _, st = run_job(tmp_path, "codex", FakeCodex(tmp_path / "codex-home", fail_on=(2,)))
    assert st["state"] == "done" and len(st["previews"]) == 2                     # one run failed: the others count
    _, st = run_job(tmp_path, "codex", FakeCodex(tmp_path / "codex-home", no_image=True))
    assert st["state"] == "error" and "didn't make a picture" in st["error"]


def test_codex_thread_id_never_leads_outside_its_folder(tmp_path):
    home = tmp_path / "codex-home"
    keep = tmp_path / "precious"
    keep.mkdir()
    (keep / "exec-1.png").write_bytes(png())
    _, st = run_job(tmp_path, "codex", FakeCodex(home, thread="../../precious", no_image=True))
    assert st["state"] == "error" and (keep / "exec-1.png").exists()


@pytest.mark.parametrize("run, msg", [
    (FakeRun(code=1, stderr="Error: Please log in with `claude login`"), "isn't signed in"),
    (FakeRun(raise_=subprocess.TimeoutExpired("claude", 90)), "took too long"),
    (FakeRun(stdout="I can't draw, sorry."), "didn't return a picture"),
    (FakeRun(stdout="```svg\n<svg><script>x</script></svg>\n```"), "didn't return a picture"),
    (FakeRun(code=2, stderr="usage limit reached"), "usage limit"),
    (FakeRun(code=1, stderr="Failed to authenticate: OAuth session expired and could not be refreshed"), "expired"),
    (FakeRun(code=1, stdout='{"type":"turn.failed","error":{"message":"exceeded retry limit, last status: 429 Too Many '
                            'Requests"}}'), "Wait a few minutes"),
])
def test_job_errors_are_plain(tmp_path, run, msg):
    _, st = run_job(tmp_path, "claude", run)
    assert st["state"] == "error" and msg in st["error"]


def test_each_run_has_its_own_temp_folder_outside_the_cache(tmp_path):
    """A Microsoft Store Python hides what it creates under AppData from other programs,
    so the CLI must run in a fresh temp folder: registered for the bridge before the run,
    removed after it."""
    seen, registered = [], []
    run = FakeRun(stdout="```svg\n" + SVG + "\n```")
    orig = run.__call__

    def spy(argv, **kw):
        seen.append(kw["cwd"])
        assert kw["cwd"] in registered                       # ignored before the CLI starts
        assert __import__("os").path.isdir(kw["cwd"]) and not __import__("os").listdir(kw["cwd"])
        return orig(argv, **kw)
    job = ai.IconJob(tmp_path / "cache", engines=lambda: {"claude": ["claude"]}, run=spy,
                     render=lambda s: png(), on_workdir=registered.append, tmp_root=str(tmp_path))
    for _ in range(2):
        job.start("claude", {"label": "Clang", "kind": "sound"})
        for _ in range(300):
            if job.status()["state"] != "running":
                break
            threading.Event().wait(0.01)
        assert job.status()["state"] == "done"
    assert len(seen) == 2 and seen[0] != seen[1]
    assert all(not str(d).startswith(str(tmp_path / "cache")) for d in seen)
    assert all(not __import__("os").path.exists(d) for d in seen)   # cleaned up


def test_codex_model_the_account_cannot_use_is_named(tmp_path):
    err = ('ERROR: {"type":"error","status":400,"error":{"type":"invalid_request_error","message":'
           '"The \'gpt-x-old\' model is not supported when using Codex with a ChatGPT account."}}')
    _, st = run_job(tmp_path, "codex", FakeCodex(tmp_path / "codex-home", code=1, stderr=err))
    assert st["state"] == "error" and "gpt-x-old" in st["error"] and "/model" in st["error"]


def test_codex_runs_with_light_reasoning(tmp_path):
    argv = ai.build_command("codex", ["codex"], "draw", tmp_path)
    i = argv.index("-c")
    assert argv[i + 1] == 'model_reasoning_effort="low"' and argv[-1] == "draw"


def test_job_needs_an_installed_engine(tmp_path):
    job = ai.IconJob(tmp_path, engines=lambda: {"codex": ["codex"]}, run=FakeRun(), render=lambda s: png())
    with pytest.raises(ai.IconError, match="isn't installed"):
        job.start("claude", {"label": "x"})


# ---- our own runs never reach the AI page -----------------------------------------------------------------------------

def test_bridge_ignores_the_generation_folder(tmp_path):
    b = Bridge("pc")
    b.ignore_dirs.append(str(tmp_path / "ai-icons" / "run"))
    inside = {"hook_event_name": "UserPromptSubmit", "session_id": "s1", "cwd": str(tmp_path / "ai-icons" / "run")}
    perm = {"hook_event_name": "PermissionRequest", "session_id": "s2", "cwd": str(tmp_path / "ai-icons" / "run" / "sub"),
            "tool_name": "Bash", "tool_input": {"command": "rm -rf /"}}
    outside = {"hook_event_name": "UserPromptSubmit", "session_id": "s3", "cwd": str(tmp_path / "project")}
    for p in (inside, perm):
        assert b.handle_hook(dict(p), "claude", lambda: True, 1, lambda: True) == b""
    b.handle_hook(dict(outside), "claude", lambda: True, 1, lambda: True)
    sessions, perms = b.snapshot_lists() if hasattr(b, "snapshot_lists") else (b._sessions, b._perms)
    assert list(sessions) == ["s3"] and not perms
    assert not b.ignored({"cwd": str(tmp_path / "ai-icons" / "runner")})        # a sibling folder isn't ignored


def test_ai_icon_api(fake_platform, monkeypatch):
    import http.client

    from pandadeck.service import Service
    from pandadeck.web import api
    from pandadeck.web.server import make_server
    svc = Service(panel=False, platform=fake_platform)
    assert svc.ai_icons._on_workdir == svc.bridge.ignore_dirs.append      # each run's folder is ignored
    monkeypatch.setattr(svc, "_ai_engines", lambda: {"claude": ["claude"]})
    monkeypatch.setattr(svc, "_ai_renderer", lambda: "resvg")
    started = []
    monkeypatch.setattr(svc.ai_icons, "_work", lambda *a: started.append(a))       # never a real CLI run
    svc.ai_icons.folder.mkdir(parents=True, exist_ok=True)
    data = png()
    cid = __import__("hashlib").sha1(data).hexdigest()
    (svc.ai_icons.folder / f"{cid}.png").write_bytes(data)
    srv = make_server(svc, 0, api.ROUTES)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def call(method, path, body=None, guarded=True):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        c.request(method, path, body=json.dumps(body).encode() if body is not None else None,
                  headers={"Host": f"127.0.0.1:{port}", "Content-Type": "application/json",
                           **({"X-PandaDeck": "1"} if guarded else {})})
        r = c.getresponse()
        raw = r.read()
        c.close()
        try:
            return r.status, json.loads(raw)
        except ValueError:
            return r.status, raw

    try:
        assert call("GET", "/api/ai/icons")[1]["engines"] == [{"id": "claude", "name": "Claude", "kind": "vector"}]
        assert svc.meta()["ai_icons"]["renderer"] == "resvg"
        assert call("POST", "/api/ai/icons", {"engine": "codex", "info": {"label": "x"}})[0] == 400  # not installed
        assert started == []
        assert call("GET", f"/api/ai/icons/thumb?cid={cid}", guarded=False)[1][:4] == b"\x89PNG"
        status, r = call("POST", "/api/ai/icons/choose", {"cid": cid})
        assert status == 200 and len(r["image"]) == 40
        assert call("POST", "/api/ai/icons/choose", {"cid": "0" * 40})[0] == 404
        monkeypatch.setattr(svc, "_ai_renderer", lambda: "")
        assert call("GET", "/api/ai/icons")[1]["engines"] == []                     # Claude needs a renderer
        monkeypatch.setattr(svc, "_ai_engines", lambda: {"claude": ["claude"], "codex": ["codex"]})
        assert call("GET", "/api/ai/icons")[1]["engines"] == [{"id": "codex", "name": "Codex", "kind": "picture"}]
        assert call("POST", "/api/ai/icons", {"engine": "claude", "info": {"label": "x"}})[0] == 501
    finally:
        srv.shutdown()
        srv.server_close()


def test_reference_is_the_buttons_own_picture_or_a_confident_meme(fake_platform):
    from pandadeck.deck import userimg
    from pandadeck.service import Service
    svc = Service(panel=False, platform=fake_platform)
    mine = userimg.save_upload(png((200, 20, 20)))
    meme = userimg.save_upload(png((20, 20, 200)))
    svc.artwork.catalog.upsert("Big Horn", aliases=["big horn meme"], image_id=meme)
    assert svc.ai_reference({"label": "Whatever", "image": mine, "image_origin": "manual"}) == userimg.png_bytes(mine)
    assert svc.ai_reference({"label": "Big Horn", "kind": "sound", "image": mine, "image_origin": "ai"}) \
        == userimg.png_bytes(meme)                                                  # never an AI picture of itself
    assert svc.ai_reference({"label": "Big Horn", "kind": "sound"}) == userimg.png_bytes(meme)
    assert svc.ai_reference({"label": "Something else", "kind": "sound"}) is None
    assert svc.ai_reference({"label": "x", "image": "../../etc/passwd"}) is None
