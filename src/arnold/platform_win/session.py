"""Which Windows session are we in, and can we touch the screen?

Windows isolates sessions. A command arriving over SSH runs in session 0,
which has no visible desktop: a browser launched from there opens where nobody
can see it, a toast is never shown, and the clipboard is a different clipboard
from the user's. The logged-on desktop is a separate session (typically 1 or
higher).

This is the same wall Jarvis hits on the Pi - its screenshot code notes that
"CopyFromScreen is useless in SSH session 0" and works around it with a
scheduled task. Rather than repeat that trick per command, the CLI asks here
whether it can reach the desktop, and hands the work to the agent already
running in the logon session when it cannot.
"""

from __future__ import annotations

import logging
import os

from . import IS_WINDOWS

log = logging.getLogger(__name__)


def session_id(pid: int | None = None) -> int:
    """Windows session of a process, or -1 if it cannot be determined."""
    if not IS_WINDOWS:
        return -1
    import ctypes

    value = ctypes.c_ulong()
    target = os.getpid() if pid is None else pid
    if not ctypes.windll.kernel32.ProcessIdToSessionId(target, ctypes.byref(value)):
        return -1
    return int(value.value)


def console_session_id() -> int:
    """Session attached to the physical screen and keyboard, or -1."""
    if not IS_WINDOWS:
        return -1
    import ctypes

    result = ctypes.windll.kernel32.WTSGetActiveConsoleSessionId()
    # 0xFFFFFFFF means no session is attached - nobody is logged on.
    return -1 if result == 0xFFFFFFFF else int(result)


def has_interactive_desktop() -> bool:
    """True when anything drawn on screen from this process would be visible.

    Session 0 is never interactive on modern Windows, and a session that is
    not the console session belongs to a different (or disconnected) login.
    """
    if not IS_WINDOWS:
        return True  # nothing to work around off Windows
    mine = session_id()
    if mine <= 0:
        return False
    return mine == console_session_id()


def describe() -> dict[str, object]:
    """For diagnostics - `arnold diag` shows this."""
    return {
        "session": session_id(),
        "console_session": console_session_id(),
        "interactive": has_interactive_desktop(),
    }
