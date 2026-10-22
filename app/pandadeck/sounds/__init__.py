"""SOUNDS: a soundboard on the panel's DECK page.

Tapping a sound button sends agent:sound:<id>; this computer plays the sound
(agent:sound:stop stops them all). The built-in sounds come from synth.py
(generated, no recordings shipped); settings deck.sounds holds what the user
changed per sound (name, symbol, colour, picture, own file, volume) and the
user's own extra sounds, which any custom page button can play.

    sounds.library()            {id: (label, icon)} of the built-in sounds
    sounds.sounds_page(deck)    the SOUNDS page as a custom-page dict (deck/board.py)
    Sounds(settings).play(id) / .stop_all() / .preview(...)
"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .player import Player
from .store import SoundError, SoundStore  # noqa: F401 - re-exported for the web API

log = logging.getLogger("sounds")

PAGE_ID = "sounds"
PAGE_NAME = "SOUNDS"
PAGE_COLOR = "#F472B6"
DEFAULT_VOLUME = 80


def library() -> dict[str, tuple[str, str]]:
    """The built-in sounds, in page order: {id: (label, icon)} ({} if the generator is missing)."""
    try:
        from . import synth
    except ImportError as e:  # pragma: no cover - only while synth.py is being written
        log.warning("built-in sounds unavailable: %s", e)
        return {}
    return dict(synth.LIBRARY)


def entry(deck_cfg: dict, sound_id: str, lib: dict | None = None) -> dict | None:
    """Pure: one sound with its defaults filled in, or None if there's no such sound."""
    lib = library() if lib is None else lib
    own = (deck_cfg.get("sounds") or {}).get(sound_id)
    if sound_id not in lib and not (own and own.get("file")):
        return None
    label, icon = lib.get(sound_id, ("", ""))
    own = own or {}
    return {"id": sound_id, "builtin": sound_id in lib,
            "label": own.get("label") or label or sound_id, "icon": own.get("icon") or icon or "audio",
            "color": own.get("color", ""), "image": own.get("image", ""),
            "file": own.get("file", ""), "volume": own.get("volume", DEFAULT_VOLUME)}


def sounds_page(deck_cfg: dict, lib: dict | None = None) -> dict:
    """Pure: the built-in SOUNDS page (4 x 3) as a custom-page dict, with the user's changes."""
    lib = library() if lib is None else lib
    buttons = []
    for sid in list(lib)[:12]:
        e = entry(deck_cfg, sid, lib)
        buttons.append({"label": e["label"], "icon": e["icon"], "color": e["color"], "image": e["image"],
                        "action": f"agent:sound:{sid}"})
    return {"id": PAGE_ID, "name": PAGE_NAME, "color": PAGE_COLOR, "layout": "keys", "buttons": buttons}


class Sounds:
    """What the panel's sound buttons and the settings page's preview call."""

    def __init__(self, settings, store: SoundStore | None = None, player: Player | None = None,
                 lib: dict | None = None):
        self.settings = settings
        self.store = store or SoundStore()
        self.player = player or Player()
        self._lib = lib
        self._jobs = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sounds")  # taps keep their order

    def library(self) -> dict:
        if self._lib is None:
            self._lib = library()
        return self._lib

    def warm_up(self) -> None:
        """Write the built-in sounds' files in the background (first run)."""
        def run():
            n = self.store.ensure_defaults(list(self.library()))
            if n:
                log.info("wrote %d built-in sounds to %s", n, self.store.folder)
        threading.Thread(target=run, name="sounds-warmup", daemon=True).start()

    def resolve(self, sound_id: str) -> tuple[Path | None, int]:
        e = entry(self.settings.section("deck") or {}, sound_id, self.library())
        if e is None:
            return None, DEFAULT_VOLUME
        return self.store.path_for(sound_id, e["file"], builtin=e["builtin"]), e["volume"]

    def play(self, sound_id: str) -> None:
        """agent:sound:<id>: one sound at a time; another tap cuts it, a second tap on it stops it
        (off the caller's thread: a first play may render the file)."""
        self._jobs.submit(self._play, sound_id)

    def _play(self, sound_id: str) -> None:
        path, volume = self.resolve(sound_id)
        if path is None:
            log.info("no sound %r", sound_id)
            return
        self.player.play(path, volume, key=sound_id, mode="toggle")

    def preview(self, sound_id: str = "", file: str = "", volume: int | None = None, default: bool = False) -> bool:
        """The settings page's play button: a sound as saved, an upload not saved
        yet (`file`), or a built-in sound's original (`default`, before a reset is saved)."""
        if file or default:
            path = self.store.upload_path(file) if file else (
                self.store.ensure_default(sound_id) if sound_id in self.library() else None)
            vol = DEFAULT_VOLUME if volume is None else volume
        else:
            path, vol = self.resolve(sound_id)
            vol = vol if volume is None else volume
        if path is None:
            return False
        return self.player.play(path, vol, key="preview", mode="solo")

    def stop_all(self) -> None:
        self.player.stop_all()
