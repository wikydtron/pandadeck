"""Tiny OS helpers that don't belong to the platform interface: open a URI or
folder with the default handler, start a program detached (never via a shell)."""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path

log = logging.getLogger("os")

NO_WINDOW = 0x08000000        # CREATE_NO_WINDOW
DETACHED = 0x00000008         # DETACHED_PROCESS


def open_uri(uri: str) -> bool:
    """URL, custom scheme (spotify:, ms-settings:) or a local path."""
    try:
        if uri.startswith(("http://", "https://")):
            return webbrowser.open(uri)
        if sys.platform == "win32":
            os.startfile(uri)  # noqa: S606 - user/deck-configured target, no shell parsing
        elif sys.platform == "darwin":
            subprocess.Popen(["open", uri], close_fds=True)  # noqa: S603,S607
        else:
            subprocess.Popen(["xdg-open", uri], close_fds=True, start_new_session=True)  # noqa: S603,S607
        return True
    except OSError as e:
        log.warning("couldn't open %s: %s", uri, e)
        return False


def spawn(exe_choices: str) -> bool:
    """First program of "a|b|c" found on PATH, started detached without arguments."""
    for name in exe_choices.split("|"):
        path = shutil.which(name.strip())
        if not path:
            continue
        try:
            if sys.platform == "win32":
                subprocess.Popen([path], creationflags=DETACHED, close_fds=True)  # noqa: S603
            else:
                subprocess.Popen([path], close_fds=True, start_new_session=True)  # noqa: S603
            return True
        except OSError as e:
            log.warning("couldn't start %s: %s", path, e)
    return False


def open_path(path: str) -> bool:
    """A file (with its associated app; .lnk/.url/.bat run as Explorer would) or a folder.
    Never through a shell string."""
    path = os.path.expanduser(path) if path else path
    if not path or not os.path.exists(path):
        log.warning("open: %s doesn't exist (anymore)", path)
        return False
    try:
        if sys.platform == "win32":
            os.startfile(path)  # noqa: S606 - the user picked this file for this button
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path], close_fds=True)  # noqa: S603,S607
        else:
            subprocess.Popen(["xdg-open", path], close_fds=True, start_new_session=True)  # noqa: S603,S607
        return True
    except OSError as e:
        log.warning("couldn't open %s: %s", path, e)
        return False


def open_target(target: dict) -> bool:
    """settings deck.targets entry -> opened?"""
    kind = (target or {}).get("kind")
    if kind in ("file", "folder"):
        return open_path(target.get("path", ""))
    if kind == "url" and str(target.get("url", "")).startswith(("http://", "https://")):
        return open_uri(target["url"])
    log.warning("unknown target %r", target)
    return False


RUNS_CODE = (".bat", ".cmd", ".ps1", ".sh", ".command", ".exe", ".com", ".msi", ".py", ".pyw", ".vbs", ".js",
             ".jar", ".app", ".lnk", ".url", ".desktop", ".appimage", ".run", ".bin")


def runs_code(path: str) -> bool:
    """For the settings page's warning: this button starts a program."""
    return str(path).lower().endswith(RUNS_CODE)


def launch_target(target: str, launch_app) -> bool:
    """deck/defaults.json launch targets: app:<id> | uri:<uri> | url:<url> | exe:<a|b> | dir:<path>."""
    kind, _, arg = target.partition(":")
    if kind == "app":
        launch_app(arg)
        return True
    if kind in ("uri", "url"):
        return open_uri(arg)
    if kind == "exe":
        return spawn(arg)
    if kind == "dir":
        return open_uri(str(Path(arg).expanduser()))
    log.warning("unknown launch target %r", target)
    return False
