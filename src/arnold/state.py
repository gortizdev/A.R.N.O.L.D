"""Shared runtime state between the long-running agent and one-shot CLI calls.

`arnold exec` runs in a fresh process with its own AlertEngine that
has never evaluated anything, so it cannot know what the running agent is
currently reporting. Since `exec` over SSH is the primary path Jarvis uses, an
answer of "no alerts" while the agent has one firing would be plainly wrong.

The agent writes its state here each tick; one-shot calls read it.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_STATE_FILE = Path("logs/state.json")

# Beyond this the file is treated as stale - the agent is probably not running,
# and reporting its last known alerts as current would be misleading.
MAX_AGE_SECONDS = 120.0


def write_state(path: Path, payload: dict[str, Any]) -> None:
    """Write state atomically so a reader never sees a half-written file."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {**payload, "ts": time.time()}
        # Write to a temp file in the same directory, then replace: os.replace
        # is atomic on Windows and POSIX alike.
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".state-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    except OSError as exc:
        log.debug("could not write state file %s: %s", path, exc)


def read_state(path: Path, max_age: float = MAX_AGE_SECONDS) -> dict[str, Any] | None:
    """Return the agent's last state, or None if missing, stale, or unreadable."""
    try:
        with path.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    ts = payload.get("ts")
    if not isinstance(ts, (int, float)) or (time.time() - ts) > max_age:
        return None
    return payload
