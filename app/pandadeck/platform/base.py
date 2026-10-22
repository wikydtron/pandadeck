"""OS abstraction for the PandaDeck app (see docs/app.md).

Every platform module must import cleanly on every OS: OS-only modules
(winrt, ctypes.windll, jeepney, ...) are imported lazily inside methods.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:  # pragma: no cover
    from PIL import Image


@dataclass
class AppInfo:
    id: str            # stable id: AUMID (Windows), bundle path (macOS), .desktop id (Linux)
    name: str
    score: float = 0.0  # how much the user uses it, for "suggested apps" (0 = unknown)


@dataclass
class NowPlaying:
    app: str
    title: str
    artist: str = ""
    album: str = ""
    playing: bool = False
    pos: float = 0.0     # seconds, already advanced to "now" while playing
    dur: float = 0.0     # 0 = unknown
    shuffle: int = -1    # -1 = not reported, 0 off, 1 on
    repeat: int = -1     # -1 = not reported, 0 off, 1 track, 2 list
    art: "Image.Image | None" = None          # cover, any size
    art_key: str = ""                         # changes whenever the cover changes
    artist_image: "Image.Image | None" = None  # Spotify Web API only
    extra: dict = field(default_factory=dict)


class MediaBackend(Protocol):
    def now_playing(self) -> NowPlaying | None: ...
    def command(self, cmd: str) -> None: ...   # playpause | next | prev | seek:<s> | shuffle | repeat


class AudioBackend(Protocol):
    def volume(self) -> int: ...               # 0..100, -1 unknown
    def set_volume(self, level: int) -> None: ...   # 0..100: the default output
    def mic_muted(self) -> bool | None: ...    # None = unknown / no mic
    def toggle_mic(self) -> None: ...


class Platform(Protocol):
    name: str  # "windows" | "macos" | "linux"

    def list_apps(self) -> list[AppInfo]: ...
    def app_icon(self, app_id: str) -> "Image.Image | None": ...   # RGBA, any size
    # RGBA icon (or thumbnail) of a file or folder path, any size; None if the path
    # doesn't exist or nothing fits. Never raises. Added for "open file/folder" buttons.
    def file_icon(self, path: str) -> "Image.Image | None": ...
    def launch_app(self, app_id: str) -> None: ...
    def media(self) -> MediaBackend | None: ...
    def audio(self) -> AudioBackend | None: ...
    def set_autostart(self, enabled: bool, argv: list[str]) -> None: ...
    def autostart_enabled(self) -> bool: ...
