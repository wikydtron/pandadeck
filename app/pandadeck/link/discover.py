"""Find the panel and keep a link to it: USB first, Wi-Fi as the fallback.

    auto  USB if a panel answers on a PandaDeck port, else Wi-Fi (configured
          host, else the last IP the panel reported over USB, else
          pandadeck.local); while on Wi-Fi, keep looking for USB and switch.
    usb   USB only.     wifi  Wi-Fi only.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from .base import LinkError
from .usb import UsbLink, candidate_ports, port_serial
from .wifi import WifiLink, resolve_mdns

log = logging.getLogger("link")

USB_SCAN_S = 2.0
WIFI_RETRY_S = 10.0
USB_UPGRADE_S = 10.0
FAILS_BEFORE_DROP = 3
NOT_OURS_S = 300.0  # an Espressif port that answered as something else isn't re-probed for a while
SILENT_S = 10.0     # one that didn't answer at all (another firmware, or a panel still booting)
INFO_REFRESH_S = 20.0  # re-read the panel's info (is its USB keyboard plugged into a computer?)


def link_mode(state: str, kind: str, info: dict | None) -> str:
    """Pure: how the panel is connected, for the settings page's "What works right now".

        usb        USB cable to this computer: everything, keys included
        wifi       over Wi-Fi only: no keyboard keys reach this computer
        usb_other  over Wi-Fi, and its USB keyboard is plugged into a computer (another one)
        none       not reachable (power only, or off)
    """
    if state != "connected":
        return "none"
    if kind == "usb":
        return "usb"
    return "usb_other" if (info or {}).get("usb") is True else "wifi"


class LinkManager(threading.Thread):
    def __init__(self, settings, secrets, state, *, ports: Callable[..., list[str]] = candidate_ports,
                 open_usb: Callable[[str], object] = UsbLink, make_wifi: Callable[..., object] = WifiLink,
                 resolve: Callable[[], str | None] = resolve_mdns, serial_of: Callable[[str], str] = port_serial,
                 clock: Callable[[], float] = time.monotonic, enabled: bool = True, demo: bool = False):
        super().__init__(name="link", daemon=True)
        self.settings, self.secrets, self.state = settings, secrets, state
        self._ports, self._open_usb, self._make_wifi = ports, open_usb, make_wifi
        self._resolve, self._serial_of, self._clock = resolve, serial_of, clock
        self.enabled, self.demo = enabled, demo
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._halt = threading.Event()
        self.link = None
        self.info: dict = {}
        self.error = ""
        self.since = 0.0
        self._fails = 0
        self._next_wifi = 0.0
        self._next_usb_upgrade = 0.0
        self.busy = ""  # "update" while flashing: the sync loop stays away
        self._not_ours: dict[str, float] = {}
        self._usb_paused_until = 0.0
        self._next_info = 0.0
        self.on_connect: list[Callable[[object, dict], None]] = []

    # ---- public ------------------------------------------------------------------
    def status(self) -> dict:
        with self._lock:
            link = self.link
            if not self.enabled:
                return {"state": "off", "kind": "", "where": "", "error": "", "info": {}}
            state, kind = ("connected" if link else "searching"), getattr(link, "kind", "")
            return {
                "state": state, "kind": kind, "where": getattr(link, "where", ""),
                "mode": link_mode(state, kind, self.info),
                "error": self.error, "since": self.since, "info": dict(self.info), "busy": self.busy,
                "usb_paused": max(0, int(self._usb_paused_until - self._clock())),
            }

    def current(self):
        with self._lock:
            return self.link

    def failed(self, err: Exception) -> None:
        """The sync loop hit a link error. USB disconnects drop at once; other
        errors (panel busy) only after a few in a row."""
        with self._lock:
            self._fails += 1
            self.error = str(err)
            dead = getattr(self.link, "dead", False)
            if dead or self._fails >= FAILS_BEFORE_DROP:
                self._drop_locked(f"link lost: {err}")

    def succeeded(self) -> None:
        with self._lock:
            self._fails = 0
            self.error = ""

    def reconnect(self) -> None:
        """Settings changed (link mode / host): rediscover now."""
        with self._lock:
            self._drop_locked("settings changed")
            self._next_wifi = 0.0
        self._wake.set()

    def pause_usb(self, seconds: float) -> None:
        """Keep off the serial ports for a while (e.g. while esptool flashes the board)."""
        self._usb_paused_until = self._clock() + seconds

    def resume_usb(self) -> None:
        self._usb_paused_until = 0.0
        self._wake.set()

    def stop(self) -> None:
        self._halt.set()
        self._wake.set()
        with self._lock:
            self._drop_locked("stopping")

    # ---- loop ----------------------------------------------------------------------
    def run(self) -> None:
        while not self._halt.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001
                log.exception("link manager tick failed")
            self._wake.wait(1.0 if self.current() else USB_SCAN_S)
            self._wake.clear()

    def tick(self) -> None:
        if not self.enabled or self.busy:
            return
        link = self.current()
        mode = (self.settings.section("panel") or {}).get("link", "auto")
        if link is None:
            new = self.connect_once(mode)
            if new is not None:
                self._adopt(*new)
            return
        if self._clock() >= self._next_info:
            self._next_info = self._clock() + INFO_REFRESH_S
            self._refresh_info(link)
        if link.kind == "wifi" and mode == "auto" and self._clock() >= self._next_usb_upgrade:
            self._next_usb_upgrade = self._clock() + USB_UPGRADE_S
            found = self._try_usb()
            if found is not None:
                log.info("USB panel found: switching from Wi-Fi")
                with self._lock:
                    self._drop_locked("switching to USB")
                self._adopt(*found)

    def connect_once(self, mode: str):
        if self.demo:
            from .demo import DemoLink
            d = DemoLink()
            return d, d.hello()
        if mode in ("auto", "usb"):
            found = self._try_usb()
            if found is not None:
                return found
        if mode in ("auto", "wifi") and self._clock() >= self._next_wifi:
            self._next_wifi = self._clock() + WIFI_RETRY_S
            return self._try_wifi()
        return None

    def _refresh_info(self, link) -> None:
        """Keep the panel's info current (its USB keyboard can be plugged in or out any time)."""
        try:
            info = link.hello()
        except Exception as e:  # noqa: BLE001 - the sync loop notices real link trouble
            log.debug("info refresh failed: %s", e)
            return
        with self._lock:
            if self.link is link and isinstance(info, dict):
                self.info = {k: v for k, v in info.items() if k != "token"}

    # ---- attempts ----------------------------------------------------------------------
    def _try_usb(self):
        if self._clock() < self._usb_paused_until:
            return None
        try:
            ports = self._ports(preferred_serial=self.state.get("usb_serial", ""))
        except Exception as e:  # noqa: BLE001 - pyserial missing, permission problems...
            self.error = f"can't list USB ports: {e}"
            return None
        now = self._clock()
        for port in ports:
            if now < self._not_ours.get(port, 0):
                continue
            try:
                link = self._open_usb(port)
            except Exception as e:  # noqa: BLE001 - busy (another program), permissions
                log.debug("can't open %s: %s", port, e)
                self.error = f"{port} is in use by another program or not accessible"
                continue
            try:
                info = link.hello()
            except LinkError as e:
                log.debug("%s didn't answer: %s", port, e)
                link.close()
                self._not_ours[port] = now + SILENT_S
                continue
            if info.get("app") == "pandadeck":
                return link, info
            log.debug("%s answered as %r, not a PandaDeck", port, info.get("app"))
            link.close()
            self._not_ours[port] = now + NOT_OURS_S
        return None

    def _try_wifi(self):
        token = self.secrets.get("panel_token") or ""
        configured = (self.settings.section("panel") or {}).get("host", "")
        host = configured or self.state.get("last_ip", "")
        if not token:
            if host:
                self.error = "Wi-Fi needs the panel's token: plug the panel in by USB once"
            return None
        if not host:
            host = self._resolve() or ""
        if not host:
            return None
        try:
            # Automatic addresses (last known IP, mDNS) must prove they're the panel
            # before the token is sent; a typed address is the user's call.
            link = self._make_wifi(host, token, require_proof=not configured)
            info = link.hello()
            return link, info
        except (LinkError, OSError) as e:
            self.error = str(e)
            return None

    def _adopt(self, link, info: dict) -> None:
        with self._lock:
            # The token is a secret: it goes to secrets.json, never into status.
            self.link, self.info = link, {k: v for k, v in (info or {}).items() if k != "token"}
            self.since, self._fails, self.error = time.time(), 0, ""
            self._next_info = self._clock() + INFO_REFRESH_S
        token = (info or {}).get("token")
        if token and token != self.secrets.get("panel_token"):
            self.secrets.set("panel_token", token)  # handed over USB, used for Wi-Fi
        ip = ((info or {}).get("wifi") or {}).get("ip")
        if ip and link.kind == "usb":
            self.state.set("last_ip", ip)
        if link.kind == "usb" and link.where != "demo":
            try:
                self.state.set("usb_serial", self._serial_of(link.where))
            except Exception:  # noqa: BLE001
                pass
        log.info("panel connected over %s (%s), firmware %s", link.kind, link.where, (info or {}).get("fw"))
        for fn in list(self.on_connect):
            try:
                fn(link, dict(self.info))
            except Exception:  # noqa: BLE001
                log.exception("on_connect callback failed")

    def update_info(self, **kw) -> None:
        with self._lock:
            self.info.update(kw)

    def _drop_locked(self, why: str) -> None:
        if self.link is not None:
            log.info("panel link closed (%s)", why)
            try:
                self.link.close()
            except Exception:  # noqa: BLE001
                pass
        self.link = None
        self.info = {}
        self._fails = 0
