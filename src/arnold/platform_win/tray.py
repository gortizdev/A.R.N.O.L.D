"""A notification-area ("system tray") icon via Shell_NotifyIcon.

ctypes rather than pystray for the same reason the face uses tkinter: the
standard library already reaches everything this needs, and a tray icon is
one hidden window, one Shell_NotifyIcon call, and a message loop.

The icon lives on its own daemon thread because a tray window must pump
messages, and the face's Tk mainloop owns the main thread. Everything the
user does with the icon (click, menu choice) therefore arrives *on the tray
thread* - callers that own a GUI must marshal the callbacks back themselves.
`update()` may be called from any thread.
"""

from __future__ import annotations

import ctypes
import logging
import threading
from ctypes import wintypes
from typing import Callable

from PIL import Image

from . import IS_WINDOWS

log = logging.getLogger(__name__)

# Callback message the shell sends our hidden window for mouse events.
_WM_TRAY = 0x8000 + 1  # WM_APP + 1

_WM_DESTROY = 0x0002
_WM_CLOSE = 0x0010
_WM_LBUTTONUP = 0x0202
_WM_LBUTTONDBLCLK = 0x0203
_WM_RBUTTONUP = 0x0205

_NIM_ADD, _NIM_MODIFY, _NIM_DELETE = 0, 1, 2
_NIF_MESSAGE, _NIF_ICON, _NIF_TIP = 0x1, 0x2, 0x4

_MF_STRING = 0x0
_MF_GRAYED = 0x1
_MF_SEPARATOR = 0x800
_TPM_RIGHTBUTTON = 0x2
_TPM_RETURNCMD = 0x100

_SM_CXSMICON = 49

# First command id in the popup menu; anything below is never a menu choice.
_MENU_BASE = 1024

_LRESULT = wintypes.LPARAM
_WNDPROC = ctypes.WINFUNCTYPE(
    _LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
)

if IS_WINDOWS:
    # Private handles with real prototypes: the default c_int restype truncates
    # 64-bit handles, and mutating the shared ctypes.windll cache would leak
    # these prototypes into every other module.
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    _shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE
    _kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    _user32.CreateWindowExW.restype = wintypes.HWND
    _user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
    ]
    _user32.PostMessageW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
    ]
    _user32.DestroyWindow.argtypes = [wintypes.HWND]
    _user32.DefWindowProcW.restype = _LRESULT
    _user32.DefWindowProcW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
    ]
    _user32.CreateIconIndirect.restype = wintypes.HICON
    _user32.CreatePopupMenu.restype = wintypes.HMENU
    _user32.AppendMenuW.argtypes = [
        wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR
    ]
    _user32.TrackPopupMenu.argtypes = [
        wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, wintypes.HWND, ctypes.c_void_p,
    ]
    _user32.DestroyMenu.argtypes = [wintypes.HMENU]
    _user32.DestroyIcon.argtypes = [wintypes.HICON]
    _gdi32.CreateBitmap.restype = wintypes.HBITMAP
    _gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]

# A menu is a list of (label, callback) rows. callback None = disabled row,
# label "-" = separator.
MenuItems = list[tuple[str, Callable[[], None] | None]]


class _WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", _WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", ctypes.c_void_p),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


class _NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", ctypes.c_byte * 16),
        ("hBalloonIcon", wintypes.HICON),
    ]


class _ICONINFO(ctypes.Structure):
    _fields_ = [
        ("fIcon", wintypes.BOOL),
        ("xHotspot", wintypes.DWORD),
        ("yHotspot", wintypes.DWORD),
        ("hbmMask", wintypes.HBITMAP),
        ("hbmColor", wintypes.HBITMAP),
    ]


def _icon_from_image(image: Image.Image) -> int:
    """PIL image -> HICON, at the shell's small-icon size.

    A 32-bit bitmap carries the alpha channel, so the mask bitmap is only a
    formality CreateIconIndirect insists on.
    """
    side = max(16, _user32.GetSystemMetrics(_SM_CXSMICON))
    img = image.convert("RGBA")
    if img.size != (side, side):
        img = img.resize((side, side), Image.LANCZOS)

    bgra = img.tobytes("raw", "BGRA")
    color = _gdi32.CreateBitmap(side, side, 1, 32, bgra)
    mask = _gdi32.CreateBitmap(side, side, 1, 1, None)
    info = _ICONINFO(True, 0, 0, mask, color)
    hicon = _user32.CreateIconIndirect(ctypes.byref(info))
    _gdi32.DeleteObject(color)
    _gdi32.DeleteObject(mask)
    if not hicon:
        raise OSError("CreateIconIndirect failed")
    return hicon


