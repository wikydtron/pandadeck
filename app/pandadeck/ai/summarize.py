"""Turn raw Claude Code hook payloads into short, panel-sized text.

Pure functions (no I/O).
The panel's fonts are ASCII + a few symbols, so everything is folded to ASCII.
"""
from __future__ import annotations

import os
import re
import unicodedata

ACT_MAX = 110       # one mono-16 line on a session card
DETAIL_MAX = 600    # permission card detail box (fits panel PermInfo.detail[640])

_FOLD = {
    "‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-",
    "…": "...", " ": " ", "→": "->", "•": "*",
}


def fold_ascii(text: str) -> str:
    """Best-effort ASCII fold: smart quotes/dashes mapped, accents stripped."""
    if not text:
        return ""
    for k, v in _FOLD.items():
        text = text.replace(k, v)
    norm = unicodedata.normalize("NFKD", text)
    out = "".join(ch for ch in norm if ord(ch) < 128 and (ch.isprintable() or ch in "\n\t"))
    return out


def one_line(text: str, limit: int = ACT_MAX) -> str:
    text = fold_ascii(text or "")
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[: limit - 3].rstrip() + "..."
    return text


def clip(text: str, limit: int) -> str:
    text = fold_ascii(text or "")
    if len(text) > limit:
        text = text[: limit - 3].rstrip() + "..."
    return text


def short_path(path: str, cwd: str | None = None) -> str:
    """Path relative to the session cwd when inside it, else the last 2 parts."""
    if not path:
        return ""
    p = path.replace("\\", "/")
    if cwd:
        c = cwd.replace("\\", "/").rstrip("/") + "/"
        if p.lower().startswith(c.lower()):
            return p[len(c):]
    parts = [x for x in p.split("/") if x]
    return "/".join(parts[-2:]) if len(parts) > 2 else p


def project_name(cwd: str | None) -> str:
    if not cwd:
        return "session"
    name = os.path.basename(cwd.replace("\\", "/").rstrip("/"))
    # Folders named like scratch-<date>-<id> are temporary workspaces.
    if name.startswith("scratch-"):
        return "scratch"
    return fold_ascii(name) or "session"


def tool_label(tool: str) -> str:
    """mcp__server__tool -> server.tool ; plain tools unchanged."""
    if tool and tool.startswith("mcp__"):
        parts = tool.split("__")
        if len(parts) >= 3:
            server = parts[1]
            # long UUID-ish server ids add nothing on a 5" screen
            if re.fullmatch(r"[0-9a-f-]{20,}", server):
                server = "mcp"
            return f"{server}.{'__'.join(parts[2:])}"
    return tool or "tool"


def _first(d: dict, *keys: str) -> str:
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v
    return ""


def tool_activity(tool: str, tool_input: dict | None, cwd: str | None = None) -> str:
    """One-line 'what is it doing' for a session card."""
    ti = tool_input if isinstance(tool_input, dict) else {}
    label = tool_label(tool)
    if tool in ("Bash", "PowerShell"):
        detail = _first(ti, "description") or _first(ti, "command")
    elif tool in ("Read", "Write", "Edit", "MultiEdit", "NotebookEdit"):
        detail = short_path(_first(ti, "file_path", "notebook_path"), cwd)
    elif tool in ("Grep", "Glob"):
        detail = _first(ti, "pattern")
    elif tool in ("WebFetch",):
        detail = re.sub(r"^https?://", "", _first(ti, "url"))
    elif tool in ("WebSearch",):
        detail = _first(ti, "query")
    elif tool in ("Task", "Agent"):
        detail = _first(ti, "description", "prompt")
    else:
        detail = _first(ti, "description", "command", "query", "url", "path", "file_path", "name")
    return one_line(f"{label}: {detail}" if detail else label)


PANEL_COLS = 60   # mono-20 chars per line in the approval card's detail box
PANEL_ROWS = 4    # lines visible without scrolling


def fits_panel(text: str, cols: int = PANEL_COLS, rows: int = PANEL_ROWS) -> bool:
    """True when the whole text is visible on the approval card unscrolled and
    nothing would be lost to ASCII folding - i.e. what you approve is what you
    saw. Anything else is left to the PC's own permission dialog."""
    if not text or not text.isascii():
        return False
    lines = 0
    for raw in text.split("\n"):
        lines += max(1, -(-len(raw) // cols))
    return lines <= rows


def permission_detail_raw(tool: str, tool_input: dict | None, cwd: str | None = None) -> str:
    """The complete thing being approved, unclipped and unfolded. The panel
    only shows it when it fits whole (fits_panel); otherwise the PC decides,
    so what you approve on the panel is always everything there is."""
    import json
    ti = tool_input if isinstance(tool_input, dict) else {}
    if tool in ("Bash", "PowerShell"):
        cmd = _first(ti, "command")
        desc = _first(ti, "description")
        return (cmd + (f"\n# {desc}" if desc else "")).strip()
    if tool in ("WebFetch",):
        return _first(ti, "url")
    if tool in ("Write",):
        return (short_path(_first(ti, "file_path"), cwd) + "\n\n" + str(ti.get("content") or "")).strip()
    if tool in ("Edit", "MultiEdit"):
        text = short_path(_first(ti, "file_path"), cwd)
        if "edits" in ti:
            return (text + "\n\n" + json.dumps(ti["edits"], ensure_ascii=False)).strip()
        old, new = str(ti.get("old_string") or ""), str(ti.get("new_string") or "")
        return (text + (f"\n\n- {old}\n+ {new}" if old or new else "")).strip()
    parts = []
    for k, v in ti.items():
        if isinstance(v, (str, int, float, bool)) or v is None:
            parts.append(f"{k}: {v}")
        else:
            parts.append(f"{k}: {json.dumps(v, ensure_ascii=False)}")
    return "\n".join(parts) or tool_label(tool)


def permission_detail(tool: str, tool_input: dict | None, cwd: str | None = None) -> str:
    """Multi-line detail for the approval card: the thing being approved."""
    ti = tool_input if isinstance(tool_input, dict) else {}
    if tool in ("Bash", "PowerShell"):
        cmd = _first(ti, "command")
        desc = _first(ti, "description")
        text = cmd + (f"\n# {desc}" if desc else "")
    elif tool in ("Write",):
        text = short_path(_first(ti, "file_path"), cwd) + "\n\n" + _first(ti, "content")[:400]
    elif tool in ("Edit", "MultiEdit"):
        old = _first(ti, "old_string")
        new = _first(ti, "new_string")
        text = short_path(_first(ti, "file_path"), cwd)
        if old or new:
            text += f"\n\n- {one_line(old, 200)}\n+ {one_line(new, 200)}"
    elif tool in ("WebFetch",):
        text = _first(ti, "url")
    else:
        parts = []
        for k, v in ti.items():
            if isinstance(v, (str, int, float, bool)):
                parts.append(f"{k}: {v}")
        text = "\n".join(parts) or tool_label(tool)
    return clip(text.strip(), DETAIL_MAX)


def model_short(model: str | None) -> str:
    """claude-opus-5-5 -> opus 5.5 ; anything else passes through trimmed."""
    if not model:
        return ""
    m = re.match(r"(?:claude-)?(opus|sonnet|haiku|fable)-(\d+)(?:-(\d+))?", model)
    if m:
        ver = m.group(2) + (f".{m.group(3)}" if m.group(3) and len(m.group(3)) <= 2 else "")
        return f"{m.group(1)} {ver}"
    return one_line(model, 20)
