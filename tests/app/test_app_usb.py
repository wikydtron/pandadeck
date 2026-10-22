"""USB link: frame codec (docs/protocol.md), port discovery, request/reply."""
import json
import queue
import struct
import threading
import time
from types import SimpleNamespace

import pytest

from pandadeck.link import usb
from pandadeck.link.base import LinkError, Unsupported
from pandadeck.link.usb import MAGIC, FrameParser, UsbLink, candidate_ports, encode_frame


# ---- codec ---------------------------------------------------------------------------

def test_encode_frame_layout():
    f = encode_frame("S", b'{"a":1}')
    assert f[:3] == b"\x1bPD" and f[3:4] == b"S"
    assert struct.unpack("<I", f[4:8])[0] == 7 and f[8:] == b'{"a":1}'
    assert encode_frame("P") == b"\x1bPDP\x00\x00\x00\x00"


@pytest.mark.parametrize("bad", ["", "SS", "1", "é"])
def test_encode_frame_rejects_bad_types(bad):
    with pytest.raises(ValueError):
        encode_frame(bad, b"")


def test_encode_frame_rejects_oversize():
    with pytest.raises(ValueError):
        encode_frame("I", b"x" * (usb.MAX_PAYLOAD + 1))


def test_parser_splits_logs_and_frames():
    p = FrameParser()
    data = b"boot ok\r\nwifi: up\n" + encode_frame("h", b'{"app":"pandadeck"}') + b"after\n"
    events = p.feed(data)
    assert events == [("log", "boot ok"), ("log", "wifi: up"), ("frame", "h", b'{"app":"pandadeck"}'), ("log", "after")]


def test_parser_handles_byte_by_byte_feeding():
    p = FrameParser()
    blob = b"x\n" + encode_frame("s", b"0123456789" * 50) + encode_frame("p") + b"tail\n"
    out = []
    for b in blob:
        out.extend(p.feed(bytes([b])))
    assert out[0] == ("log", "x")
    assert out[1] == ("frame", "s", b"0123456789" * 50)
    assert out[2] == ("frame", "p", b"")
    assert out[3] == ("log", "tail")


def test_parser_keeps_partial_magic_and_ansi_escapes():
    p = FrameParser()
    assert p.feed(b"\x1b[0;32mI (12) app: hi\x1b[0m\n") == [("log", "\x1b[0;32mI (12) app: hi\x1b[0m")]
    assert p.feed(b"\x1b") == []          # might be the start of a frame
    assert p.feed(b"P") == []
    assert p.feed(b"Dc\x02\x00\x00\x00{}") == [("frame", "c", b"{}")]


def test_parser_resyncs_after_bogus_header():
    p = FrameParser(max_payload=16)
    bogus = MAGIC + b"S" + struct.pack("<I", 10_000)   # too long -> not a frame
    good = encode_frame("p")
    out = p.feed(bogus + b"\n" + good)
    assert ("frame", "p", b"") in out
    assert any(e[0] == "log" for e in out)
    out = FrameParser().feed(MAGIC + b"1" + struct.pack("<I", 0) + b"\n")  # non-letter type
    assert out and out[0][0] == "log"


def test_parser_flushes_overlong_log_lines():
    p = FrameParser(max_line=8)
    out = p.feed(b"0123456789abc")
    assert out == [("log", "0123456789abc")]


def test_parser_multiple_frames_in_one_chunk():
    p = FrameParser()
    out = p.feed(encode_frame("u", b'{"ok":true}') + encode_frame("d", b'{"ok":true,"written":4}'))
    assert [e[1] for e in out] == ["u", "d"]


# ---- discovery -------------------------------------------------------------------------

def _port(device, vid=0x303A, product=None, serial="", desc="", interface=None):
    return SimpleNamespace(device=device, vid=vid, product=product, serial_number=serial, description=desc,
                           interface=interface)


def test_candidate_ports_filters_and_orders():
    ports = [
        _port("COM3", vid=0x10C4, product="CP2102"),            # not Espressif
        _port("COM8", product="OtherBoard"),                  # Espressif, other product: skip
        _port("COM5", product=None, serial="AABBCC112233"),     # Windows: product unknown -> maybe
        _port("/dev/ttyACM0", product="PandaDeck"),             # sure
        _port("COM9", product=None, desc="USB Serial Device (COM9)"),
    ]
    got = candidate_ports(lambda: ports)
    assert got[0] == "/dev/ttyACM0" and set(got[1:]) == {"COM5", "COM9"} and "COM8" not in got
    got = candidate_ports(lambda: ports, preferred_serial="AABBCC112233")
    assert got[0] == "COM5"
    # Windows: no product string, but a PandaDeck serial number marks the port as ours
    win = [_port("COM9", product=None), _port("COM4", product=None, serial="PandaDeck-AABBCC112233")]
    assert candidate_ports(lambda: win)[0] == "COM4"


