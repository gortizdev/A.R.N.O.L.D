"""The time and the date, read off this PC's clock.

The voice model has no clock of its own and will happily guess, so "what
time is it" has to be a command that looks.
"""

from __future__ import annotations

import time
from datetime import datetime

from ..humanize import clock_speech
from .registry import CommandContext, CommandResult, Registry


def register_all(registry: Registry) -> None:
    registry.register("clock.now", _now, "The current time, date and day of the week.")


def _ordinal(day: int) -> str:
    if 11 <= day % 100 <= 13:
        return f"{day}th"
    return f"{day}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(day % 10, 'th') }"


def date_speech(when: datetime) -> str:
    """'Wednesday, September 23rd, 2026'."""
    return f"{when:%A}, {when:%B} {_ordinal(when.day)}, {when.year}"


def now_sentence(now: datetime | None = None) -> str:
    """One line of context for a prompt: what the clock says right now."""
    now = (now or datetime.now()).astimezone()
    return f"{clock_speech(now.timestamp())} on {date_speech(now)} ({now.tzname()})"


def _now(ctx: CommandContext, args: dict) -> CommandResult:
    now = datetime.now().astimezone()
    offset = now.utcoffset()
    week = now.isocalendar().week
    return CommandResult(
        speech=f"It's {clock_speech(now.timestamp())} on {date_speech(now)}.",
        result={
            "iso": now.isoformat(timespec="seconds"),
            "time": now.strftime("%H:%M"),
            "date": now.date().isoformat(),
            "weekday": now.strftime("%A"),
            "week_of_year": week,
            "timezone": now.tzname() or time.tzname[0],
            "utc_offset_minutes": int(offset.total_seconds() // 60) if offset else 0,
        },
    )
