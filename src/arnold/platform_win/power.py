"""Power state: lock, sleep, hibernate, shutdown, restart, log off.

Shutdown and restart go through `shutdown.exe` with a delay so a mis-heard
voice command can be taken back with `control.cancel_shutdown` before anything
is lost.
"""

from __future__ import annotations

import ctypes
import logging
import subprocess

from .. import process
from . import require_windows

log = logging.getLogger(__name__)

DEFAULT_DELAY_SECONDS = 30


def _run(args: list[str]) -> None:
    proc = process.run(args, timeout=20)
    if proc.returncode != 0:
        raise RuntimeError(
            f"{args[0]} failed (exit {proc.returncode}): {(proc.stderr or proc.stdout).strip()[:300]}"
        )


def lock() -> None:
    require_windows("workstation lock")
    if not ctypes.windll.user32.LockWorkStation():  # type: ignore[attr-defined]
        raise RuntimeError("LockWorkStation refused (no interactive session?)")


def sleep() -> None:
    require_windows("sleep")
    # SetSuspendState(Hibernate=False, Force=False, WakeupEventsDisabled=False)
    if not ctypes.windll.powrprof.SetSuspendState(False, False, False):  # type: ignore[attr-defined]
        raise RuntimeError("SetSuspendState refused - sleep may be disabled in power settings")


def hibernate() -> None:
    require_windows("hibernate")
    if not ctypes.windll.powrprof.SetSuspendState(True, False, False):  # type: ignore[attr-defined]
        raise RuntimeError("hibernate refused - run `powercfg /hibernate on` to enable it")


def shutdown(delay_seconds: int = DEFAULT_DELAY_SECONDS, message: str = "") -> int:
    require_windows("shutdown")
    delay = max(0, int(delay_seconds))
    args = ["shutdown.exe", "/s", "/t", str(delay)]
    if message:
        args += ["/c", message[:500]]
    _run(args)
    return delay


def restart(delay_seconds: int = DEFAULT_DELAY_SECONDS, message: str = "") -> int:
    require_windows("restart")
    delay = max(0, int(delay_seconds))
    args = ["shutdown.exe", "/r", "/t", str(delay)]
    if message:
        args += ["/c", message[:500]]
    _run(args)
    return delay


def logoff() -> None:
    require_windows("log off")
    _run(["shutdown.exe", "/l"])


def cancel_shutdown() -> None:
    require_windows("cancel shutdown")
    try:
        _run(["shutdown.exe", "/a"])
    except RuntimeError as exc:
        # 1116 = ERROR_NO_SHUTDOWN_IN_PROGRESS; not worth surfacing as a failure.
        if "1116" in str(exc):
            raise RuntimeError("there is no pending shutdown to cancel") from exc
        raise
