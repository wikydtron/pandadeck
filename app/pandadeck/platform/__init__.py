"""Pick the Platform implementation for this OS."""
from __future__ import annotations

import sys

from .base import AppInfo, AudioBackend, MediaBackend, NowPlaying, Platform  # noqa: F401


def current() -> "Platform":
    if sys.platform == "win32":
        from .windows import WindowsPlatform
        return WindowsPlatform()
    if sys.platform == "darwin":
        from .macos import MacPlatform
        return MacPlatform()
    from .linux import LinuxPlatform
    return LinuxPlatform()
