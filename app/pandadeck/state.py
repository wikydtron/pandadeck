"""Small non-secret runtime state (last panel IP, USB serial, Spotify display
name, ...) kept in the cache folder. Losing it only costs a rediscovery."""
from __future__ import annotations

import copy
import threading
from pathlib import Path

from .util import read_json, write_json_atomic


class StateStore:
    def __init__(self, path: Path | None):
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        data = read_json(self.path, {}) if self.path else {}
        self._data: dict = data if isinstance(data, dict) else {}

    def get(self, key: str, default=None):
        with self._lock:
            return copy.deepcopy(self._data.get(key, default))

    def set(self, key: str, value) -> None:
        with self._lock:
            if self._data.get(key) == value:
                return
            if value is None:
                self._data.pop(key, None)
            else:
                self._data[key] = copy.deepcopy(value)
            if self.path:
                try:
                    write_json_atomic(self.path, self._data, private=False)
                except OSError:
                    pass
