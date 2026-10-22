"""Core Audio on Windows: speaker volume + default-mic mute (via pycaw's COM
interfaces).

COM rules that matter here (a violation crashes the process with 0xC0000374):
  * One dedicated thread owns every COM object and joins the *multi-threaded*
    apartment, so if the GC releases a pointer from another thread it's legal.
  * Endpoint interfaces are created once and reused; they're rebuilt only after
    an error or every REBUILD_EVERY_S (to follow a changed default device),
    never churned per poll.
Readers get cached values; the mic toggle is queued to the COM thread.
"""
from __future__ import annotations

import importlib.util
import logging
import queue
import sys
import threading

log = logging.getLogger("pandadeck.platform.win")

POLL_S = 1.0
REBUILD_EVERY_S = 30
COINIT_MULTITHREADED = 0x0
CLSID_MM_DEVICE_ENUMERATOR = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"


def pycaw_available() -> bool:
    return importlib.util.find_spec("pycaw") is not None and importlib.util.find_spec("comtypes") is not None


def _import_com():
    """Import comtypes + pycaw on the calling (audio) thread, in the MTA.

    comtypes CoInitializes the thread that first imports it (STA by default),
    so if it isn't loaded yet, ask it for the MTA via sys.coinit_flags."""
    if "comtypes" not in sys.modules:
        missing = object()
        prev = getattr(sys, "coinit_flags", missing)
        sys.coinit_flags = COINIT_MULTITHREADED  # type: ignore[attr-defined]
        try:
            import comtypes  # noqa: F401
        finally:
            if prev is missing:
                del sys.coinit_flags  # type: ignore[attr-defined]
            else:
                sys.coinit_flags = prev  # type: ignore[attr-defined]
    import comtypes

    comtypes.CoInitializeEx(COINIT_MULTITHREADED)  # S_FALSE if the import already did it
    from pycaw.pycaw import EDataFlow, ERole, IAudioEndpointVolume, IMMDeviceEnumerator

    return comtypes, EDataFlow, ERole, IAudioEndpointVolume, IMMDeviceEnumerator


class WindowsAudio:
    """AudioBackend: volume() / mic_muted() are cached, refreshed every POLL_S."""

    def __init__(self):
        self._q: queue.Queue[str] = queue.Queue()
        self._volume = -1
        self._mic_muted: bool | None = None
        threading.Thread(target=self._run, name="pandadeck-audio", daemon=True).start()

    @classmethod
    def create(cls) -> "WindowsAudio | None":
        return cls() if pycaw_available() else None

    # ---- public (any thread) -------------------------------------------------------------
    def volume(self) -> int:
        return self._volume

    def mic_muted(self) -> bool | None:
        return self._mic_muted

    def toggle_mic(self) -> None:
        self._q.put("toggle_mic")

    def set_volume(self, level: int) -> None:
        self._q.put(f"volume:{int(level)}")

    # ---- COM thread ------------------------------------------------------------------------
    def _endpoints(self, com):
        comtypes, EDataFlow, ERole, IAudioEndpointVolume, IMMDeviceEnumerator = com

        enum = comtypes.CoCreateInstance(comtypes.GUID(CLSID_MM_DEVICE_ENUMERATOR), IMMDeviceEnumerator,
                                         comtypes.CLSCTX_INPROC_SERVER)

        def ep(flow, role):
            try:
                dev = enum.GetDefaultAudioEndpoint(flow, role)
                # QueryInterface, not ctypes.cast: cast() makes a second pointer without
                # an AddRef, so when the temporary from Activate() is collected the
                # endpoint is released under us -> access violations / heap corruption.
                return dev.Activate(IAudioEndpointVolume._iid_, comtypes.CLSCTX_ALL, None).QueryInterface(
                    IAudioEndpointVolume)
            except Exception:  # noqa: BLE001 - no such device / can't open it: just that one is missing
                return None

        capture, render = EDataFlow.eCapture.value, EDataFlow.eRender.value
        return (ep(capture, ERole.eConsole.value), ep(capture, ERole.eCommunications.value),
                ep(render, ERole.eConsole.value))

    @staticmethod
    def _toggle(mic, mic_comms) -> None:
        new = 0 if mic.GetMute() else 1
        mic.SetMute(new, None)
        # Teams/Discord use the *communications* device; keep it in step.
        if mic_comms is not None:
            try:
                mic_comms.SetMute(new, None)
            except Exception:  # noqa: BLE001
                pass
        log.info("mic %s", "muted" if new else "unmuted")

    def _run(self) -> None:
        try:
            com = _import_com()
        except Exception as e:  # noqa: BLE001 - pycaw/comtypes missing or broken
            log.info("Core Audio unavailable: %s", e)
            return
        eps = None
        age = 0.0
        cmd: str | None = None
        while True:
            if cmd == "toggle_mic":
                # Fresh endpoints, so the toggle hits the *current* default mic; one retry.
                for attempt in (1, 2):
                    try:
                        eps, age = self._endpoints(com), 0.0
                        if eps[0] is not None:
                            self._toggle(eps[0], eps[1])
                        break
                    except Exception as e:  # noqa: BLE001 - COMError isn't an OSError
                        log.warning("mic toggle failed (attempt %d): %s", attempt, e)
                        eps = None
            if cmd and cmd.startswith("volume:"):
                try:
                    if eps is None:
                        eps, age = self._endpoints(com), 0.0
                    if eps[2] is not None:
                        eps[2].SetMasterVolumeLevelScalar(int(cmd[7:]) / 100.0, None)
                except Exception as e:  # noqa: BLE001 - COMError isn't an OSError
                    log.warning("set volume failed: %s", e)
                    eps = None
            try:
                if eps is None or age >= REBUILD_EVERY_S:
                    eps, age = self._endpoints(com), 0.0
                mic, _, spk = eps
                self._mic_muted = bool(mic.GetMute()) if mic is not None else None
                self._volume = int(round(spk.GetMasterVolumeLevelScalar() * 100)) if spk is not None else -1
                age += POLL_S
            except Exception as e:  # noqa: BLE001
                log.debug("audio poll failed (%s); rebuilding endpoints", e)
                eps = None
            try:
                cmd = self._q.get(timeout=POLL_S)
            except queue.Empty:
                cmd = None
