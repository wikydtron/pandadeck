"""Panel events -> what happens on this computer.

    {"k":"perm","id":..,"a":"allow"|"deny"}   approve/deny an AI permission request
    {"k":"cmd","a":"mic"}                     toggle the microphone mute
    {"k":"cmd","a":"settings"}                open this app's settings page (the panel's gear, agent:settings)
    {"k":"cmd","a":"settings:music"}          ... at a section (music: the Spotify sign-in, for ❤ Like)
    {"k":"cmd","a":"sound:<id>"}              play a sound on this computer (agent:sound:<id>)
    {"k":"cmd","a":"sound:stop"}              stop every sound playing
    {"k":"cmd","a":"media:<cmd>"}             seek:<s> | shuffle | repeat | playpause | next | prev
    {"k":"cmd","a":"launch:app:<key>"}        open an app from the deck
    {"k":"cmd","a":"launch:target:<id>"}      open a file, folder or website from the deck
    {"k":"cmd","a":"launch:<name>"}           built-in targets (terminal, files, spotify, ...)
    {"k":"cmd","a":"restart:<container id>"}  homelab container restart
"""
from __future__ import annotations

import logging
import threading
import time

log = logging.getLogger("actions")

SETTINGS_EVERY_S = 3.0  # a second tap on the gear (or a repeated event) doesn't open another tab


class Actions:
    def __init__(self, bridge=None, audio=None, media=None, board=None, lab=None, open_settings=None,
                 sounds=None, clock=time.monotonic):
        self.bridge, self.audio, self.media, self.board, self.lab = bridge, audio, media, board, lab
        self.sounds = sounds
        self.open_settings = open_settings
        self.handlers: dict[str, object] = {}  # prefix -> callable(arg) (extensions register here)
        self._clock = clock
        self._settings_at = float("-inf")

    def run(self, ev: dict) -> None:
        kind, arg, eid = ev.get("k"), str(ev.get("a") or ""), str(ev.get("id") or "")
        if kind == "perm":
            ok = self.bridge.decide(eid, arg) if self.bridge else False
            log.info("panel %s perm %s -> %s", arg, eid, "applied" if ok else "stale")
            return
        if kind != "cmd":
            log.debug("ignoring panel event %r", kind)
            return
        log.info("panel cmd %s", arg)
        head, _, rest = arg.partition(":")
        if arg == "mic":
            if self.audio is not None:
                self.audio.toggle_mic()
        elif arg == "noop":
            pass
        elif head == "settings":
            self._settings(rest)
        elif head == "media" and self.media is not None:
            self.media.command(rest)
        elif head == "launch" and self.board is not None:
            if rest.startswith("app:"):
                self.board.launch(rest[4:])
            elif rest.startswith("target:"):
                self.board.launch_target(rest[7:])
            else:
                self.board.launch_named(rest)
        elif head == "sound" and self.sounds is not None:
            if rest == "stop":
                self.sounds.stop_all()
            elif rest:
                self.sounds.play(rest)
        elif head == "restart" and self.lab is not None:
            threading.Thread(target=self.lab.restart, args=(rest,), daemon=True).start()
        elif head in self.handlers:
            self.handlers[head](rest)
        else:
            log.warning("unknown panel cmd %r", arg)

    def _settings(self, section: str = "") -> None:
        now = self._clock()
        if self.open_settings is None or now - self._settings_at < SETTINGS_EVERY_S:
            return
        self._settings_at = now
        args = (section,) if section else ()
        threading.Thread(target=self.open_settings, args=args, name="open-settings", daemon=True).start()
