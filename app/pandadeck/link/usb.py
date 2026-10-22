"""USB CDC link to the panel (docs/protocol.md "USB").

    frame := MAGIC(3) TYPE(1) LEN(4, little-endian u32) PAYLOAD(LEN)
    MAGIC := 0x1B 'P' 'D'

The same port carries the firmware's plain-text log lines; anything outside a
frame is log text. One request in flight at a time; the reply TYPE is the
lowercase of the request TYPE.

Opening the port safely (ESP32-S3 + Arduino's TinyUSB CDC):
  * never 1200 baud: that line coding reboots the chip into its ROM bootloader;
  * assert DTR and RTS together: the CDC only sends while DTR is set, and the
    esptool reset sequence (DTR=0,RTS=1 -> 1,1 -> 1,0 -> 0,0) must never occur.
"""
from __future__ import annotations

import hashlib
import json
import logging
import queue
import struct
import sys
import threading
import time
from typing import Callable, Iterable

from .base import LinkError, Progress, Unsupported

log = logging.getLogger("link.usb")

MAGIC = b"\x1bPD"
HEADER_LEN = 8
MAX_PAYLOAD = 512 * 1024
MAX_LOG_LINE = 2048
UPDATE_CHUNK = 64 * 1024
BAUD = 115200            # ignored by USB CDC; anything but 1200
ESPRESSIF_VID = 0x303A
PRODUCT_MARK = "PandaDeck"

HELLO = {"app": "pandadeck", "ver": "1.0.0"}

# Pacing big frames. The firmware's CDC receive path handles one byte at a time,
# so a 173 KB backdrop written in one go keeps its UI from drawing for 250-450 ms.
# Image frames are written in PACE_CHUNK pieces at no more than PACE_BYTES_PER_S
# (a 400x216 backdrop then takes ~1.2 s, the cover ~0.8 s, while the panel keeps
# animating; a 56 px app icon ~65 ms). The firmware drops a frame after 1.5 s
# without a byte; one gap here is ~27 ms. JSON frames (snapshots, acks, config)
# and firmware chunks (D: the update screen doesn't animate) go out in one write.
PACE_CHUNK = 4096
PACE_BYTES_PER_S = 150_000
PACE_MIN_BYTES = PACE_CHUNK      # an image frame that fits in one chunk needs no pacing
PACED_TYPES = frozenset("I")


# ---- frame codec (pure) -------------------------------------------------------------

def encode_frame(ftype: str, payload: bytes = b"") -> bytes:
    if len(ftype) != 1 or not ftype.isalpha() or not ftype.isascii():
        raise ValueError("frame type must be one ASCII letter")
    if len(payload) > MAX_PAYLOAD:
        raise ValueError(f"payload too large ({len(payload)} > {MAX_PAYLOAD})")
    return MAGIC + ftype.encode() + struct.pack("<I", len(payload)) + payload


def should_pace(ftype: str, frame_len: int) -> bool:
    """Pure: write this frame in paced chunks?"""
    return ftype in PACED_TYPES and frame_len > PACE_MIN_BYTES


def write_paced(write: Callable[[bytes], object], frame: bytes, sleep: Callable[[float], None] = time.sleep,
                clock: Callable[[], float] = time.monotonic, chunk: int = PACE_CHUNK,
                rate: float = PACE_BYTES_PER_S) -> None:
    """Write `frame` in `chunk`-sized pieces, sleeping so the average stays at or
    under `rate` bytes/s (a slow write already counts towards the budget)."""
    start = clock()
    sent = 0
    view = memoryview(frame)
    while sent < len(frame):
        piece = view[sent:sent + chunk]
        write(bytes(piece))
        sent += len(piece)
        if sent < len(frame):
            wait = start + sent / rate - clock()
            if wait > 0:
                sleep(wait)


