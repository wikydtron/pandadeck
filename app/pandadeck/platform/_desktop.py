"""freedesktop.org desktop entries: parsing, discovery, Exec handling.

Pure Python (no GLib), so it's unit-testable on any OS. Follows the Desktop
Entry spec 1.5: desktop-file IDs, XDG data-dir precedence (first ID wins,
Hidden masks lower ones), NoDisplay/OnlyShowIn/NotShowIn/TryExec, localized
Name, string escapes, Exec quoting and field codes.
"""
from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping

log = logging.getLogger("pandadeck.platform.linux")

GROUP = "Desktop Entry"
MAX_DESKTOP_BYTES = 512 * 1024
DROP_CODES = frozenset("fFuUdDnNvm")  # file/URL lists and deprecated codes: we never pass files
EXEC_RESERVED = set(' \t\n"\'\\><~|&;$*?#()`')
EXEC_ESCAPED_IN_QUOTES = '"`$\\'
TERMINALS = (("x-terminal-emulator", ["-e"]), ("gnome-terminal", ["--"]), ("konsole", ["-e"]),
             ("xfce4-terminal", ["-x"]), ("kgx", ["--"]), ("ptyxis", ["--"]), ("tilix", ["-e"]),
             ("alacritty", ["-e"]), ("kitty", []), ("foot", []), ("wezterm", ["start", "--"]),
             ("xterm", ["-e"]))


# ---- low-level parsing -------------------------------------------------------------------------

def parse_group(text: str, group: str = GROUP) -> dict[str, str]:
    """Raw key -> value pairs of one group (first occurrence of a key wins)."""
    out: dict[str, str] = {}
    current = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            if current == group:
                break  # the group ended; later groups are actions etc.
            current = line[1:-1]
            continue
        if current != group or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out.setdefault(key.strip(), value.strip())
    return out


def unescape(value: str) -> str:
    """Spec 'string' escapes: \\s \\n \\t \\r \\\\ (unknown escapes kept as-is)."""
    out, i = [], 0
    table = {"s": " ", "n": "\n", "t": "\t", "r": "\r", "\\": "\\"}
    while i < len(value):
        c = value[i]
        if c == "\\" and i + 1 < len(value) and value[i + 1] in table:
            out.append(table[value[i + 1]])
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def escape(value: str) -> str:
    """Inverse of unescape() for writing values."""
    return (value.replace("\\", "\\\\").replace("\n", "\\n").replace("\t", "\\t").replace("\r", "\\r"))


def split_list(value: str) -> tuple[str, ...]:
    return tuple(p.strip() for p in value.split(";") if p.strip())


def is_true(value: str | None) -> bool:
    return (value or "").strip().lower() in ("true", "1")


def locale_candidates(env: Mapping[str, str]) -> list[str]:
    """LC_ALL / LC_MESSAGES / LANG -> spec match order: ll_CC@mod, ll_CC, ll@mod, ll."""
    raw = env.get("LC_ALL") or env.get("LC_MESSAGES") or env.get("LANG") or ""
    raw = raw.split(":")[0]
    if not raw or raw in ("C", "POSIX") or raw.startswith("C."):
        return []
    mod = ""
    if "@" in raw:
        raw, mod = raw.split("@", 1)
    raw = raw.split(".", 1)[0]
    lang, _, country = raw.partition("_")
    out = []
    if country and mod:
        out.append(f"{lang}_{country}@{mod}")
    if country:
        out.append(f"{lang}_{country}")
    if mod:
        out.append(f"{lang}@{mod}")
    out.append(lang)
    return out


def localized(raw: Mapping[str, str], key: str, locales: Iterable[str]) -> str:
    for loc in locales:
        v = raw.get(f"{key}[{loc}]")
        if v:
            return unescape(v)
    return unescape(raw.get(key, ""))


# ---- Exec ------------------------------------------------------------------------------------------

def split_exec(value: str) -> list[str]:
    """Tokenise an (already unescaped) Exec value per the spec's quoting rules."""
    args: list[str] = []
    cur: list[str] = []
    quoted = in_quotes = False
    i = 0
    while i < len(value):
        c = value[i]
        if in_quotes:
            if c == "\\" and i + 1 < len(value) and value[i + 1] in EXEC_ESCAPED_IN_QUOTES:
                cur.append(value[i + 1])
                i += 2
                continue
            if c == '"':
                in_quotes = False
            else:
                cur.append(c)
        elif c == '"':
            in_quotes = quoted = True
        elif c in " \t\n":
            if cur or quoted:
                args.append("".join(cur))
            cur, quoted = [], False
        else:
            cur.append(c)
        i += 1
    if in_quotes:
        raise ValueError("unterminated quote in Exec")
    if cur or quoted:
        args.append("".join(cur))
    return args


def expand_field_codes(args: list[str], name: str = "", icon: str = "", location: str = "") -> list[str]:
    """Apply field codes for a launch with no files: %f %u ... are dropped, %i
    becomes --icon <Icon>, %c the name, %k the desktop file, %% a literal %."""
    out: list[str] = []
    for arg in args:
        if arg == "%i":
            if icon:
                out += ["--icon", icon]
            continue
        if len(arg) == 2 and arg[0] == "%" and arg[1] in DROP_CODES:
            continue
        res: list[str] = []
        dropped = False
        i = 0
        while i < len(arg):
            if arg[i] == "%" and i + 1 < len(arg):
                code = arg[i + 1]
                if code == "%":
                    res.append("%")
                elif code == "c":
                    res.append(name)
                elif code == "k":
                    res.append(location)
                else:
                    dropped = True
                i += 2
                continue
            res.append(arg[i])
            i += 1
        expanded = "".join(res)
        if expanded or not dropped:
            out.append(expanded)
    return out


