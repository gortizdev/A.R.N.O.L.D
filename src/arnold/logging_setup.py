"""Logging configuration."""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

_FORMAT = "%(asctime)s %(levelname)-7s %(name)-28s %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"

_SECRET_KEYS = ("password", "token", "secret", "sig")


class RedactFilter(logging.Filter):
    """Best-effort scrub of secrets that slip into log messages."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = str(record.msg)
        low = msg.lower()
        if any(k in low for k in _SECRET_KEYS):
            record.msg = _redact(msg)
            record.args = ()
        return True


def _redact(text: str) -> str:
    import re

    pattern = re.compile(
        r"((?:password|token|secret|sig)\"?\s*[:=]\s*\"?)([^\s,\"}\]]+)", re.IGNORECASE
    )
    return pattern.sub(lambda m: m.group(1) + "***", text)


class TolerantRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """A rotating handler that keeps writing when it cannot rotate.

    On Windows a file cannot be renamed while any process has it open, and
    the agent, the face and the voice session all append to the same log.
    The stock handler closes its stream, fails the rename, and then fails it
    again on every record for ever after - so the log simply stops at the
    size cap, which is the worst possible moment for it to go quiet. This one
    reopens the file and carries on past the cap, and tries the rotation
    again later, when the others may have let go.
    """

    RETRY_SECONDS = 300.0

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._rotate_failed_at = 0.0

    def shouldRollover(self, record: logging.LogRecord) -> int:  # noqa: N802
        import time

        if self._rotate_failed_at and time.monotonic() - self._rotate_failed_at < self.RETRY_SECONDS:
            return 0
        return super().shouldRollover(record)

    def doRollover(self) -> None:  # noqa: N802
        import time

        try:
            super().doRollover()
            self._rotate_failed_at = 0.0
        except OSError:
            self._rotate_failed_at = time.monotonic()
            if self.stream is None:
                self.stream = self._open()


def setup_logging(level: str = "INFO", log_file: str = "") -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter = logging.Formatter(_FORMAT, datefmt=_DATEFMT)

    # Under pythonw.exe (used to run the agent without a console window) there
    # is no stderr at all, and attaching a handler to None raises on first emit.
    if sys.stderr is not None:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(formatter)
        stream.addFilter(RedactFilter())
        root.addHandler(stream)

    if log_file:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        rotating = TolerantRotatingFileHandler(
            path, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
        )
        rotating.setFormatter(formatter)
        rotating.addFilter(RedactFilter())
        root.addHandler(rotating)

    # paho is chatty at DEBUG and adds nothing at that level.
    logging.getLogger("paho").setLevel(logging.WARNING)
