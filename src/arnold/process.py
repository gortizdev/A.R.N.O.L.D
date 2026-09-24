"""Child processes that never flash a console window.

The agent runs under pythonw.exe, which has no console of its own. On Windows
a console child launched from a console-less parent is given a brand new
console window, which appears on screen for as long as the command runs.
Nearly everything here shells out - ssh, powershell, nvidia-smi, shutdown -
and the wake-word claim alone runs ssh every 35 seconds, so without
CREATE_NO_WINDOW the desktop flashes a black box all day.

One helper rather than the flag repeated at each call site: it is easy to
forget, and forgetting it is invisible while developing, because a child
launched from a terminal quietly inherits that console instead of creating
one.
"""

from __future__ import annotations

import subprocess
from typing import Any

# Absent off Windows, where a child does not get a console of its own anyway.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def run(argv, **kwargs: Any) -> subprocess.CompletedProcess:
    """subprocess.run, with output captured as text and no console window.

    Not for children that are *meant* to have a window of their own - starting
    an application, say. CREATE_NO_WINDOW cannot be combined with
    DETACHED_PROCESS or CREATE_NEW_CONSOLE.
    """
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    kwargs["creationflags"] = kwargs.get("creationflags", 0) | NO_WINDOW
    return subprocess.run(argv, **kwargs)


# Detached, so the child outlives us and does not die with the agent.
DETACHED = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
    subprocess, "CREATE_NEW_PROCESS_GROUP", 0
)


def launch(argv, cwd: str | None = None) -> subprocess.Popen:
    """Start an application that should own its own window and outlive us.

    DETACHED_PROCESS rather than NO_WINDOW: the child must not inherit our
    console, but it may put a window on screen - it is something the user
    asked to see. The two flags cannot be combined.
    """
    return subprocess.Popen(argv, cwd=cwd, creationflags=DETACHED, close_fds=True)


def launch_quiet(
    argv,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    output: Any = None,
) -> subprocess.Popen:
    """Start a console program that must outlive us and show nothing.

    For a job handed off and forgotten - a headless Claude Code turn, say.
    DETACHED_PROCESS gives a console child no console at all, so nothing
    appears, and it is not tied to our lifetime. `output` is a file object
    or descriptor for stdout and stderr; nothing means discarded.
    """
    return subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=output if output is not None else subprocess.DEVNULL,
        stderr=subprocess.STDOUT if output is not None else subprocess.DEVNULL,
        creationflags=DETACHED,
        close_fds=True,
    )
