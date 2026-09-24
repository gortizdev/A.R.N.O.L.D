"""The hook Claude Code runs to tell the assistant what a session is doing.

Installed into ``~/.claude/settings.json`` by ``arnold claude hooks
install``; every Claude Code session on this machine - desktop, VS Code,
terminal - then runs it on a handful of events. It reads the event from stdin
and appends one line to the events file the watch tails. Nothing else: it runs
inside someone's coding session, so it must be quick and must never fail loudly.

Only the standard library is imported, on purpose. Pulling the rest of the
package in would add a third of a second to every event.
"""

from __future__ import annotations

import json
import os
import sys
import time

# What is kept from each event. The prompt itself is deliberately not: the
# transcript has it, and this file should not be a second copy of what the
# user typed.
_FIELDS = ("session_id", "transcript_path", "cwd", "hook_event_name", "notification_type", "message", "title", "stop_hook_active", "source", "reason")

MAX_BYTES = 4 * 1024 * 1024


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        return 0
    target = argv[0]
    try:
        raw = sys.stdin.read()
    except Exception:
        raw = ""
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    row = {"ts": time.time(), "event": payload.get("hook_event_name") or (argv[1] if len(argv) > 1 else "")}
    for key in _FIELDS:
        value = payload.get(key)
        if value is None:
            continue
        if isinstance(value, str) and len(value) > 500:
            value = value[:500]
        row[key] = value

    try:
        folder = os.path.dirname(target)
        if folder:
            os.makedirs(folder, exist_ok=True)
        # A runaway file is truncated rather than grown for ever: the watch
        # only ever wants what is new.
        try:
            if os.path.getsize(target) > MAX_BYTES:
                os.replace(target, target + ".1")
        except OSError:
            pass
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
