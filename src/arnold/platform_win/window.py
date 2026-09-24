"""Foreground-window inspection via user32."""

from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes

from . import IS_WINDOWS

log = logging.getLogger(__name__)


def cursor_position() -> tuple[int, int] | None:
    """Mouse position in screen coordinates, or None if it cannot be read.

    Polled rather than bound to an event: a colour-keyed window only receives
    mouse messages over its own opaque pixels, so the face would otherwise
    only notice the pointer once it was already on top of it.
    """
    if not IS_WINDOWS:
        return None
    try:
        point = wintypes.POINT()
        if not ctypes.windll.user32.GetCursorPos(ctypes.byref(point)):
            return None
        return int(point.x), int(point.y)
    except Exception as exc:
        log.debug("GetCursorPos failed: %s", exc)
        return None


def idle_seconds() -> float | None:
    """How long since the last keyboard or mouse input, or None off Windows.

    GetLastInputInfo is session-wide and costs nothing, which is what makes it
    usable from the agent's tick. It measures input, not attention: a film
    counts as idle, which is the right answer for "have they had a break".
    """
    if not IS_WINDOWS:
        return None

    class _LastInput(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

    try:
        info = _LastInput()
        info.cbSize = ctypes.sizeof(_LastInput)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return None
        # Both are millisecond tick counts that wrap after 49 days; the
        # subtraction is wrap-safe as long as it is masked back to 32 bits.
        elapsed = (ctypes.windll.kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF
        return elapsed / 1000.0
    except Exception as exc:
        log.debug("GetLastInputInfo failed: %s", exc)
        return None


def foreground_hwnd() -> int:
    """The window in front, as a handle. 0 when there is none.

    Shared with uia.py, so both are looking at the same window even if the
    user alt-tabs between two calls.
    """
    if not IS_WINDOWS:
        return 0
    try:
        return int(ctypes.windll.user32.GetForegroundWindow() or 0)
    except Exception as exc:
        log.debug("GetForegroundWindow failed: %s", exc)
        return 0


def active_window() -> dict[str, object]:
    """Title and owning process of the foreground window.

    Returns a dict with `title`, `process`, `pid`. Values are None when there is
    no foreground window (locked session, or running headless).
    """
    empty: dict[str, object] = {"title": None, "process": None, "pid": None}
    if not IS_WINDOWS:
        return empty

    try:
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        hwnd = foreground_hwnd()
        if not hwnd:
            return empty

        length = user32.GetWindowTextLengthW(hwnd)
        title = ""
        if length > 0:
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value

        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        pid_value = int(pid.value) or None

        process_name = None
        if pid_value:
            try:
                import psutil

                process_name = psutil.Process(pid_value).name()
            except Exception:
                process_name = None

        return {"title": title or None, "process": process_name, "pid": pid_value}
    except Exception as exc:
        log.debug("active_window failed: %s", exc)
        return empty
