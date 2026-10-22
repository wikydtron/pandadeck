"""This computer's time zone as a POSIX TZ string for the panel's clock
(config key "tz", at most 47 characters), e.g. "EST5EDT,M3.2.0,M11.1.0".

POSIX systems: the footer of /etc/localtime (TZif v2+) already is one.
Everywhere else (Windows): derived from the OS's own local-time rules by
scanning this year for the daylight-saving transitions.
"""
from __future__ import annotations

import calendar
import datetime as dt
import re
import time

MAX_LEN = 47
STEP_S = 15 * 60


def _fmt_offset(seconds_east: int) -> str:
    """UTC+5:30 -> "-5:30" (POSIX counts hours *west* of UTC)."""
    west = -seconds_east
    sign = "-" if west < 0 else ""
    west = abs(west)
    h, rem = divmod(west, 3600)
    m, s = divmod(rem, 60)
    out = f"{sign}{h}"
    if m or s:
        out += f":{m:02d}"
    if s:
        out += f":{s:02d}"
    return out


def _name(raw: str, seconds_east: int) -> str:
    """POSIX zone name: 3+ letters, else an <+0530>-style quoted name."""
    raw = (raw or "").strip()
    if not re.fullmatch(r"[A-Za-z]{3,6}", raw):
        initials = "".join(w[0] for w in raw.split() if w[:1].isalpha())  # "Eastern Standard Time" -> "EST"
        raw = initials if re.fullmatch(r"[A-Za-z]{3,6}", initials) else ""
    if raw:
        return {"CUT": "UTC"}.get(raw.upper(), raw.upper())  # Windows' "Coordinated Universal Time"
    sign = "+" if seconds_east >= 0 else "-"
    h, rem = divmod(abs(seconds_east), 3600)
    return f"<{sign}{h:02d}{rem // 60:02d}>" if rem else f"<{sign}{h:02d}>"


def _rule(local: dt.datetime) -> str:
    """Wall-clock moment of a transition -> "Mm.w.d[/h[:mm]]" (w=5: last such weekday)."""
    days = calendar.monthrange(local.year, local.month)[1]
    week = 5 if local.day + 7 > days else (local.day - 1) // 7 + 1
    weekday = (local.weekday() + 1) % 7  # POSIX: Sunday = 0
    rule = f"M{local.month}.{week}.{weekday}"
    if (local.hour, local.minute) != (2, 0):
        rule += f"/{local.hour}" + (f":{local.minute:02d}" if local.minute else "")
    return rule


def from_rules(year: int, localtime=time.localtime, tznames=None) -> str:
    """Pure-ish: build the TZ string from a localtime() function (testable with fakes)."""
    tznames = tznames or time.tzname
    start = calendar.timegm((year, 1, 1, 0, 0, 0))
    end = calendar.timegm((year + 1, 1, 1, 0, 0, 0))
    prev = localtime(start)
    std_off = dst_off = None
    to_dst = to_std = None
    for ts in range(start, end, STEP_S):
        cur = localtime(ts)
        if cur.tm_isdst > 0:
            dst_off = cur.tm_gmtoff
        else:
            std_off = cur.tm_gmtoff
        if cur.tm_gmtoff != prev.tm_gmtoff:
            wall = dt.datetime(1970, 1, 1) + dt.timedelta(seconds=ts + prev.tm_gmtoff)  # in the old offset
            if cur.tm_isdst > 0:
                to_dst = wall
            else:
                to_std = wall
        prev = cur
    if std_off is None:  # permanent "DST": treat as standard time
        std_off, dst_off = dst_off, None
    std = _name(tznames[0], std_off) + _fmt_offset(std_off)
    if dst_off is None or to_dst is None or to_std is None:
        return std
    out = f"{std}{_name(tznames[1], dst_off)}"
    if dst_off - std_off != 3600:
        out += _fmt_offset(dst_off)
    return f"{out},{_rule(to_dst)},{_rule(to_std)}"


def tzif_footer(path: str = "/etc/localtime") -> str:
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return ""
    if data[:4] != b"TZif" or data[4:5] not in (b"2", b"3", b"4"):
        return ""
    footer = data.rstrip(b"\n").rsplit(b"\n", 1)[-1]
    try:
        text = footer.decode("ascii").strip()
    except UnicodeDecodeError:
        return ""
    return text if 0 < len(text) <= MAX_LEN and "\n" not in text else ""


def posix_tz() -> str:
    """Best effort; "" when nothing sensible could be built."""
    tz = tzif_footer()
    if tz:
        return tz
    try:
        tz = from_rules(dt.date.today().year)
    except (OverflowError, OSError, ValueError):
        return ""
    return tz if len(tz) <= MAX_LEN else ""
