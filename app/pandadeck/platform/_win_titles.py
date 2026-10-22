"""Titles of the visible top-level windows on Windows (EnumWindows via ctypes).

Used to tell which web player a browser's media session is (the active tab's
title is in the browser's window title). No COM, no message sent to other
processes: GetWindowTextW returns the caption Windows already has. Imports on
any OS; calling it elsewhere returns [].
"""
from __future__ import annotations

import sys

MAX_WINDOWS = 512
MAX_TITLE = 1024


def top_level_titles() -> list[str]:
    if sys.platform != "win32":
        return []
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)  # private instance: our argtypes stay ours
    proc_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [proc_type, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int

    titles: list[str] = []

    def visit(hwnd, _lparam):
        if user32.IsWindowVisible(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            if 0 < n < MAX_TITLE:
                buf = ctypes.create_unicode_buffer(n + 1)
                if user32.GetWindowTextW(hwnd, buf, n + 1) and buf.value:
                    titles.append(buf.value)
        return len(titles) < MAX_WINDOWS

    callback = proc_type(visit)  # keep a reference for the duration of the call
    user32.EnumWindows(callback, 0)
    return titles
