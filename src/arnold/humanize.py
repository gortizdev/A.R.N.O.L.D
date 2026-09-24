"""Formatting helpers.

Two audiences: `*_human` renders for a screen (compact, with digits and unit
abbreviations), `*_speech` renders for Jarvis's text-to-speech (whole words, no
abbreviations, rounded so it does not read out a decimal nobody asked for).
"""

from __future__ import annotations

_UNITS_SHORT = ("B", "KB", "MB", "GB", "TB", "PB")
_UNITS_SPOKEN = ("bytes", "kilobytes", "megabytes", "gigabytes", "terabytes", "petabytes")


def _scale(num: float) -> tuple[float, int]:
    value = float(num)
    index = 0
    while abs(value) >= 1024 and index < len(_UNITS_SHORT) - 1:
        value /= 1024.0
        index += 1
    return value, index


def bytes_human(num: float | None) -> str:
    if num is None:
        return "unknown"
    value, index = _scale(num)
    return f"{value:.0f} {_UNITS_SHORT[index]}" if index == 0 else f"{value:.1f} {_UNITS_SHORT[index]}"


def bytes_speech(num: float | None) -> str:
    if num is None:
        return "an unknown amount"
    value, index = _scale(num)
    if index == 0:
        return f"{value:.0f} byte" if round(value) == 1 else f"{value:.0f} bytes"
    # Below 10 a single decimal carries real information; above it, noise.
    rendered = f"{value:.1f}".rstrip("0").rstrip(".") if value < 10 else f"{value:.0f}"
    unit = _UNITS_SPOKEN[index]
    if rendered == "1":
        unit = unit.removesuffix("s")
    return f"{rendered} {unit}"


def rate_speech(bytes_per_second: float | None) -> str:
    if bytes_per_second is None:
        return "an unknown rate"
    return f"{bytes_speech(bytes_per_second)} per second"


def duration_speech(seconds: float | None) -> str:
    """Render a duration the way a person would say it, to two units."""
    if seconds is None:
        return "an unknown amount of time"
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds} second{'s' if seconds != 1 else ''}"

    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60

    parts: list[str] = []
    if days:
        parts.append(f"{days} day{'s' if days != 1 else ''}")
    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if minutes and len(parts) < 2:
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")

    if not parts:
        return "less than a minute"
    if len(parts) == 1:
        return parts[0]
    return f"{parts[0]} and {parts[1]}"


def clock_speech(ts: float | None) -> str:
    """A timestamp as a spoken clock time: '2:30 PM', or '3 PM' on the hour.

    Deliberately not 'half past two': a 12-hour clock with an explicit meridiem
    is unambiguous read aloud, which matters when the answer is when to be
    somewhere.
    """
    if ts is None:
        return "an unknown time"
    from datetime import datetime

    when = datetime.fromtimestamp(ts)
    hour = when.hour % 12 or 12
    meridiem = "AM" if when.hour < 12 else "PM"
    return f"{hour} {meridiem}" if when.minute == 0 else f"{hour}:{when.minute:02d} {meridiem}"


def count_speech(count: int, singular: str, plural: str = "") -> str:
    """'1 message' / '4 messages', with the number kept as a digit for TTS."""
    if count == 1:
        return f"1 {singular}"
    return f"{count} {plural or singular + 's'}"


def percent_speech(value: float | None) -> str:
    if value is None:
        return "an unknown percentage"
    return f"{value:.0f} percent"


def join_speech(items: list[str], conjunction: str = "and") -> str:
    """Oxford-comma-free list suitable for reading aloud."""
    items = [i for i in items if i]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} {conjunction} {items[1]}"
    return f"{', '.join(items[:-1])}, {conjunction} {items[-1]}"


def drive_speech(letter: str) -> str:
    """'C:' -> 'drive C' so TTS does not read the colon."""
    clean = letter.rstrip(":\\/")
    return f"drive {clean}" if len(clean) == 1 else clean


def sentence_case(text: str) -> str:
    """Capitalise the first character only.

    `str.capitalize()` lowercases everything after it, which would turn
    "drive C has 91 gigabytes free" into "Drive c has ...".
    """
    return text[:1].upper() + text[1:] if text else text
