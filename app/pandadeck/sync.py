"""The sync loop (link-agnostic).

Every 0.5 s (0.25 s while an approval is pending, at once when an agent's
state changes) the app sends the panel a snapshot and gets back the events
the user tapped. Events carry (boot, seq) and are re-sent until acknowledged,
so they are de-duplicated here and the highest handled seq is echoed back as
"ack". Images (app icons, cover, backdrop) are uploaded by a separate worker
whenever the panel lacks them: after every panel boot, or when they change.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from typing import Callable

from .link.base import LinkError, Unsupported

log = logging.getLogger("sync")

SYNC_S = 0.5
SYNC_BUSY_S = 0.25
DECK_EVERY = 20          # resend the deck every N syncs (cheap insurance)
PANEL_ALIVE_S = 5.0


class ImageUploader(threading.Thread):
    """Keeps the panel's images in step: name -> (header, pixels). An image is
    sent once per (panel boot, content); art goes first (it's on screen)."""

    def __init__(self, link_of: Callable[[], object], images: Callable[[], dict],
                 busy: Callable[[], bool] = lambda: False):
        super().__init__(name="images", daemon=True)
        self._link_of, self._images, self._busy = link_of, images, busy
        self._lock = threading.Lock()
        self._kick = threading.Event()
        self._halt = threading.Event()
        self._sent: dict[str, tuple[int, str]] = {}
        self._boot = -1
        self._backoff_until = 0.0
        self.uploaded = 0

    def offer(self, boot: int, panel_icons: int | None = None) -> None:
        with self._lock:
            if boot != self._boot:
                self._boot, self._sent = boot, {}
            elif panel_icons is not None:
                icons_sent = sum(1 for n in self._sent if n == "art" or n.startswith("icon:"))
                if panel_icons < icons_sent:  # the panel lost some (store full / reset)
                    self._sent = {n: v for n, v in self._sent.items() if not (n == "art" or n.startswith("icon:"))}
        self._kick.set()

    def reset(self) -> None:
        with self._lock:
            self._boot, self._sent = -1, {}

    def pending(self) -> list[tuple[str, dict, bytes, str]]:
        if self._boot < 0:
            return []  # the panel's boot isn't known yet
        want = self._images()
        out = []
        with self._lock:
            for name, (hdr, data) in want.items():
                # The header counts too: the cover's pixels stay the same when only the
                # backdrop changes, but its tag doesn't -- and the panel matches on the tag.
                digest = hashlib.sha1(json.dumps(hdr, sort_keys=True).encode() + data).hexdigest()[:12]
                if self._sent.get(name) != (self._boot, digest):
                    out.append((name, hdr, data, digest))
        out.sort(key=lambda x: (x[0] not in ("art", "backdrop"), x[0]))
        return out

    def run(self) -> None:
        while not self._halt.is_set():
            self._kick.wait(2.0)
            self._kick.clear()
            try:
                self.upload_pending()
            except Exception:  # noqa: BLE001 - a bad image must never stop the uploader for good
                log.exception("image upload round failed")
                self._backoff_until = time.time() + 30

    def upload_pending(self) -> None:
        if time.time() < self._backoff_until or self._boot < 0:
            return
        with self._lock:
            boot = self._boot  # what we send is for this panel boot, even if it reboots meanwhile
        for name, hdr, data, digest in self.pending():
            link = self._link_of()
            if link is None or self._halt.is_set() or self._busy():
                return  # never between firmware-update chunks
            try:
                ok = link.image(hdr, data)
            except Unsupported:
                ok = False
            except LinkError as e:
                log.debug("image %s upload failed: %s", name, e)
                self._backoff_until = time.time() + 5
                return
            if not ok:
                log.info("panel refused image %s; retrying in 30 s", name)
                self._backoff_until = time.time() + 30
                return
            with self._lock:
                if self._boot == boot:
                    self._sent[name] = (boot, digest)
            self.uploaded += 1

    def stop(self) -> None:
        self._halt.set()
        self._kick.set()


class Syncer(threading.Thread):
    def __init__(self, links, bridge, actions, *, sources: dict[str, Callable[[], dict | None]] | None = None,
                 deck: Callable[[], dict | None] = lambda: None, images: Callable[[], dict] = lambda: {},
                 clock: Callable[[], float] = time.time):
        super().__init__(name="sync", daemon=True)
        self.links, self.bridge, self.actions = links, bridge, actions
        self.sources = sources or {}
        self.deck = deck
        self._clock = clock
        self.uploader = ImageUploader(links.current, images, busy=lambda: bool(links.busy))
        self._halt = threading.Event()
        self._n = 0
        self._boot = 0
        self._acked = 0
        self._deck_sent: tuple = ()
        self._last_ok_at = 0.0
        self.last_reply: dict = {}

    # ---- snapshot ------------------------------------------------------------------
    def snapshot(self, trusted: bool = True) -> dict:
        sessions, perms = self.bridge.snapshot() if self.bridge else ([], [])
        if not trusted:
            perms = []  # approvals only travel over USB or a Wi-Fi link that proved it's the panel
        snap: dict = {"t": int(self._clock()), "sessions": sessions, "perms": perms,
                      "ack": self._acked, "boot": self._boot}
        for key, src in self.sources.items():
            try:
                d = src()
            except Exception:  # noqa: BLE001 - one broken source must not stop the panel
                log.exception("%s snapshot failed", key)
                d = None
            if d is not None:
                snap[key] = d
        deck = self.deck()
        if deck:
            sig = (self._boot, deck.get("rev"))
            if sig != self._deck_sent or self._n % DECK_EVERY == 0:
                snap["deck"] = deck
                self._deck_sent = sig
        return snap

    # ---- one round trip ---------------------------------------------------------------
    def once(self) -> bool:
        link = self.links.current()
        if link is None or self.links.busy:
            return False
        trusted = bool(getattr(link, "trusted", True))
        snap = self.snapshot(trusted)
        try:
            resp = link.sync(snap)
        except LinkError as e:
            self.links.failed(e)
            self._deck_sent = ()
            return False
        finally:
            self._n += 1
        if not isinstance(resp, dict):
            self.links.failed(LinkError("bad sync reply"))
            return False
        self.links.succeeded()
        self._last_ok_at = self._clock()
        self.last_reply = {k: resp.get(k) for k in ("fw", "boot", "icons")}
        boot = int(resp.get("boot") or 0)
        self.handle_events(boot, resp.get("events") or [], trusted)
        if "deck" in snap:  # it reached the panel instance that just answered
            self._deck_sent = (self._boot, snap["deck"].get("rev"))
        self.uploader.offer(boot, resp.get("icons") if isinstance(resp.get("icons"), int) else None)
        return True

    def handle_events(self, boot: int, events: list, trusted: bool = True) -> None:
        if boot != self._boot:  # panel rebooted: its seq restarts at 1
            self._boot, self._acked = boot, 0
        for ev in events:
            if not isinstance(ev, dict):
                continue
            try:
                seq = int(ev.get("seq") or 0)
            except (TypeError, ValueError):
                continue
            if seq <= self._acked:
                continue  # already handled (re-sent until our ack lands)
            self._acked = seq
            if ev.get("k") == "perm" and not trusted:
                log.warning("ignoring an approval from an unverified Wi-Fi panel at %s",
                            getattr(self.links.current(), "where", "?"))
                continue
            try:
                self.actions.run(ev)
            except Exception:  # noqa: BLE001
                log.exception("action failed: %r", ev)

    def panel_alive(self) -> bool:
        return self._clock() - self._last_ok_at < PANEL_ALIVE_S

    def approvals_possible(self) -> bool:
        """A permission request may wait for the panel only if the panel is
        reachable over a link that can carry approvals."""
        link = self.links.current()
        return self.panel_alive() and link is not None and bool(getattr(link, "trusted", True))

    def new_link(self, *_args) -> None:
        """A (re)connected panel: resend everything."""
        self._deck_sent = ()
        self.uploader.reset()

    # ---- loop ------------------------------------------------------------------------------
    def run(self) -> None:
        self.uploader.start()
        changed = self.bridge.changed if self.bridge else threading.Event()
        while not self._halt.is_set():
            t0 = time.monotonic()
            try:
                self.once()
            except Exception:  # noqa: BLE001 - the loop must survive anything
                log.exception("sync iteration failed")
            busy = self.bridge.has_pending_perms() if self.bridge else False
            interval = SYNC_BUSY_S if busy else SYNC_S
            # Wake early when an agent's state changes so the panel reacts instantly.
            changed.wait(max(0.05, interval - (time.monotonic() - t0)))
            changed.clear()

    def stop(self) -> None:
        self._halt.set()
        self.uploader.stop()
        if self.bridge:
            self.bridge.changed.set()
