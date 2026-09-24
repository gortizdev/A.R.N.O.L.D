"""Windows-specific integrations.

Every module here imports cleanly on non-Windows (so the test suite and the
Pi-side helpers can import the package), but raises UnsupportedPlatform when a
function that genuinely needs Win32 is called.
"""

from __future__ import annotations

import sys

IS_WINDOWS = sys.platform == "win32"


class UnsupportedPlatform(RuntimeError):
    pass


def require_windows(feature: str) -> None:
    if not IS_WINDOWS:
        raise UnsupportedPlatform(f"{feature} requires Windows (running on {sys.platform})")