class FrameParser:
    """Incremental decoder. feed() returns a list of events:
    ("frame", type, payload) or ("log", line)."""

    def __init__(self, max_payload: int = MAX_PAYLOAD, max_line: int = MAX_LOG_LINE):
        self.max_payload = max_payload
        self.max_line = max_line
        self._buf = bytearray()
        self._text = bytearray()

    def feed(self, data: bytes) -> list[tuple]:
        self._buf += data
        out: list[tuple] = []
        while self._buf:
            i = self._buf.find(MAGIC)
            if i < 0:
                # Keep a trailing partial magic (b"\x1b" / b"\x1bP") for the next read.
                keep = 0
                for k in (2, 1):
                    if self._buf.endswith(MAGIC[:k]):
                        keep = k
                        break
                self._text_in(bytes(self._buf[:len(self._buf) - keep]), out)
                del self._buf[:len(self._buf) - keep]
                break
            if i:
                self._text_in(bytes(self._buf[:i]), out)
                del self._buf[:i]
            if len(self._buf) < HEADER_LEN:
                break  # wait for the rest of the header
            ftype = self._buf[3]
            (length,) = struct.unpack_from("<I", self._buf, 4)
            if not (65 <= ftype <= 90 or 97 <= ftype <= 122) or length > self.max_payload:
                # Not a real frame: treat the ESC as log text and resync.
                self._text_in(bytes(self._buf[:1]), out)
                del self._buf[:1]
                continue
            if len(self._buf) < HEADER_LEN + length:
                break  # wait for the payload
            payload = bytes(self._buf[HEADER_LEN:HEADER_LEN + length])
            del self._buf[:HEADER_LEN + length]
            out.append(("frame", chr(ftype), payload))
        return out

    def _text_in(self, data: bytes, out: list) -> None:
        self._text += data
        while True:
            nl = self._text.find(b"\n")
            if nl < 0:
                break
            line = bytes(self._text[:nl]).rstrip(b"\r")
            del self._text[:nl + 1]
            out.append(("log", line.decode("utf-8", "replace")))
        if len(self._text) > self.max_line:
            out.append(("log", bytes(self._text).decode("utf-8", "replace")))
            self._text.clear()


# ---- port discovery ---------------------------------------------------------------------

def _comports():
    from serial.tools import list_ports  # lazy: pyserial is optional for tests
    return list_ports.comports()


def candidate_ports(comports: Callable[[], Iterable] | None = None, preferred_serial: str = "") -> list[str]:
    """Ports that may be a PandaDeck, best first.

    VID 0x303A (Espressif) and a product string containing "PandaDeck". On
    Windows pyserial can't read the product string (it's None), so unnamed
    Espressif ports are candidates too and get verified with a hello."""
    sure, maybe = [], []
    for p in (comports or _comports)():
        if getattr(p, "vid", None) != ESPRESSIF_VID:
            continue
        names = " ".join(str(x) for x in (getattr(p, "product", None), getattr(p, "interface", None),
                                          getattr(p, "description", None), getattr(p, "serial_number", None)) if x)
        if PRODUCT_MARK.lower() in names.lower():  # (Windows only shows the serial: "PandaDeck-..." works there)
            sure.append(p)
        elif not getattr(p, "product", None):
            maybe.append(p)
    ordered = sure + maybe
    ordered.sort(key=lambda p: 0 if preferred_serial and getattr(p, "serial_number", "") == preferred_serial else 1)
    return [p.device for p in ordered]


def port_serial(device: str, comports: Callable[[], Iterable] | None = None) -> str:
    for p in (comports or _comports)():
        if p.device == device:
            return getattr(p, "serial_number", "") or ""
    return ""


def open_serial(port: str):
    import serial  # lazy

    ser = serial.Serial()
    ser.port = port
    ser.baudrate = BAUD
    ser.timeout = 0.05
    ser.write_timeout = 3
    ser.dtr = True   # applied together on open: never the esptool reset sequence
    ser.rts = True
    if sys.platform != "win32":
        ser.exclusive = True
    ser.open()
    return ser


# ---- the link -----------------------------------------------------------------------------