def test_port_serial_lookup():
    ports = [_port("COM5", serial="ABC")]
    assert usb.port_serial("COM5", lambda: ports) == "ABC"
    assert usb.port_serial("COM6", lambda: ports) == ""


def test_open_serial_never_uses_1200_baud_and_raises_dtr_rts_together(monkeypatch):
    seen = {}

    class FakeSerial:
        def __init__(self):
            self.dtr = self.rts = None

        def open(self):
            seen.update(baud=self.baudrate, dtr=self.dtr, rts=self.rts, port=self.port)

    import serial
    monkeypatch.setattr(serial, "Serial", FakeSerial)
    usb.open_serial("COM5")
    assert seen["baud"] != 1200 and seen["dtr"] is True and seen["rts"] is True and seen["port"] == "COM5"


# ---- link over a fake serial port --------------------------------------------------------

class FakePanelSerial:
    """Answers frames like the firmware would, with log noise in between."""

    def __init__(self, handler=None):
        self.rx = queue.Queue()
        self.parser = FrameParser()
        self.handler = handler or self.default
        self.closed = False
        self.unplug = False
        self.update_bytes = bytearray()
        self.requests = []

    def default(self, ftype, payload):
        if ftype == "H":
            return "h", {"app": "pandadeck", "fw": "1.0.0", "boot": 7, "token": "tok123",
                         "wifi": {"ip": "10.0.0.9"}}
        if ftype == "S":
            return "s", {"ok": True, "boot": 7, "icons": 0, "events": [{"seq": 1, "k": "cmd", "a": "mic"}]}
        if ftype == "I":
            hdr, _, pixels = payload.partition(b"\n")
            h = json.loads(hdr)
            ok = len(pixels) == h["w"] * h["h"] * (3 if h["kind"] == "icon" else 2)
            return "i", {"ok": ok}
        if ftype == "C":
            return "c", {"ok": True, "settings": json.loads(payload), "reboot": False}
        if ftype == "U":
            return "u", {"ok": True, "written": len(self.update_bytes)}
        if ftype == "D":
            self.update_bytes += payload
            return "d", {"ok": True, "written": len(self.update_bytes)}
        if ftype == "P":
            return "p", None
        return None

    def write(self, data):
        if self.unplug:
            raise OSError("device gone")
        for ev in self.parser.feed(data):
            if ev[0] != "frame":
                continue
            self.requests.append((ev[1], ev[2]))
            reply = self.handler(ev[1], ev[2])
            if reply is None:
                continue
            rtype, obj = reply
            body = b"" if obj is None else json.dumps(obj).encode()
            self.rx.put(b"I (100) log line before reply\n" + encode_frame(rtype, body))
        return len(data)

    def read(self, n):
        if self.unplug:
            raise OSError("device gone")
        try:
            return self.rx.get(timeout=0.02)
        except queue.Empty:
            return b""

    def close(self):
        self.closed = True


def test_link_hello_sync_image_config():
    ser = FakePanelSerial()
    link = UsbLink("COM5", ser=ser)
    try:
        info = link.hello()
        assert info["app"] == "pandadeck" and info["token"] == "tok123"
        assert json.loads(ser.requests[0][1]) == {"app": "pandadeck", "ver": "1.0.0"}
        r = link.sync({"t": 1, "ack": 0})
        assert r["boot"] == 7 and r["events"][0]["a"] == "mic"
        assert link.image({"kind": "icon", "key": "a0", "w": 2, "h": 2, "tag": ""}, b"\0" * 12) is True
        assert link.image({"kind": "backdrop", "w": 2, "h": 2, "tag": "x"}, b"\0" * 3) is False
        assert link.config({"pages": ["deck"]})["settings"] == {"pages": ["deck"]}
        assert link.ping() is True
        assert any("log line" in line for line in link.panel_log)
    finally:
        link.close()
    assert ser.closed


def test_link_update_sends_begin_chunks_end():
    ser = FakePanelSerial()
    link = UsbLink("COM5", ser=ser)
    fw = bytes([0xE9]) + bytes(range(256)) * 600  # ~150 KB -> 3 chunks
    progress = []
    try:
        link.update(fw, lambda d, t: progress.append((d, t)))
    finally:
        link.close()
    kinds = [r[0] for r in ser.requests]
    assert kinds[0] == "U" and kinds[-1] == "U" and kinds.count("D") == 3
    begin = json.loads(ser.requests[0][1])["begin"]
    assert begin["size"] == len(fw) and len(begin["md5"]) == 32
    assert json.loads(ser.requests[-1][1]) == {"end": True}
    assert bytes(ser.update_bytes) == fw
    assert progress[-1] == (len(fw), len(fw)) and all(len(c) <= usb.UPDATE_CHUNK for k, c in ser.requests if k == "D")


