"""Windows shell icons for Start-menu apps.

The shell renders any shell:AppsFolder item (Store apps and classic
programs alike) via IShellItemImageFactory -> 32-bit premultiplied BGRA.
All COM work runs on one long-lived STA thread (no COM objects shared or
churned across threads - see _win_audio.py for why that matters).
Only ctypes is used, and only inside functions, so this imports on any OS.
"""
from __future__ import annotations

import ctypes
import logging
import queue
import threading
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout

log = logging.getLogger("pandadeck.platform.win")

SIIGBF_BIGGERSIZEOK = 0x1
SIIGBF_ICONONLY = 0x4
COINIT_APARTMENTTHREADED = 0x2
ICON_TIMEOUT_S = 15.0
MAX_RESTARTS = 3
IID_IMAGE_FACTORY = "{bcc18b79-ba16-442f-80c4-8a59c30c463b}"


def premultiplied_bgra_to_rgba(data: bytes, width: int, height: int):
    """Pure: shell bitmap bytes -> straight-alpha RGBA. Bitmaps whose alpha is
    all zero carry no alpha at all: those are opaque icons."""
    from PIL import Image

    img = Image.frombuffer("RGBA", (width, height), data, "raw", "BGRA", 0, 1)
    if img.getchannel("A").getextrema() == (0, 0):
        return Image.frombuffer("RGB", (width, height), data, "raw", "BGRX", 0, 1).convert("RGBA")
    # Reinterpret as premultiplied RGBa; PIL un-premultiplies on convert.
    return Image.frombytes("RGBa", (width, height), img.tobytes()).convert("RGBA")


class _Api:
    """Private WinDLL handles with explicit prototypes (64-bit safe handles,
    and no clobbering of the shared ctypes.windll prototypes)."""

    def __init__(self):
        from ctypes import wintypes as w

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort), ("Data3", ctypes.c_ushort),
                        ("Data4", ctypes.c_ubyte * 8)]

        class SIZE(ctypes.Structure):
            _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]

        class BITMAP(ctypes.Structure):
            _fields_ = [("bmType", ctypes.c_long), ("bmWidth", ctypes.c_long), ("bmHeight", ctypes.c_long),
                        ("bmWidthBytes", ctypes.c_long), ("bmPlanes", w.WORD), ("bmBitsPixel", w.WORD),
                        ("bmBits", ctypes.c_void_p)]

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [("biSize", w.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
                        ("biPlanes", w.WORD), ("biBitCount", w.WORD), ("biCompression", w.DWORD),
                        ("biSizeImage", w.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                        ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", w.DWORD), ("biClrImportant", w.DWORD)]

        self.GUID, self.SIZE, self.BITMAP, self.BITMAPINFOHEADER = GUID, SIZE, BITMAP, BITMAPINFOHEADER
        self.ole32 = ctypes.WinDLL("ole32")
        self.shell32 = ctypes.WinDLL("shell32")
        self.gdi32 = ctypes.WinDLL("gdi32")
        self.user32 = ctypes.WinDLL("user32")

        self.ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, w.DWORD]
        self.ole32.CoInitializeEx.restype = ctypes.c_long
        self.ole32.CLSIDFromString.argtypes = [w.LPCWSTR, ctypes.POINTER(GUID)]
        self.ole32.CLSIDFromString.restype = ctypes.c_long
        self.shell32.SHCreateItemFromParsingName.argtypes = [w.LPCWSTR, ctypes.c_void_p, ctypes.POINTER(GUID),
                                                             ctypes.POINTER(ctypes.c_void_p)]
        self.shell32.SHCreateItemFromParsingName.restype = ctypes.c_long
        self.gdi32.GetObjectW.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p]
        self.gdi32.GetObjectW.restype = ctypes.c_int
        self.gdi32.GetDIBits.argtypes = [w.HDC, w.HBITMAP, w.UINT, w.UINT, ctypes.c_void_p,
                                         ctypes.POINTER(BITMAPINFOHEADER), w.UINT]
        self.gdi32.GetDIBits.restype = ctypes.c_int
        self.gdi32.DeleteObject.argtypes = [w.HANDLE]
        self.gdi32.DeleteObject.restype = w.BOOL
        self.user32.GetDC.argtypes = [w.HWND]
        self.user32.GetDC.restype = w.HDC
        self.user32.ReleaseDC.argtypes = [w.HWND, w.HDC]
        self.user32.ReleaseDC.restype = ctypes.c_int

        self.iid_factory = GUID()
        self.ole32.CLSIDFromString(IID_IMAGE_FACTORY, ctypes.byref(self.iid_factory))
        # IShellItemImageFactory vtable: 0 QueryInterface, 1 AddRef, 2 Release, 3 GetImage
        self.GetImage = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, SIZE, ctypes.c_int,
                                           ctypes.POINTER(w.HBITMAP))
        self.Release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)

    def method(self, obj: ctypes.c_void_p, index: int, proto):
        vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        return proto(vtable[index])


