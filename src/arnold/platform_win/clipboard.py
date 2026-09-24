"""Clipboard read/write."""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

MAX_READ_CHARS = 10_000


def read_clipboard(max_chars: int = MAX_READ_CHARS) -> tuple[str, bool]:
    """Return (text, truncated). Non-text clipboard contents come back as ''."""
    import pyperclip

    try:
        text = pyperclip.paste() or ""
    except Exception as exc:  # pyperclip raises its own exception types
        raise RuntimeError(f"could not read clipboard: {exc}") from exc
    if len(text) > max_chars:
        return text[:max_chars], True
    return text, False


def write_clipboard(text: str) -> None:
    import pyperclip

    try:
        pyperclip.copy(text)
    except Exception as exc:
        raise RuntimeError(f"could not write clipboard: {exc}") from exc
