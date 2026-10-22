""""Connect Claude": run `claude setup-token` for the user in a terminal window.

The command is interactive (it opens the browser to authorize and prints a
long-lived token at the end), so it gets a real terminal; the user pastes the
token into the settings page, which stores it write-only.
"""
from __future__ import annotations

import logging
import shlex
import shutil
import subprocess
import sys

log = logging.getLogger("connect")

LINUX_TERMINALS = (
    ("x-terminal-emulator", ["-e"]), ("gnome-terminal", ["--"]), ("konsole", ["-e"]),
    ("xfce4-terminal", ["-x"]), ("kitty", []), ("alacritty", ["-e"]), ("xterm", ["-e"]),
)


def claude_cli() -> str | None:
    return shutil.which("claude")


def start_setup_token(which=shutil.which, popen=subprocess.Popen, platform: str | None = None) -> dict:
    plat = platform or sys.platform
    cli = which("claude")
    if not cli:
        return {"started": False, "reason": "The Claude Code CLI isn't installed on this computer. "
                                            "Run `claude setup-token` where it is, then paste the token here."}
    try:
        if plat == "win32":
            popen(["cmd.exe", "/k", cli, "setup-token"], creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0x10),
                  close_fds=True)
        elif plat == "darwin":
            cmd = f"{shlex.quote(cli)} setup-token"
            script = 'tell application "Terminal"\nactivate\ndo script "{}"\nend tell'.format(
                cmd.replace("\\", "\\\\").replace('"', '\\"'))
            popen(["osascript", "-e", script], close_fds=True)
        else:
            inner = f"{shlex.quote(cli)} setup-token; echo; read -r -p 'Press Enter to close this window' _"
            for term, flag in LINUX_TERMINALS:
                if which(term):
                    popen([term, *flag, "bash", "-c", inner], close_fds=True, start_new_session=True)
                    break
            else:
                return {"started": False, "reason": "No terminal app found. Run `claude setup-token` in a terminal "
                                                    "and paste the token here."}
    except OSError as e:
        log.warning("could not start claude setup-token: %s", e)
        return {"started": False, "reason": f"Couldn't open a terminal ({e}). Run `claude setup-token` yourself."}
    return {"started": True}
