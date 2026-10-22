"""A native "choose a file / folder" dialog on the user's desktop, for the
settings page's Browse buttons (a web page can't see real paths).

    macOS   osascript `choose file` / `choose folder` (Tk must not run off the main thread there)
    Linux   zenity or kdialog when installed, else tkinter
    Windows tkinter (hidden, topmost root on its own short-lived thread)

One dialog at a time; the caller gets the absolute path, None when the user
cancels, or PickerUnavailable when this system has no way to show one (the
page then offers its manual path field).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading

TIMEOUT_S = 600

_busy = threading.Lock()


class PickerUnavailable(RuntimeError):
    pass


class PickerBusy(RuntimeError):
    pass


def pick(kind: str, timeout: float = TIMEOUT_S, platform: str | None = None) -> str | None:
    if kind not in ("file", "folder"):
        raise ValueError("kind is file or folder")
    if not _busy.acquire(blocking=False):
        raise PickerBusy("A file picker is already open on this computer")
    try:
        plat = platform or sys.platform
        if plat == "darwin":
            return _osascript(kind, timeout)
        if plat.startswith("linux"):
            for tool in (_zenity, _kdialog):
                found = tool(kind, timeout)
                if found is not NotImplemented:
                    return found
        return _tk(kind, timeout)
    finally:
        _busy.release()


def _run(argv: list[str], timeout: float) -> str | None:
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)  # noqa: S603
    except subprocess.TimeoutExpired:
        return None
    path = out.stdout.strip()
    return path if out.returncode == 0 and path else None


def _osascript(kind: str, timeout: float) -> str | None:
    if not shutil.which("osascript"):
        raise PickerUnavailable("osascript is missing")
    what = "choose folder" if kind == "folder" else "choose file"
    script = f'POSIX path of ({what} with prompt "Choose for your PandaDeck button")'
    path = _run(["osascript", "-e", script], timeout)
    return path.rstrip("/") if path and kind == "folder" and len(path) > 1 else path


def _zenity(kind: str, timeout: float):
    if not shutil.which("zenity") or not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        return NotImplemented
    argv = ["zenity", "--file-selection", "--title=Choose for your PandaDeck button"]
    if kind == "folder":
        argv.append("--directory")
    return _run(argv, timeout)


def _kdialog(kind: str, timeout: float):
    if not shutil.which("kdialog"):
        return NotImplemented
    argv = ["kdialog", "--getexistingdirectory" if kind == "folder" else "--getopenfilename", os.path.expanduser("~")]
    return _run(argv, timeout)


def _tk(kind: str, timeout: float) -> str | None:
    try:
        import tkinter
        from tkinter import filedialog
    except Exception as e:  # noqa: BLE001 - Python built without Tk
        raise PickerUnavailable(f"no file picker available ({e})") from None
    result: dict = {}

    def run():
        try:
            root = tkinter.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            root.update()
            if kind == "folder":
                result["path"] = filedialog.askdirectory(parent=root, title="Choose for your PandaDeck button")
            else:
                result["path"] = filedialog.askopenfilename(parent=root, title="Choose for your PandaDeck button")
            root.destroy()
        except Exception as e:  # noqa: BLE001 - no display, Tk init failure
            result["error"] = str(e)

    t = threading.Thread(target=run, name="picker", daemon=True)
    t.start()
    t.join(timeout)
    if "error" in result:
        raise PickerUnavailable(f"couldn't open a file picker ({result['error']})")
    path = result.get("path") or ""
    return os.path.normpath(path) if path else None
