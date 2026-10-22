"""The hook summarizer: raw hook payloads -> short panel text."""
from pandadeck.ai import summarize as s


def test_text_folding():
    assert s.fold_ascii("Hunter\u2019s \u201cMoon\u201d \u2014 caf\u00e9\u2026") == "Hunter's \"Moon\" - cafe..."
    out = s.one_line("a\n\n  b\tc " + "x" * 200, limit=20)
    assert out.startswith("a b c ") and len(out) == 20 and out.endswith("...")
    assert s.clip("abcdef", 5) == "ab..." and s.fold_ascii("") == ""


def test_paths_and_projects():
    assert s.project_name("D:\\work\\Projects\\DeskBox") == "DeskBox"
    assert s.project_name("/srv/x/scratch-2026-01-01-abcdef") == "scratch" and s.project_name(None) == "session"
    assert s.short_path("C:\\proj\\src\\a.ts", "C:\\proj") == "src/a.ts"
    assert s.short_path("/var/lib/x/y/z.txt", "/home") == "y/z.txt" and s.short_path("") == ""


def test_tool_labels_and_activity():
    assert s.tool_label("mcp__github__create_issue") == "github.create_issue"
    assert s.tool_label("mcp__0123456789abcdef0123__do") == "mcp.do" and s.tool_label("") == "tool"
    assert s.tool_activity("Bash", {"description": "Run tests", "command": "pytest"}) == "Bash: Run tests"
    assert s.tool_activity("Edit", {"file_path": "/p/src/x.py"}, "/p") == "Edit: src/x.py"
    assert s.tool_activity("Grep", {"pattern": "TODO"}) == "Grep: TODO"
    assert s.tool_activity("WebFetch", {"url": "https://example.com/a"}) == "WebFetch: example.com/a"
    assert s.tool_activity("WebSearch", {"query": "lvgl"}) == "WebSearch: lvgl"
    assert s.tool_activity("Task", {"description": "explore"}) == "Task: explore"
    assert s.tool_activity("Other", None) == "Other"


def test_permission_details():
    assert s.permission_detail_raw("Bash", {"command": "rm -rf build", "description": "Clean"}) == "rm -rf build\n# Clean"
    assert s.permission_detail_raw("WebFetch", {"url": "https://x"}) == "https://x"
    assert s.permission_detail("Write", {"file_path": "/p/a.txt", "content": "hi"}, "/p") == "a.txt\n\nhi"
    edit = s.permission_detail("Edit", {"file_path": "/p/a.py", "old_string": "x = 1", "new_string": "x = 2"}, "/p")
    assert edit == "a.py\n\n- x = 1\n+ x = 2"
    assert s.permission_detail("Custom", {"a": 1, "b": "two", "c": {"nested": 1}}) == "a: 1\nb: two"
    assert len(s.permission_detail("Bash", {"command": "x" * 2000})) == s.DETAIL_MAX


def test_fits_panel_and_model_names():
    assert s.fits_panel("ls -la") and not s.fits_panel("") and not s.fits_panel("caf\u00e9")
    assert not s.fits_panel("\n".join(["x"] * 5)) and not s.fits_panel("x" * 300)
    assert s.model_short("claude-opus-5-5") == "opus 5.5" and s.model_short("claude-haiku-4-5-20251001") == "haiku 4.5"
    assert s.model_short("gpt-5-codex") == "gpt-5-codex" and s.model_short(None) == ""
