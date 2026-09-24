"""Bringing a window to the front.

Windows deliberately refuses `SetForegroundWindow` from a process that is not
already in the foreground - it is what stops background programs stealing
focus mid-sentence. An assistant asked out loud to put something on screen is
the legitimate case, so this uses the two supported ways through:

* `AllowSetForegroundWindow` before launching, which hands our foreground
  privilege to the child. This is the clean path: the browser raises itself.
* Attaching to the foreground window's input queue, which makes us "the
  foreground thread" for as long as it takes to call SetForegroundWindow.
  Needed when the target is an *already running* browser that only opens a new
  tab, so no new process is ever launched to inherit the privilege.

Neither is a trick that can force focus against the user's wishes; both fail
closed, leaving the window flashing in the taskbar.
"""

from __future__ import annotations

import logging
import time

from . import IS_WINDOWS

log = logging.getLogger(__name__)

ASFW_ANY = -1
SW_RESTORE = 9
SW_SHOW = 5


def allow_foreground() -> None:
    """Let the next process we start take the foreground."""
    if not IS_WINDOWS:
        return
    import ctypes

    try:
        ctypes.windll.user32.AllowSetForegroundWindow(ASFW_ANY)
    except Exception as exc:  # pragma: no cover - depends on the OS build
        log.debug("AllowSetForegroundWindow failed: %s", exc)


def _visible_windows_of(names: set[str]) -> list[int]:
    """Top-level visible window handles belonging to the named processes."""
    import ctypes
    from ctypes import wintypes

    import psutil

    user32 = ctypes.windll.user32
    handles: list[int] = []
    proto = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def each(hwnd, _param):
        if not user32.IsWindowVisible(hwnd):
            return True
        if user32.GetWindowTextLengthW(hwnd) == 0:
            return True  # tool windows and hidden helpers
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        try:
            if psutil.Process(pid.value).name().lower() in names:
                handles.append(int(hwnd))
        except Exception:
            pass
        return True

    user32.EnumWindows(proto(each), 0)
    return handles


def bring_to_front(hwnd: int) -> bool:
    """Restore and raise one window. False if Windows declined."""
    if not IS_WINDOWS:
        return False
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    try:
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
        else:
            user32.ShowWindow(hwnd, SW_SHOW)

        if user32.SetForegroundWindow(hwnd):
            return True

        # Refused: borrow the current foreground thread's input queue, which
        # makes this thread eligible to set the foreground window.
        target = user32.GetForegroundWindow()
        if not target:
            return False
        ours = kernel32.GetCurrentThreadId()
        theirs = user32.GetWindowThreadProcessId(target, ctypes.byref(wintypes.DWORD()))
        if theirs == ours:
            return False

        if not user32.AttachThreadInput(theirs, ours, True):
            return False
        try:
            user32.BringWindowToTop(hwnd)
            ok = bool(user32.SetForegroundWindow(hwnd))
        finally:
            user32.AttachThreadInput(theirs, ours, False)
        return ok
    except Exception as exc:  # pragma: no cover
        log.debug("bring_to_front failed: %s", exc)
        return False


def raise_process_windows(
    process_names: set[str], *, timeout: float = 4.0, settle: float = 0.35
) -> bool:
    """Wait for one of these processes to show a window, then raise it.

    A browser that is already running does not create a new process when it is
    handed a URL - it opens a tab in the existing window - so this polls for a
    window rather than for a process, and raises the most recently created one
    it can find.
    """
    if not IS_WINDOWS:
        return False
    names = {n.lower() for n in process_names}
    # Give the browser a moment to create or update its window first, or we
    # raise the old window before the new tab exists.
    time.sleep(settle)

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        handles = _visible_windows_of(names)
        if handles:
            # Last in Z-order enumeration is the least recently active; the
            # first is the topmost, which is the one just opened or updated.
            for hwnd in handles:
                if bring_to_front(hwnd):
                    return True
        time.sleep(0.2)
    log.debug("no window from %s came forward", ", ".join(sorted(names)))
    return False