class TrayIcon:
    """One icon in the notification area, with a tooltip and a popup menu.

    `on_click` fires on a left click; `menu` is called each time the user
    right-clicks, and returns the rows to show - so labels can reflect
    current state without anyone keeping a menu in sync.
    """

    def __init__(
        self,
        tooltip: str,
        image: Image.Image,
        on_click,
        menu,
    ) -> None:
        self._tooltip = tooltip
        self._image = image
        self._on_click = on_click
        self._menu = menu

        self._hwnd: int | None = None
        self._hicon: int = 0
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._lock = threading.Lock()
        # Keep the WNDPROC thunk referenced, or it is collected while Windows
        # still holds the pointer.
        self._wndproc = _WNDPROC(self._wnd_proc)

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> bool:
        """Create the icon. Returns False when the tray is unavailable."""
        if not IS_WINDOWS:
            return False
        self._thread = threading.Thread(target=self._run, name="tray", daemon=True)
        self._thread.start()
        self._ready.wait(5.0)
        return self._hwnd is not None

    def stop(self) -> None:
        hwnd = self._hwnd
        if hwnd is None:
            return
        try:
            _user32.PostMessageW(hwnd, _WM_CLOSE, 0, 0)
        except Exception:
            pass
        if self._thread is not None:
            self._thread.join(2.0)

    def update(self, image: Image.Image | None = None, tooltip: str | None = None) -> None:
        """Swap the icon and/or tooltip in place. Safe from any thread."""
        if self._hwnd is None:
            return
        with self._lock:
            old = 0
            if image is not None:
                self._image = image
                old, self._hicon = self._hicon, _icon_from_image(image)
            if tooltip is not None:
                self._tooltip = tooltip
            self._notify(_NIM_MODIFY)
            if old:
                _user32.DestroyIcon(old)

    # -- the tray thread -----------------------------------------------------

    def _run(self) -> None:
        user32 = _user32
        try:
            wc = _WNDCLASSW()
            wc.lpfnWndProc = self._wndproc
            wc.hInstance = _kernel32.GetModuleHandleW(None)
            wc.lpszClassName = "ArnoldTray"
            user32.RegisterClassW(ctypes.byref(wc))

            hwnd = user32.CreateWindowExW(
                0, wc.lpszClassName, "arnold tray",
                0, 0, 0, 0, 0, None, None, wc.hInstance, None,
            )
            if not hwnd:
                log.warning("tray window could not be created")
                return
            self._hwnd = hwnd

            # The shell resends this when explorer.exe restarts; the icon
            # must be added again or it silently disappears.
            self._taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")

            with self._lock:
                self._hicon = _icon_from_image(self._image)
                self._notify(_NIM_ADD)
        except Exception as exc:
            log.warning("tray icon unavailable: %s", exc)
            self._hwnd = None
            return
        finally:
            self._ready.set()

        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        with self._lock:
            self._notify(_NIM_DELETE)
            if self._hicon:
                user32.DestroyIcon(self._hicon)
                self._hicon = 0
        self._hwnd = None

    def _notify(self, action: int) -> None:
        data = _NOTIFYICONDATAW()
        data.cbSize = ctypes.sizeof(data)
        data.hWnd = self._hwnd
        data.uID = 1
        data.uFlags = _NIF_MESSAGE | _NIF_ICON | _NIF_TIP
        data.uCallbackMessage = _WM_TRAY
        data.hIcon = self._hicon
        data.szTip = self._tooltip[:127]
        if not _shell32.Shell_NotifyIconW(action, ctypes.byref(data)):
            log.debug("Shell_NotifyIcon(%d) failed", action)

    def _wnd_proc(self, hwnd, message, wparam, lparam):
        user32 = _user32
        if message == _WM_TRAY:
            if lparam in (_WM_LBUTTONUP, _WM_LBUTTONDBLCLK):
                self._safe(self._on_click)
            elif lparam == _WM_RBUTTONUP:
                self._popup(hwnd)
            return 0
        if message == getattr(self, "_taskbar_created", None):
            with self._lock:
                self._notify(_NIM_ADD)
            return 0
        if message == _WM_CLOSE:
            user32.DestroyWindow(hwnd)
            return 0
        if message == _WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, message, wparam, lparam)

    def _popup(self, hwnd) -> None:
        user32 = _user32
        try:
            items: MenuItems = self._menu()
        except Exception as exc:
            log.debug("tray menu provider failed: %s", exc)
            return

        menu = user32.CreatePopupMenu()
        callbacks: dict[int, object] = {}
        for i, (label, callback) in enumerate(items):
            if label == "-":
                user32.AppendMenuW(menu, _MF_SEPARATOR, 0, None)
            elif callback is None:
                user32.AppendMenuW(menu, _MF_STRING | _MF_GRAYED, 0, label)
            else:
                cmd = _MENU_BASE + i
                callbacks[cmd] = callback
                user32.AppendMenuW(menu, _MF_STRING, cmd, label)

        point = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(point))
        # Without this the menu refuses to dismiss when the user clicks away -
        # a documented quirk of TrackPopupMenu from a non-foreground window.
        user32.SetForegroundWindow(hwnd)
        chosen = user32.TrackPopupMenu(
            menu,
            _TPM_RIGHTBUTTON | _TPM_RETURNCMD,
            point.x, point.y, 0, hwnd, None,
        )
        user32.PostMessageW(hwnd, 0, 0, 0)  # WM_NULL, same quirk
        user32.DestroyMenu(menu)

        if chosen in callbacks:
            self._safe(callbacks[chosen])

    @staticmethod
    def _safe(callback) -> None:
        try:
            callback()
        except Exception as exc:
            log.error("tray callback failed: %s", exc)
