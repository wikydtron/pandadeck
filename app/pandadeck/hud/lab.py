"""Optional homelab for the HUD page: a `pandadeck-lab` server
(app/lab_server/pandadeck_lab.py) on the user's server (Linux, macOS or Windows).

    GET  <url>/api/lab                 -> the `lab` block (already condensed)
    POST <url>/api/restart/<container> -> restart one container (hold-to-restart)

Both need `Authorization: Bearer <lab token>`.
"""
from __future__ import annotations

import logging
import re

from ..deck.grammar import ascii_label
from ..util import Loop, http_json

log = logging.getLogger("lab")

CONTAINER_ID = re.compile(r"^[0-9a-f]{12,64}$")
NUM_KEYS = ("cpu", "ram", "temp", "gpu", "gpu_t", "disk", "backup_h")
INT_KEYS = ("run", "total", "healthy")
ITEM_KEYS = ("id", "name", "state", "health")
ITEM_LIMITS = {"id": 12, "name": 31, "state": 15, "health": 15}


def _num(v) -> float:
    try:
        return round(float(v), 1)
    except (TypeError, ValueError):
        return -1


def clean_lab(data: dict, name: str = "") -> dict:
    """Pure: whatever the lab server sent -> exactly the block the firmware parses."""
    if not isinstance(data, dict):
        raise ValueError("lab server sent no object")
    out = {"ok": True, "name": ascii_label(name or data.get("name") or "Homelab", 31),
           "cpu_model": ascii_label(str(data.get("cpu_model") or ""), 23),
           "gpu_model": ascii_label(str(data.get("gpu_model") or ""), 23)}
    for k in NUM_KEYS:
        out[k] = _num(data.get(k, -1))
    for k in INT_KEYS:
        out[k] = int(_num(data.get(k, -1)))

    def items(lst):
        res = []
        for it in (lst or [])[:6]:
            if not isinstance(it, dict):
                continue
            row = {k: ascii_label(str(it.get(k) or ""), ITEM_LIMITS[k]) for k in ITEM_KEYS}
            row["cpu"], row["mem"] = _num(it.get("cpu")), _num(it.get("mem"))
            res.append(row)
        return res

    out["bad"], out["top"] = items(data.get("bad")), items(data.get("top"))
    return out


class LabCollector(Loop):
    def __init__(self, settings, secrets, get=http_json):
        super().__init__("lab", 3.0)
        self.settings, self.secrets, self._get = settings, secrets, get
        self._data: dict | None = None
        self.status = {"state": "off", "error": ""}

    def _url(self) -> str:
        return ((self.settings.section("hud") or {}).get("lab_url") or "").rstrip("/")

    def _headers(self) -> dict:
        tok = self.secrets.get("lab_token") or ""
        return {"Authorization": f"Bearer {tok}"} if tok else {}

    def step(self) -> None:
        url = self._url()
        if not url:
            self._data, self.status = None, {"state": "off", "error": ""}
            return
        try:
            status, data = self._get(f"{url}/api/lab", headers=self._headers(), timeout=6, follow_redirects=False)
            if status == 401:
                raise PermissionError("the homelab server rejected the token")
            if status != 200:
                raise ValueError(f"homelab server answered HTTP {status}")
            self._data = clean_lab(data, (self.settings.section("hud") or {}).get("lab_name", ""))
            # the server's own name, for the settings page's "Connected: <host>"
            self.status = {"state": "ok", "error": "", "host": ascii_label(str(data.get("name") or ""), 31)}
        except Exception as e:  # noqa: BLE001
            msg = str(e) or type(e).__name__
            if self.status.get("error") != msg:
                log.info("homelab: %s", msg)
            self._data = {"ok": False}
            self.status = {"state": "error", "error": msg}

    def snapshot(self) -> dict | None:
        return dict(self._data) if self._data else None

    def restart(self, container_id: str) -> bool:
        url = self._url()
        if not url or not CONTAINER_ID.fullmatch(container_id or ""):
            log.warning("refusing restart of %r", container_id)
            return False
        try:
            status, _ = self._get(f"{url}/api/restart/{container_id}", method="POST", payload={},
                                  headers=self._headers(), timeout=60, follow_redirects=False)
            ok = status == 200
            log.info("restart %s -> HTTP %s", container_id, status)
            self.poke()
            return ok
        except OSError as e:
            log.warning("restart %s failed: %s", container_id, e)
            return False