def quote_exec_arg(arg: str) -> str:
    if arg and not any(c in EXEC_RESERVED for c in arg):
        return arg
    return '"' + "".join("\\" + c if c in EXEC_ESCAPED_IN_QUOTES else c for c in arg) + '"'


def exec_value(argv: list[str]) -> str:
    """argv -> a value for an Exec= line (quoted, %-escaped, string-escaped)."""
    return escape(" ".join(quote_exec_arg(a) for a in argv).replace("%", "%%"))


def terminal_command(args: list[str], which: Callable[[str], str | None] = shutil.which) -> list[str]:
    """Wrap args for Terminal=true. No terminal found: run them bare (better than nothing)."""
    for term, flag in TERMINALS:
        path = which(term)
        if path:
            return [path, *flag, *args]
    return list(args)


# ---- entries ---------------------------------------------------------------------------------------

@dataclass(frozen=True)
class DesktopEntry:
    id: str                 # desktop file ID, e.g. "org.gnome.Calculator.desktop"
    path: str               # absolute path of the .desktop file
    name: str
    exec: str = ""          # unescaped Exec value ("" = none)
    icon: str = ""
    terminal: bool = False
    try_exec: str = ""
    workdir: str = ""
    type: str = "Application"
    no_display: bool = False
    hidden: bool = False
    only_show_in: tuple[str, ...] = ()
    not_show_in: tuple[str, ...] = ()
    dbus_activatable: bool = False

    def visible(self, desktops: Iterable[str], which: Callable[[str], str | None] = shutil.which) -> bool:
        """Would a menu show this entry (on these XDG_CURRENT_DESKTOP names)?"""
        if self.type != "Application" or self.no_display or self.hidden or not self.name:
            return False
        if not self.exec and not self.dbus_activatable:
            return False
        current = {d.lower() for d in desktops}
        if self.only_show_in and not current & {d.lower() for d in self.only_show_in}:
            return False
        if self.not_show_in and current & {d.lower() for d in self.not_show_in}:
            return False
        if self.try_exec:
            if os.path.isabs(self.try_exec):
                if not os.access(self.try_exec, os.X_OK):
                    return False
            elif not which(self.try_exec):
                return False
        return True

    def command(self, which: Callable[[str], str | None] = shutil.which) -> list[str]:
        """argv to start the app (no files), wrapped in a terminal if Terminal=true."""
        args = expand_field_codes(split_exec(self.exec), self.name, self.icon, self.path)
        if not args:
            raise ValueError(f"{self.id}: empty Exec")
        return terminal_command(args, which) if self.terminal else args


def parse_entry(text: str, desktop_id: str, path: str, locales: Iterable[str] = ()) -> DesktopEntry | None:
    raw = parse_group(text)
    if not raw:
        return None
    locales = list(locales)
    return DesktopEntry(
        id=desktop_id, path=path,
        name=" ".join(localized(raw, "Name", locales).split()),
        exec=unescape(raw.get("Exec", "")),
        icon=unescape(raw.get("Icon", "")),
        terminal=is_true(raw.get("Terminal")),
        try_exec=unescape(raw.get("TryExec", "")),
        workdir=unescape(raw.get("Path", "")),
        type=raw.get("Type", ""),
        no_display=is_true(raw.get("NoDisplay")),
        hidden=is_true(raw.get("Hidden")),
        only_show_in=split_list(raw.get("OnlyShowIn", "")),
        not_show_in=split_list(raw.get("NotShowIn", "")),
        dbus_activatable=is_true(raw.get("DBusActivatable")),
    )


def load_entry(path: Path, desktop_id: str, locales: Iterable[str] = ()) -> DesktopEntry | None:
    try:
        if path.stat().st_size > MAX_DESKTOP_BYTES:
            return None
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        log.debug("unreadable desktop file %s: %s", path, e)
        return None
    return parse_entry(text, desktop_id, str(path), locales)


# ---- discovery -----------------------------------------------------------------------------------------

def data_dirs(env: Mapping[str, str], home: Path) -> list[Path]:
    """$XDG_DATA_HOME, $XDG_DATA_DIRS, then flatpak/snap export dirs (if missing)."""
    data_home = Path(env.get("XDG_DATA_HOME") or home / ".local" / "share")
    dirs = [Path(d) for d in (env.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":") if d]
    extra = [home / ".local/share/flatpak/exports/share", Path("/var/lib/flatpak/exports/share"),
             Path("/var/lib/snapd/desktop")]
    out: list[Path] = []
    for d in [data_home, *dirs, *extra]:
        if d not in out:
            out.append(d)
    return out


def current_desktops(env: Mapping[str, str]) -> list[str]:
    return [d for d in (env.get("XDG_CURRENT_DESKTOP") or "").split(":") if d]


def scan(dirs: Iterable[Path], desktops: Iterable[str], locales: Iterable[str] = (),
         which: Callable[[str], str | None] = shutil.which) -> dict[str, DesktopEntry]:
    """Visible applications by desktop-file ID, honouring data-dir precedence."""
    desktops, locales = list(desktops), list(locales)
    seen: set[str] = set()
    out: dict[str, DesktopEntry] = {}
    for base in dirs:
        appdir = Path(base) / "applications"
        if not appdir.is_dir():
            continue
        try:
            files = sorted(appdir.rglob("*.desktop"))
        except OSError:
            continue
        for path in files:
            desktop_id = "-".join(path.relative_to(appdir).parts)
            if desktop_id in seen:
                continue
            seen.add(desktop_id)  # even hidden/invalid: a user override masks lower dirs
            entry = load_entry(path, desktop_id, locales)
            if entry is not None and entry.visible(desktops, which):
                out[desktop_id] = entry
    return out