class UsbLink:
    kind = "usb"

    def __init__(self, port: str, ser=None, reply_timeout: float = 3.0, sleep: Callable[[float], None] = time.sleep):
        self.where = port
        self.reply_timeout = reply_timeout
        self._sleep = sleep
        self._ser = ser if ser is not None else open_serial(port)
        self._parser = FrameParser()
        self._frames: queue.Queue = queue.Queue()
        self._req = threading.Lock()
        self._closed = threading.Event()
        self.dead = False
        self.panel_log: list[str] = []  # last lines of the firmware's log (for diagnostics)
        self._reader = threading.Thread(target=self._read_loop, name=f"usb-{port}", daemon=True)
        self._reader.start()

    # ---- reader ---------------------------------------------------------------
    def _read_loop(self) -> None:
        while not self._closed.is_set():
            try:
                data = self._ser.read(4096)
            except Exception as e:  # noqa: BLE001 - SerialException on unplug
                if not self._closed.is_set():
                    log.info("USB read failed (%s): panel unplugged?", e)
                self.dead = True
                self._frames.put(None)  # wake a waiting request
                return
            if not data:
                continue
            for ev in self._parser.feed(data):
                if ev[0] == "frame":
                    self._frames.put((ev[1], ev[2]))
                else:
                    self.panel_log.append(ev[1])
                    del self.panel_log[:-200]
                    log.debug("panel: %s", ev[1])

    # ---- requests ---------------------------------------------------------------
    def request(self, ftype: str, payload: bytes = b"", timeout: float | None = None) -> bytes:
        if self.dead or self._closed.is_set():
            raise LinkError("USB link is closed")
        want = ftype.lower()
        with self._req:
            while True:  # drop stale replies (late answers to timed-out requests)
                try:
                    self._frames.get_nowait()
                except queue.Empty:
                    break
            frame = encode_frame(ftype, payload)
            try:
                if should_pace(ftype, len(frame)):
                    write_paced(self._ser.write, frame, sleep=self._sleep)
                else:
                    self._ser.write(frame)
            except Exception as e:  # noqa: BLE001 - SerialTimeoutException / OSError
                self.dead = True
                raise LinkError(f"USB write failed: {e}") from e
            deadline = time.monotonic() + (timeout or self.reply_timeout)
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise LinkError(f"no reply to {ftype!r} (panel busy or restarting)")
                try:
                    item = self._frames.get(timeout=left)
                except queue.Empty:
                    continue
                if item is None:
                    raise LinkError("USB link lost")
                rtype, rpayload = item
                if rtype == want:
                    return rpayload
                log.debug("ignoring unexpected %r frame while waiting for %r", rtype, want)

    def _json(self, ftype: str, obj, timeout: float | None = None) -> dict:
        body = b"" if obj is None else json.dumps(obj, separators=(",", ":")).encode()
        raw = self.request(ftype, body, timeout)
        try:
            out = json.loads(raw) if raw else {}
        except ValueError as e:
            raise LinkError(f"bad JSON in {ftype.lower()!r} reply") from e
        if not isinstance(out, dict):
            raise LinkError(f"bad {ftype.lower()!r} reply")
        if out.get("ok") is False and out.get("error") == "unknown":
            raise Unsupported(f"panel firmware doesn't support {ftype!r}")
        return out

    def hello(self) -> dict:
        return self._json("H", HELLO, timeout=2.0)

    def ping(self) -> bool:
        self.request("P", b"", timeout=2.0)
        return True

    def sync(self, snap: dict) -> dict:
        return self._json("S", snap)

    def image(self, hdr: dict, data: bytes) -> bool:
        body = json.dumps(hdr, separators=(",", ":")).encode() + b"\n" + data
        raw = self.request("I", body, timeout=10.0)
        try:
            out = json.loads(raw) if raw else {}
        except ValueError:
            return False
        if not out.get("ok"):
            log.info("panel refused image %s: %s", hdr.get("key") or hdr.get("kind"), out.get("error"))
        return bool(out.get("ok"))

    def config(self, d: dict) -> dict:
        return self._json("C", d, timeout=5.0)

    def update(self, firmware: bytes, progress: Progress | None = None) -> None:
        total = len(firmware)
        md5 = hashlib.md5(firmware).hexdigest()  # noqa: S324 - integrity check the OTA API expects
        r = self._json("U", {"begin": {"size": total, "md5": md5}}, timeout=45.0)
        if not r.get("ok"):
            raise LinkError(f"panel refused the update: {r.get('error') or 'unknown error'}")
        try:
            self._send_image(firmware, total, progress)
        except LinkError:
            try:  # tell the panel to drop the half-written image (best effort)
                self._json("U", {"abort": True}, timeout=5.0)
            except LinkError:
                pass
            raise

    def _send_image(self, firmware: bytes, total: int, progress: Progress | None) -> None:
        done = 0
        if progress:
            progress(0, total)
        while done < total:
            chunk = firmware[done:done + UPDATE_CHUNK]
            raw = self.request("D", chunk, timeout=15.0)
            try:
                r = json.loads(raw) if raw else {}
            except ValueError:
                r = {}
            if not r.get("ok"):
                raise LinkError(f"update failed at {done} bytes: {r.get('error') or 'no answer'}")
            done += len(chunk)
            if progress:
                progress(done, total)
        r = self._json("U", {"end": True}, timeout=30.0)
        if not r.get("ok"):
            raise LinkError(f"update not accepted: {r.get('error') or 'checksum mismatch?'}")

    def close(self) -> None:
        self._closed.set()
        try:
            self._ser.close()
        except Exception:  # noqa: BLE001
            pass