def _shell_icon(api: _Api, parse_name: str, px: int):
    """RGBA icon of a shell item (shell:AppsFolder\\<AppID>, or a file-system
    path: file, folder, .lnk, .url ...); runs on the STA icon thread."""
    from ctypes import wintypes as w

    factory = ctypes.c_void_p()
    hr = api.shell32.SHCreateItemFromParsingName(parse_name, None,
                                                 ctypes.byref(api.iid_factory), ctypes.byref(factory))
    if hr != 0 or not factory:
        return None
    hbm = w.HBITMAP()
    try:
        hr = api.method(factory, 3, api.GetImage)(factory, api.SIZE(px, px),
                                                  SIIGBF_ICONONLY | SIIGBF_BIGGERSIZEOK, ctypes.byref(hbm))
    finally:
        api.method(factory, 2, api.Release)(factory)
    if hr != 0 or not hbm:
        return None
    try:
        bm = api.BITMAP()
        if not api.gdi32.GetObjectW(hbm, ctypes.sizeof(bm), ctypes.byref(bm)):
            return None
        width, height = bm.bmWidth, abs(bm.bmHeight)
        if width <= 0 or height <= 0 or width * height > 4096 * 4096:
            return None
        bmi = api.BITMAPINFOHEADER(biSize=ctypes.sizeof(api.BITMAPINFOHEADER), biWidth=width, biHeight=-height,
                                   biPlanes=1, biBitCount=32, biCompression=0)
        buf = (ctypes.c_ubyte * (width * height * 4))()
        hdc = api.user32.GetDC(None)
        try:
            lines = api.gdi32.GetDIBits(hdc, hbm, 0, height, buf, ctypes.byref(bmi), 0)
        finally:
            api.user32.ReleaseDC(None, hdc)
        if lines != height:
            return None
        return premultiplied_bgra_to_rgba(bytes(buf), width, height)
    finally:
        api.gdi32.DeleteObject(hbm)


class _StaWorker:
    """One daemon STA thread running icon jobs in order. Daemon (unlike a
    ThreadPoolExecutor worker) so a shell call that never returns can't hold up
    interpreter exit."""

    def __init__(self):
        self.api: _Api | None = None
        self._jobs: queue.Queue = queue.Queue()
        threading.Thread(target=self._run, name="pandadeck-icons", daemon=True).start()

    def _run(self) -> None:
        try:
            self.api = _Api()
            hr = self.api.ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
            if hr < 0:
                log.warning("CoInitializeEx for icons failed: 0x%08X", hr & 0xFFFFFFFF)
        except Exception as e:  # noqa: BLE001 - not Windows / ctypes trouble: jobs fail below
            log.warning("icon thread setup failed: %s", e)
        while True:
            fn, fut = self._jobs.get()
            if not fut.set_running_or_notify_cancel():
                continue
            try:
                if self.api is None:
                    raise OSError("icon thread not initialised")
                fut.set_result(fn(self.api))
            except Exception as e:  # noqa: BLE001
                fut.set_exception(e)

    def submit(self, fn) -> Future:
        fut: Future = Future()
        self._jobs.put((fn, fut))
        return fut


class ShellIcons:
    """Serialises icon extraction onto one COM-initialised (STA) thread. If a
    shell call hangs, that thread is abandoned and a fresh one takes over (at
    most MAX_RESTARTS times, after which icons are simply unavailable)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._worker = _StaWorker()
        self._restarts = 0

    def icon(self, app_id: str, px: int = 256):
        """Icon of a Start-menu app (shell:AppsFolder\\<AppID>)."""
        if not app_id or "\0" in app_id:
            return None
        return self._render(f"shell:AppsFolder\\{app_id}", px)

    def path_icon(self, path: str, px: int = 256):
        """Icon of a file-system item: .exe/.lnk/.url/.bat, documents, folders."""
        if not path or "\0" in path:
            return None
        return self._render(path, px)

    def _render(self, parse_name: str, px: int):
        with self._lock:
            worker = self._worker
        if worker is None:
            return None
        fut = worker.submit(lambda api: _shell_icon(api, parse_name, px))
        try:
            return fut.result(timeout=ICON_TIMEOUT_S)
        except FutureTimeout:
            fut.cancel()
            self._replace(worker, parse_name)
            return None
        except Exception as e:  # noqa: BLE001 - ctypes/shell failure: no icon
            log.debug("icon for %s failed: %s", parse_name, e)
            return None

    def _replace(self, wedged: _StaWorker, app_id: str) -> None:
        with self._lock:
            if self._worker is not wedged:
                return  # another caller already replaced it
            if self._restarts >= MAX_RESTARTS:
                log.warning("shell icons keep hanging (last: %s); icons disabled", app_id)
                self._worker = None
                return
            self._restarts += 1
            log.warning("shell icon for %s hung; starting a fresh icon thread", app_id)
            self._worker = _StaWorker()