def test_link_times_out_when_panel_is_silent():
    ser = FakePanelSerial(handler=lambda t, p: None)
    link = UsbLink("COM5", ser=ser, reply_timeout=0.2)
    try:
        t0 = time.monotonic()
        with pytest.raises(LinkError):
            link.sync({})
        assert time.monotonic() - t0 < 2
    finally:
        link.close()


def test_link_ignores_stale_replies_from_a_timed_out_request():
    calls = {"n": 0}
    ser = FakePanelSerial()

    def slow_first(ftype, payload):
        calls["n"] += 1
        if calls["n"] == 1:
            return None  # first sync never answered...
        return ser.default(ftype, payload)

    ser.handler = slow_first
    link = UsbLink("COM5", ser=ser, reply_timeout=0.2)
    try:
        with pytest.raises(LinkError):
            link.sync({})
        ser.rx.put(encode_frame("s", b'{"stale":true}'))  # ...arrives late
        time.sleep(0.1)
        assert "stale" not in link.sync({})  # drained before the next request
    finally:
        link.close()


def test_unknown_request_reports_unsupported():
    ser = FakePanelSerial(handler=lambda t, p: ("c", {"ok": False, "error": "unknown"}))
    link = UsbLink("COM5", ser=ser)
    try:
        with pytest.raises(Unsupported):
            link.config({"bootloader": True})
    finally:
        link.close()


def test_unplug_marks_link_dead():
    ser = FakePanelSerial()
    link = UsbLink("COM5", ser=ser)
    ser.unplug = True
    deadline = time.monotonic() + 2
    while not link.dead and time.monotonic() < deadline:
        time.sleep(0.01)
    assert link.dead
    with pytest.raises(LinkError):
        link.sync({})
    link.close()


def test_one_request_in_flight_at_a_time():
    ser = FakePanelSerial()
    link = UsbLink("COM5", ser=ser)
    errors = []

    def worker():
        try:
            for _ in range(20):
                assert link.sync({})["boot"] == 7
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    link.close()
    assert not errors


# ---- pacing big image frames ------------------------------------------------------------------

def test_write_paced_chunks_and_keeps_the_rate():
    from pandadeck.link import usb
    now = [0.0]
    writes, sleeps = [], []

    def write(b):
        writes.append(b)
        now[0] += 0.001  # a write takes 1 ms

    def sleep(s):
        sleeps.append(s)
        now[0] += s

    frame = bytes(range(256)) * 700  # 179 200 bytes, like the 400x216 backdrop
    usb.write_paced(write, frame, sleep=sleep, clock=lambda: now[0])
    assert b"".join(writes) == frame
    assert all(len(w) == usb.PACE_CHUNK for w in writes[:-1]) and 0 < len(writes[-1]) <= usb.PACE_CHUNK
    assert len(sleeps) == len(writes) - 1                     # no pause after the last chunk
    assert max(sleeps) < 0.1                                  # far below the firmware's 1.5 s frame timeout
    assert len(frame) / now[0] <= usb.PACE_BYTES_PER_S * (1 + usb.PACE_CHUNK / len(frame))  # rate capped (the last chunk isn't waited for)
    assert now[0] < 1.5


def test_only_big_image_frames_are_paced():
    from pandadeck.link import usb
    assert usb.should_pace("I", 173_000) and usb.should_pace("I", 9_500)  # backdrop, 56 px icon
    assert not usb.should_pace("I", 1_000)                               # tiny picture: one write
    assert not usb.should_pace("S", 60_000) and not usb.should_pace("D", 65_544)  # JSON, firmware chunks


def test_link_paces_image_frames_but_not_json(monkeypatch):
    ser = FakePanelSerial()
    sizes = []
    real_write = ser.write
    ser.write = lambda data: sizes.append(len(data)) or real_write(data)
    slept = []
    link = UsbLink("COM5", ser=ser, sleep=slept.append)
    try:
        assert link.image({"kind": "backdrop", "w": 100, "h": 100, "tag": "x"}, b"\0" * 20_000) is True
        assert max(sizes) == 4096 and len(sizes) == 5 and len(slept) <= 4
        sizes.clear()
        link.sync({"t": 1, "ack": 0, "pad": "x" * 30_000})
        assert len(sizes) == 1                                 # JSON: one write
    finally:
        link.close()
