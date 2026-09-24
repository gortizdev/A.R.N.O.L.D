"""Installing the assistant's hooks into Claude Code's settings.

Claude Code reads ``~/.claude/settings.json`` in every session, so one entry
there covers the desktop app, VS Code and the terminal alike. The entries
added here all run the same small script (``claude_hook.py``); they are
recognised again by that, so `remove` takes out exactly what `install` put in
and leaves any hooks of the user's own alone.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any

MARKER = "arnold.claude_hook"

# The events worth a process start. Not the per-tool ones: the transcript
# already shows every tool call, and a hook on each would tax the session.
EVENTS = ("SessionStart", "UserPromptSubmit", "Notification", "Stop", "SessionEnd")


def settings_path(home: Path | None = None) -> Path:
    from .monitors.claude import claude_home

    return (home or claude_home()) / "settings.json"


def hook_command(events_file: Path, python: str | None = None) -> str:
    """The command line Claude Code will run.

    pythonw would be the natural choice for a windowless process, but it does
    not reliably get stdin; python.exe does, and Claude Code starts hooks
    without a console so nothing flashes.
    """
    exe = python or sys.executable
    if exe.lower().endswith("pythonw.exe"):
        exe = exe[:-11] + "python.exe"
    return f'"{exe}" -m {MARKER} "{events_file}"'


def _load(path: Path) -> dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"{path} is not readable JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"{path} does not hold a JSON object")
    return data


def _save(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        stamp = time.strftime("%Y%m%d-%H%M%S")
        shutil.copy2(path, path.with_name(f"{path.name}.{stamp}.bak"))
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def _ours(entry: Any) -> bool:
    if not isinstance(entry, dict):
        return False
    for hook in entry.get("hooks") or []:
        if isinstance(hook, dict) and MARKER in str(hook.get("command") or ""):
            return True
    return False


def status(path: Path | None = None) -> dict[str, Any]:
    path = path or settings_path()
    try:
        data = _load(path)
    except RuntimeError as exc:
        return {"installed": False, "path": str(path), "error": str(exc), "events": []}
    hooks = data.get("hooks") or {}
    present = [event for event, entries in hooks.items() if isinstance(entries, list) and any(_ours(e) for e in entries)]
    command = ""
    for entries in hooks.values():
        for entry in entries if isinstance(entries, list) else []:
            if _ours(entry):
                command = str(entry["hooks"][0].get("command") or "")
                break
        if command:
            break
    return {
        "installed": bool(present),
        "path": str(path),
        "events": sorted(present),
        "missing": sorted(set(EVENTS) - set(present)),
        "command": command,
    }


def install(events_file: Path, path: Path | None = None, python: str | None = None) -> dict[str, Any]:
    path = path or settings_path()
    data = _load(path)
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
    command = hook_command(events_file, python)
    changed = False
    for event in EVENTS:
        entries = hooks.get(event)
        if not isinstance(entries, list):
            entries = []
        kept = [e for e in entries if not _ours(e)]
        entry = {"hooks": [{"type": "command", "command": command, "timeout": 5}]}
        wanted = kept + [entry]
        if wanted != entries:
            changed = True
        hooks[event] = wanted
    data["hooks"] = hooks
    if changed:
        _save(path, data)
    return {"installed": True, "changed": changed, "path": str(path), "events": list(EVENTS), "command": command}


def remove(path: Path | None = None) -> dict[str, Any]:
    path = path or settings_path()
    data = _load(path)
    hooks = data.get("hooks")
    removed: list[str] = []
    if isinstance(hooks, dict):
        for event, entries in list(hooks.items()):
            if not isinstance(entries, list):
                continue
            kept = [e for e in entries if not _ours(e)]
            if len(kept) != len(entries):
                removed.append(event)
            if kept:
                hooks[event] = kept
            else:
                del hooks[event]
        if not hooks:
            del data["hooks"]
    if removed:
        _save(path, data)
    return {"installed": False, "removed": sorted(removed), "path": str(path)}
