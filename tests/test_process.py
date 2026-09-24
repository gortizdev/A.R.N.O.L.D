"""Child processes must not flash a console window.

The agent runs under pythonw.exe, which has no console, so on Windows any
console child gets a brand new window unless CREATE_NO_WINDOW is set. The
wake-word claim runs ssh every 35 seconds, which made this very visible.

The source scan matters more than the unit test: a missing flag is invisible
while developing, because a child launched from a terminal inherits that
console instead of creating one.
"""

import ast
import subprocess
from pathlib import Path

import pytest

from arnold import process

SRC = Path(__file__).resolve().parent.parent / "src" / "arnold"


def test_no_window_flag_is_applied(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        subprocess, "run", lambda argv, **kw: seen.update(kw) or "ok"
    )
    process.run(["whoami"])
    assert seen["creationflags"] & process.NO_WINDOW == process.NO_WINDOW


def test_output_is_captured_as_text_by_default(monkeypatch):
    seen = {}
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: seen.update(kw))
    process.run(["whoami"])
    assert seen["capture_output"] is True
    assert seen["text"] is True


def test_caller_flags_are_kept(monkeypatch):
    seen = {}
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: seen.update(kw))
    process.run(["whoami"], creationflags=0x00000200)  # CREATE_NEW_PROCESS_GROUP
    assert seen["creationflags"] & 0x00000200
    assert seen["creationflags"] & process.NO_WINDOW


def test_caller_can_override_capture(monkeypatch):
    seen = {}
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: seen.update(kw))
    process.run(["whoami"], capture_output=False)
    assert seen["capture_output"] is False


def _spawn_calls(path: Path):
    """Every subprocess.<spawn>(...) call in a file, with line numbers."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        value = node.func.value
        if isinstance(value, ast.Name) and value.id == "subprocess":
            if node.func.attr in ("run", "Popen", "call", "check_output", "check_call"):
                yield node


@pytest.mark.parametrize(
    "path", sorted(SRC.rglob("*.py")), ids=lambda p: p.name
)
def test_modules_spawn_through_the_helper(path):
    """Nothing outside process.py starts a child process directly.

    process.run hides the console; process.launch detaches an application that
    is meant to have a window of its own. Between them there is no reason to
    reach for subprocess, and reaching for it is how the two ssh calls ended up
    flashing a window every 35 seconds.
    """
    if path.name == "process.py":
        return
    offenders = [f"{path.name}:{n.lineno}" for n in _spawn_calls(path)]
    assert not offenders, (
        f"spawns a process directly: {offenders}. Use arnold.process.run "
        "(hidden) or .launch (detached, owns its window) instead."
    )


def test_launch_detaches_without_asking_for_a_console():
    """The flags are mutually exclusive - launch must not pick up NO_WINDOW."""
    assert process.DETACHED
    assert not (process.DETACHED & process.NO_WINDOW)
