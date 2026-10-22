"""The panel clock's POSIX TZ string, the "tz" push, and firmware-0.6 link details."""
import calendar
import json
import threading
from types import SimpleNamespace

import pytest

from pandadeck import tz
from pandadeck.link.base import LinkError
from pandadeck.link.usb import UsbLink


def fake_localtime(std_off, dst_off=None, dst_start=None, dst_end=None):
    """localtime() for a zone with DST between two UTC instants."""
    def lt(ts):
        in_dst = dst_off is not None and (
            dst_start <= ts < dst_end if dst_start < dst_end else not dst_end <= ts < dst_start)
        return SimpleNamespace(tm_gmtoff=dst_off if in_dst else std_off, tm_isdst=1 if in_dst else 0)
    return lt


def utc(y, mo, d, h=0, mi=0):
    return calendar.timegm((y, mo, d, h, mi, 0))


def test_us_eastern():
    lt = fake_localtime(-5 * 3600, -4 * 3600, utc(2026, 3, 8, 7), utc(2026, 11, 1, 6))
    assert tz.from_rules(2026, lt, ("Eastern Standard Time", "Eastern Daylight Time")) == "EST5EDT,M3.2.0,M11.1.0"


def test_southern_hemisphere_and_last_sunday():
    # Sydney 2026: DST ends Sun 5 Apr 03:00 AEDT (16:00 UTC the day before), starts Sun 4 Oct 02:00 AEST.
    lt = fake_localtime(10 * 3600, 11 * 3600, utc(2026, 10, 3, 16), utc(2026, 4, 4, 16))
    assert tz.from_rules(2026, lt, ("AEST", "AEDT")) == "AEST-10AEDT,M10.1.0,M4.1.0/3"
    # Europe/Paris 2026: last Sundays of March (29th) and October (25th), 01:00 UTC.
    lt = fake_localtime(3600, 7200, utc(2026, 3, 29, 1), utc(2026, 10, 25, 1))
    assert tz.from_rules(2026, lt, ("CET", "CEST")) == "CET-1CEST,M3.5.0,M10.5.0/3"


def test_no_dst_and_odd_offsets():
    assert tz.from_rules(2026, fake_localtime(19800), ("India Standard Time", "India Daylight Time")) == "IST-5:30"
    assert tz.from_rules(2026, fake_localtime(0), ("Coordinated Universal Time", "")) == "UTC0"
    assert tz.from_rules(2026, fake_localtime(-3 * 3600), ("-03", "-03")) == "<-03>3"
    assert len(tz.from_rules(2026, fake_localtime(20700), ("Nepal", ""))) <= tz.MAX_LEN


def test_tzif_footer(tmp_path):
    f = tmp_path / "localtime"
    f.write_bytes(b"TZif2" + b"\0" * 40 + b"\nEST5EDT,M3.2.0,M11.1.0\n")
    assert tz.tzif_footer(str(f)) == "EST5EDT,M3.2.0,M11.1.0"
    f.write_bytes(b"TZif" + b"\0" * 40)  # version 1: no footer
    assert tz.tzif_footer(str(f)) == ""
    assert tz.tzif_footer(str(tmp_path / "missing")) == ""


def test_this_machine_has_a_usable_tz():
    out = tz.posix_tz()
    assert 0 < len(out) <= tz.MAX_LEN and "\n" not in out


def test_tz_only_goes_to_firmware_that_knows_it(fake_platform, monkeypatch):
    from pandadeck import service as svcmod
    monkeypatch.setattr(svcmod, "posix_tz", lambda: "EST5EDT,M3.2.0,M11.1.0")
    svc = svcmod.Service(panel=False, platform=fake_platform)
    sent = []

    class Link:
        kind = "usb"

        def config(self, d):
            sent.append(d)
            return {"ok": True, "settings": d}

    link = Link()
    svc.links.current = lambda: link
    svc._on_connect(link, {"settings": {"pages": ["deck", "music", "hud"]}})  # 0.5 firmware: no "tz" key
    svc._on_connect(link, {"settings": {"pages": ["deck", "music", "hud"], "tz": "UTC0"}})
    svc._on_connect(link, {"settings": {"pages": ["deck", "music", "hud"], "tz": "EST5EDT,M3.2.0,M11.1.0"}})
    for _ in range(100):
        if sent:
            break
        threading.Event().wait(0.02)
    threading.Event().wait(0.1)
    assert sent == [{"tz": "EST5EDT,M3.2.0,M11.1.0"}]


# ---- firmware 0.6 link details ----------------------------------------------------------------------

def test_failed_usb_update_is_aborted():
    from test_app_usb import FakePanelSerial

    ser = FakePanelSerial()
    calls = {"d": 0}

    def handler(ftype, payload):
        if ftype == "D":
            calls["d"] += 1
            return ("d", {"ok": False, "error": "flash write failed"}) if calls["d"] == 2 else ser.default(ftype, payload)
        return ser.default(ftype, payload)

    ser.handler = handler
    link = UsbLink("COM7", ser=ser)
    try:
        with pytest.raises(LinkError, match="flash write failed"):
            link.update(bytes([0xE9]) + bytes(200_000))
    finally:
        link.close()
    assert json.loads(ser.requests[-1][1]) == {"abort": True}


def test_bootloader_pauses_usb_probing(settings, fake_secrets, fake_state):
    from pandadeck.link.discover import LinkManager
    probed = []
    clock = [100.0]
    m = LinkManager(settings, fake_secrets, fake_state, ports=lambda preferred_serial="": ["COM7"],
                    open_usb=lambda p: probed.append(p) or (_ for _ in ()).throw(OSError("busy")),
                    clock=lambda: clock[0])
    m.pause_usb(600)
    m.tick()
    assert probed == [] and m.status()["usb_paused"] == 600
    m.resume_usb()
    m.tick()
    assert probed == ["COM7"]


def test_windows_serial_number_is_matched_case_insensitively():
    from pandadeck.link.usb import candidate_ports
    ports = [SimpleNamespace(device="COM9", vid=0x303A, product=None, serial_number="", description="", interface=None),
             SimpleNamespace(device="COM7", vid=0x303A, product=None, serial_number="PANDADECK-AABBCC112233",
                             description="USB Serial Device (COM7)", interface=None)]
    assert candidate_ports(lambda: ports)[0] == "COM7"
