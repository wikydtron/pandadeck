"""Sound files in <config dir>/sounds/.

    <id>.wav           a built-in sound, written from synth.render(<id>) the first
                       time it's needed (never overwritten: replace the file with
                       your own and it stays yours)
    <sha1>.<ext>       an upload from the settings page (wav, mp3 or ogg)

Uploads are checked by their header, not their name, and stored under the
hash of their bytes, so a name can never point outside this folder.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
from pathlib import Path
from typing import Callable

from .. import paths

log = logging.getLogger("sounds")

MAX_BYTES = 5 * 1024 * 1024
KINDS = ("wav", "mp3", "ogg", "m4a")
UPLOAD_NAME = re.compile(r"[0-9a-f]{40}\.(?:wav|mp3|ogg|m4a)")
SOUND_ID = re.compile(r"[a-z][a-z0-9_]{0,23}")  # same as deck/grammar.py


class SoundError(ValueError):
    """An upload we refuse; the message is shown to the user."""


def sniff(data: bytes) -> str | None:
    """Pure: "wav" | "mp3" | "ogg" | "m4a" from the first bytes, or None."""
    head = bytes(data[:12])
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "wav"
    if head[:4] == b"OggS":
        return "ogg"
    if len(head) >= 8 and head[4:8] == b"ftyp":  # MPEG-4 audio (AAC .m4a)
        return "m4a"
    if head[:3] == b"ID3":
        return "mp3"
    if len(head) >= 2 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0 and head[1] & 0x06:  # MPEG audio frame sync, layer set
        return "mp3"
    return None


def sounds_dir() -> Path:
    return paths.config_dir() / "sounds"


class SoundStore:
    def __init__(self, folder: Path | None = None, render: Callable[[str], bytes] | None = None):
        self.folder = Path(folder) if folder else sounds_dir()
        self._render = render
        self._lock = threading.Lock()

    def _renderer(self) -> Callable[[str], bytes]:
        if self._render is None:
            from . import synth  # pure-Python generator, imported on first use
            self._render = synth.render
        return self._render

    # ---- built-in sounds ----------------------------------------------------------------
    def default_path(self, sound_id: str) -> Path:
        return self.folder / f"{sound_id}.wav"

    def ensure_default(self, sound_id: str) -> Path | None:
        """The built-in sound's file, written now if it's missing (never replaced)."""
        if not isinstance(sound_id, str) or not SOUND_ID.fullmatch(sound_id):
            return None
        path = self.default_path(sound_id)
        if path.exists():
            return path
        with self._lock:
            if path.exists():
                return path
            try:
                data = self._renderer()(sound_id)
            except Exception as e:  # noqa: BLE001 - unknown id / generator trouble
                log.warning("couldn't make the %s sound: %s", sound_id, e)
                return None
            self._write_new(path, data)
        return path if path.exists() else None

    def ensure_defaults(self, ids) -> int:
        """Write every missing built-in sound; returns how many were written."""
        n = 0
        for sid in ids:
            existed = self.default_path(sid).exists()
            if self.ensure_default(sid) is not None and not existed:
                n += 1
        return n

    # ---- uploads ---------------------------------------------------------------------------
    def save_upload(self, data: bytes, filename: str = "") -> str:
        """Checked upload -> its stored name "<sha1>.<ext>"."""
        if not data:
            raise SoundError("That file is empty.")
        if len(data) > MAX_BYTES:
            raise SoundError("Sounds can be at most 5 MB.")
        kind = sniff(data)
        if kind is None:
            raise SoundError("That isn't a WAV, MP3, OGG or M4A sound.")
        ext = (filename.rsplit(".", 1)[-1].lower() if "." in filename else "")
        if ext and ext not in KINDS:
            raise SoundError("Sounds are WAV, MP3, OGG or M4A files.")
        name = f"{hashlib.sha1(data).hexdigest()}.{kind}"  # noqa: S324 - a content address, not security
        path = self.folder / name
        if not path.exists():
            self._write_new(path, data)
        return name

    def upload_path(self, name: str) -> Path | None:
        if not isinstance(name, str) or not UPLOAD_NAME.fullmatch(name):
            return None
        path = self.folder / name
        return path if path.is_file() else None

    def path_for(self, sound_id: str, file: str = "", builtin: bool = True) -> Path | None:
        """What to play for a sound: the user's file when set and present, else the
        built-in one (made on demand)."""
        if file:
            p = self.upload_path(file)
            if p is not None:
                return p
            log.info("sound %s: %s is gone, using the default", sound_id, file)
        return self.ensure_default(sound_id) if builtin else None

    # ---- helpers ---------------------------------------------------------------------------
    def _write_new(self, path: Path, data: bytes) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_bytes(data)
        try:
            if path.exists():  # someone (or the user) put a file there meanwhile: keep theirs
                return
            os.replace(tmp, path)
        finally:
            if tmp.exists():
                tmp.unlink()
