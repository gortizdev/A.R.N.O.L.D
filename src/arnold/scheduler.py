"""Doing something at a time, rather than only when asked.

Everything else in this project happens because someone just spoke. That makes
a very good voice command line and stops short of an assistant: "tell me when
the render finishes", "close Steam at midnight", "brief me at nine" are all
ordinary requests, and none of them can be honoured by a process that only acts
while a person is talking to it.

A job is a command from the registry, or a line to say, plus when. The agent's
tick is the clock - it already runs every few seconds, and a scheduler that
needs its own thread to be a second late is a scheduler with two clocks.

Times are parsed from speech, so this understands what people say ("in twenty
minutes", "at half four", "every day at 9") rather than what a cron table
wants. Anything it cannot parse is refused with a sentence, never guessed at:
a reminder that silently lands at the wrong time is worse than one that was
never set.

**Timers** are the same machinery with two differences, and they earn their own
`kind` rather than their own file: they *ring* when they fire, and they describe
themselves as what is left rather than as a clock time, because "nine minutes
left on the pasta" is the answer to the question and "at 19:42" is not. Sharing
the store means one loader, one thing for the tick to read, and one honest
answer to "what have I got on".
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

MAX_JOBS = 200
# A job whose time passed while the machine was off. Beyond this it is stale -
# a reminder from last Tuesday helps nobody - but a few minutes is just a
# reboot, and should still fire.
LATE_TOLERANCE_SECONDS = 15 * 60

_UNITS = {
    "second": 1, "seconds": 1, "sec": 1, "secs": 1,
    "minute": 60, "minutes": 60, "min": 60, "mins": 60,
    "hour": 3600, "hours": 3600, "hr": 3600, "hrs": 3600,
    "day": 86400, "days": 86400,
    "week": 604800, "weeks": 604800,
}
_WORD_NUMBERS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40,
    "forty-five": 45, "forty five": 45, "sixty": 60, "ninety": 90,
    "half": 30,  # "in half an hour" - handled with the unit below
}
_DAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}

_IN_RE = re.compile(
    r"\bin\s+(?P<count>\d+|[a-z]+(?:[ -]five)?)\s*"
    # "in half AN hour", "in a couple of minutes" - the article sits between
    # the count and the unit, and without this the whole phrase fails to match
    # and is refused as unparseable.
    r"(?:an?\s+)?"
    r"(?P<unit>seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|weeks?)\b"
)
_AT_RE = re.compile(
    r"\bat\s+(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<meridiem>am|pm|a\.m\.|p\.m\.)?\b"
)
_EVERY_RE = re.compile(r"\bevery\s+(?P<what>day|morning|evening|night|hour|week|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b")


class ScheduleError(ValueError):
    """The request could not be turned into a time. The message is spoken."""


def _count_from(text: str) -> int | None:
    text = text.strip().lower()
    if text.isdigit():
        return int(text)
    return _WORD_NUMBERS.get(text)


def parse_when(text: str, *, now: float | None = None) -> tuple[float, str]:
    """Turn a spoken time into (timestamp, how it will be repeated).

    Returns the first firing and a repeat rule: "" for one-shot, else "daily",
    "hourly", "weekly", or "weekly:<0-6>".
    """
    now = time.time() if now is None else now
    said = (text or "").strip().lower()
    if not said:
        raise ScheduleError("Tell me when.")

    base = datetime.fromtimestamp(now)
    repeat = ""

    every = _EVERY_RE.search(said)
    if every:
        what = every.group("what")
        if what == "hour":
            return now + 3600, "hourly"
        if what == "week":
            repeat = "weekly"
        elif what in _DAYS:
            repeat = f"weekly:{_DAYS[what]}"
        else:
            repeat = "daily"

    relative = _IN_RE.search(said)
    if relative and not every:
        count = _count_from(relative.group("count"))
        if count is None:
            raise ScheduleError(f"I didn't catch how long {relative.group('count')} is.")
        seconds = count * _UNITS[relative.group("unit")]
        # "in half an hour" parses as 30 of the unit, which is 30 hours.
        if relative.group("count").strip() == "half":
            seconds = _UNITS[relative.group("unit")] // 2
        if seconds < 5:
            raise ScheduleError("That's too soon to be worth setting.")
        return now + seconds, ""

    clock = _AT_RE.search(said)
    if clock:
        hour = int(clock.group("hour"))
        minute = int(clock.group("minute") or 0)
        meridiem = (clock.group("meridiem") or "").replace(".", "")
        if hour > 23 or minute > 59:
            raise ScheduleError(f"{hour}:{minute:02d} isn't a time I understand.")
        if meridiem == "pm" and hour < 12:
            hour += 12
        elif meridiem == "am" and hour == 12:
            hour = 0
        elif not meridiem and hour < 7:
            # "at 4" in the afternoon means four in the afternoon. Nobody sets
            # a reminder for four in the morning without saying "am".
            hour += 12

        when = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if "tomorrow" in said:
            when += timedelta(days=1)
        if repeat.startswith("weekly:"):
            wanted = int(repeat.split(":")[1])
            ahead = (wanted - when.weekday()) % 7
            when += timedelta(days=ahead)
        if when.timestamp() <= now:
            when += timedelta(days=7 if repeat.startswith("weekly") else 1)
        return when.timestamp(), repeat

    if repeat:
        # "every morning" with no clock time: pick the obvious hour.
        hour = {"morning": 8, "evening": 18, "night": 21}.get(
            (every.group("what") if every else ""), 9
        )
        when = base.replace(hour=hour, minute=0, second=0, microsecond=0)
        if when.timestamp() <= now:
            when += timedelta(days=1)
        return when.timestamp(), repeat

    if "tomorrow" in said:
        when = (base + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
        return when.timestamp(), ""

    raise ScheduleError(
        "I couldn't work out when. Try 'in twenty minutes', 'at half past four', "
        "or 'every day at nine'."
    )


_DURATION_RE = re.compile(
    r"(?P<count>\d+(?:\.\d+)?|[a-z]+(?:[ -]five)?)\s*"
    r"(?:an?\s+)?"
    r"(?P<unit>seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h)\b"
)
_CLOCK_DURATION_RE = re.compile(r"^(?P<a>\d{1,3}):(?P<b>[0-5]\d)(?::(?P<c>[0-5]\d))?$")

_DURATION_UNITS = {
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
}
_UNIT_WORDS = "seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h"
# "two and a half hours" - the half belongs to the unit that follows.
_HALF_BEFORE_RE = re.compile(
    rf"\b(?P<count>\d+|[a-z]+)\s+and\s+a\s+half\s+(?P<unit>{_UNIT_WORDS})\b"
)
# "an hour and a half" - the half belongs to the unit just named.
_HALF_AFTER_RE = re.compile(
    rf"\b(?:an?\s+)?(?P<unit>{_UNIT_WORDS})\s+and\s+a\s+half\b"
)


def parse_duration(text: str) -> float:
    """A spoken length of time, in seconds.

    Separate from `parse_when` on purpose: a timer is a *length*, and "twenty
    minutes" with no "in" in front of it is how everybody says one. Understands
    "twenty minutes", "an hour and a half", "90 seconds", "20m", "1:30".

    Refused rather than guessed at when the unit is missing: "set a timer for
    twenty" is twenty of something, and picking one for the user is how a
    dinner burns.
    """
    raw = (text or "").strip().lower()
    if not raw:
        raise ScheduleError("How long for?")

    clock = _CLOCK_DURATION_RE.match(raw)
    if clock:
        # 1:30 is an hour and a half; 1:30:00 is the same said longer.
        if clock.group("c") is not None:
            return (
                int(clock.group("a")) * 3600
                + int(clock.group("b")) * 60
                + int(clock.group("c"))
            )
        return int(clock.group("a")) * 3600 + int(clock.group("b")) * 60

    total = 0.0
    found = False

    # "and a half" first, and consume what it covered: which unit the half
    # belongs to depends on whether it comes before or after one, and the
    # general loop below cannot see that.
    def take_half(match) -> str:
        nonlocal total, found
        unit = _DURATION_UNITS[match.group("unit")]
        count = 1.0
        if "count" in match.groupdict():
            raw_count = match.group("count")
            count = (
                float(raw_count)
                if raw_count.replace(".", "", 1).isdigit()
                else float(_count_from(raw_count) or 1)
            )
        total += (count + 0.5) * unit
        found = True
        return " "

    cleaned = _HALF_BEFORE_RE.sub(take_half, raw)
    cleaned = _HALF_AFTER_RE.sub(take_half, cleaned)
    cleaned = cleaned.replace(" and ", " ")

    for match in _DURATION_RE.finditer(cleaned):
        count_text = match.group("count")
        unit = _DURATION_UNITS.get(match.group("unit"))
        if unit is None:
            continue

        # "half an hour" is half of the unit. _WORD_NUMBERS maps "half" to 30
        # for parse_when's benefit ("half four"), which is the wrong reading
        # here, so it is handled before the lookup rather than after.
        if count_text == "half":
            total += 0.5 * unit
            found = True
            continue

        if count_text.replace(".", "", 1).isdigit():
            count: float | None = float(count_text)
        else:
            count = _count_from(count_text)
        if count is None:
            continue
        total += count * unit
        found = True

    if not found:
        if re.fullmatch(r"[\d.]+", raw):
            raise ScheduleError(
                f"{raw} what - minutes or seconds? Say 'twenty minutes' and I'll set it."
            )
        raise ScheduleError(
            "I couldn't work out how long. Try 'twenty minutes', 'an hour and a "
            "half', or '90 seconds'."
        )
    if total < 1:
        raise ScheduleError("That's too short to be worth a timer.")
    return total


def _next_after(when: float, repeat: str) -> float | None:
    """When a repeating job fires next, or None if it is finished."""
    if not repeat:
        return None
    moment = datetime.fromtimestamp(when)
    if repeat == "hourly":
        return (moment + timedelta(hours=1)).timestamp()
    if repeat == "daily":
        return (moment + timedelta(days=1)).timestamp()
    if repeat.startswith("weekly"):
        return (moment + timedelta(days=7)).timestamp()
    return None


@dataclass(slots=True)
class Job:
    id: str
    when: float
    what: str  # a spoken phrase for `say`, or a command name
    kind: str = "say"  # say | command | timer
    args: dict[str, Any] = field(default_factory=dict)
    repeat: str = ""
    created: float = 0.0
    fired: int = 0
    # What the user actually said, so `schedule.list` can read it back to them
    # in their own words rather than describing a data structure.
    said: str = ""
    # Timers only: the length it was set for, so it can be read back as "the
    # twenty-minute pasta timer" rather than as an absolute time nobody chose.
    # 0 means it was set at a clock time, which is an alarm.
    total_seconds: float = 0.0
    # Timers only: what it is for. "pasta", "the oven".
    name: str = ""

    @property
    def is_timer(self) -> bool:
        return self.kind == "timer"

    def remaining(self, now: float | None = None) -> float:
        return max(0.0, self.when - (time.time() if now is None else now))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "when": self.when, "what": self.what, "kind": self.kind,
            "args": self.args, "repeat": self.repeat, "created": self.created,
            "fired": self.fired, "said": self.said,
            "total_seconds": self.total_seconds, "name": self.name,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Job | None":
        try:
            return cls(
                id=str(raw["id"]),
                when=float(raw["when"]),
                what=str(raw["what"]),
                kind=str(raw.get("kind") or "say"),
                args=dict(raw.get("args") or {}),
                repeat=str(raw.get("repeat") or ""),
                created=float(raw.get("created") or 0.0),
                fired=int(raw.get("fired") or 0),
                said=str(raw.get("said") or ""),
                total_seconds=float(raw.get("total_seconds") or 0.0),
                name=str(raw.get("name") or ""),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def describe(self, now: float | None = None) -> str:
        """One spoken line: what it is and when."""
        now = time.time() if now is None else now
        moment = datetime.fromtimestamp(self.when)
        delta = self.when - now

        if self.is_timer:
            return self._describe_timer(now, moment)

        if self.repeat == "daily":
            when = f"every day at {moment.strftime('%H:%M')}"
        elif self.repeat == "hourly":
            when = "every hour"
        elif self.repeat.startswith("weekly"):
            when = f"every {moment.strftime('%A')} at {moment.strftime('%H:%M')}"
        elif delta < 3600:
            when = f"in {max(1, round(delta / 60))} minutes"
        elif moment.date() == datetime.fromtimestamp(now).date():
            when = f"at {moment.strftime('%H:%M')}"
        elif delta < 86400 * 6:
            when = f"{moment.strftime('%A')} at {moment.strftime('%H:%M')}"
        else:
            when = moment.strftime("%d %B at %H:%M")

        subject = self.said or (self.what if self.kind == "say" else f"run {self.what}")
        return f"{subject}, {when}"

    def _describe_timer(self, now: float, moment: datetime) -> str:
        """A timer answers 'how long left', not 'at what time'."""
        from .humanize import duration_speech

        if not self.total_seconds:
            # Set at a clock time, so it is an alarm and the time is the point.
            when = f"the alarm for {moment.strftime('%H:%M')}"
            if self.repeat == "daily":
                when += ", every day"
            elif self.repeat.startswith("weekly"):
                when += f", every {moment.strftime('%A')}"
            return f"{when} ({self.name})" if self.name else when

        left = self.remaining(now)
        left_text = "less than a minute left" if left < 60 else f"{duration_speech(left)} left"
        if self.name:
            return f"the {self.name} timer, {left_text}"
        return f"a {duration_speech(self.total_seconds)} timer, {left_text}"


class Schedule:
    """The jobs, on disk. The agent's tick asks it what is due."""

    def __init__(self, path: Path | str, max_jobs: int = MAX_JOBS) -> None:
        self.path = Path(path)
        self.max_jobs = max_jobs
        self._jobs: list[Job] = []
        self._mtime: int | None = None
        self._load()

    def _stat(self) -> int | None:
        try:
            return self.path.stat().st_mtime_ns
        except OSError:
            return None

    def _load(self) -> None:
        mtime = self._stat()
        try:
            with self.path.open("r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except FileNotFoundError:
            self._jobs = []
            self._mtime = mtime
            return
        except (OSError, json.JSONDecodeError) as exc:
            # Keep what we already have. Clearing on a failed read would mean
            # one unreadable moment silently forgot every timer and reminder,
            # and this is now re-read on every tick rather than once at boot.
            log.warning("could not read the schedule (%s); keeping what I have", exc)
            return

        loaded: list[Job] = []
        for raw in payload.get("jobs") or []:
            job = Job.from_dict(raw) if isinstance(raw, dict) else None
            if job is not None:
                loaded.append(job)
        self._jobs = loaded
        self._mtime = mtime

    def reload_if_changed(self) -> bool:
        """Pick up jobs added by another process.

        This matters more than it looks. The agent builds one Schedule at
        startup and its tick is the clock, but almost nothing is *set* in the
        agent: a timer comes from the voice session, a reminder from an SSH
        `exec`, both of which are separate processes that write this file.
        Without this the agent would go on firing the jobs it happened to load
        at boot and silently ignore every one added since.
        """
        mtime = self._stat()
        if mtime is not None and mtime != self._mtime:
            self._load()
            return True
        return False

    def _save(self) -> None:
        # Temp file then replace, rather than truncating in place: another
        # process reads this every tick, and a half-written file read as
        # "no jobs" is a timer that never rings.
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(
                dir=str(self.path.parent), prefix=".schedule-", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump({"jobs": [job.to_dict() for job in self._jobs]}, fh)
                os.replace(tmp, self.path)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
        except OSError as exc:
            log.error("could not save the schedule: %s", exc)
            return
        # Our own write must not read as somebody else's change.
        self._mtime = self._stat()

    # -- editing ------------------------------------------------------------

    def add(
        self,
        what: str,
        when_text: str,
        *,
        kind: str = "say",
        args: dict[str, Any] | None = None,
        said: str = "",
        now: float | None = None,
        seconds: float | None = None,
        name: str = "",
    ) -> Job:
        """Add a job. `seconds` sets a timer a length from now instead of
        parsing `when_text`, which is what makes "twenty minutes" a timer and
        "at seven" an alarm."""
        # Almost every job is set from a different process than the one whose
        # tick fires it, and both rewrite the whole list. Re-reading first
        # means a job the agent just fired is not written back underneath it
        # and rung a second time.
        self.reload_if_changed()

        if len(self._jobs) >= self.max_jobs:
            raise ScheduleError("I'm holding as many reminders as I can already.")

        started = time.time() if now is None else now
        total = 0.0
        if seconds is not None:
            total = float(seconds)
            when, repeat = started + total, ""
        else:
            when, repeat = parse_when(when_text, now=now)

        job = Job(
            id=uuid.uuid4().hex[:8],
            when=when,
            what=what.strip(),
            kind=kind,
            args=dict(args or {}),
            repeat=repeat,
            created=started,
            said=said.strip(),
            total_seconds=total,
            name=name.strip(),
        )
        self._jobs.append(job)
        self._save()
        log.info("scheduled %s (%s)", job.id, job.describe(now))
        return job

    def cancel(self, query: str, kinds: tuple[str, ...] | None = None) -> list[Job]:
        """Drop jobs by id, or by words in what they are for.

        `kinds` narrows it, so cancelling "the pasta" as a timer can never take
        a nine o'clock reminder with it.
        """
        wanted = (query or "").strip().lower()
        if not wanted:
            raise ScheduleError("Tell me which one to cancel.")
        self.reload_if_changed()

        def in_scope(job: Job) -> bool:
            return kinds is None or job.kind in kinds

        if wanted in ("all", "everything"):
            dropped = [job for job in self._jobs if in_scope(job)]
        else:
            dropped = [
                job for job in self._jobs
                if in_scope(job)
                and (
                    job.id == wanted
                    or wanted in (job.name or "").lower()
                    or wanted in (job.said or job.what).lower()
                )
            ]
        if dropped:
            self._jobs = [job for job in self._jobs if job not in dropped]
            self._save()
        return dropped

    def jobs(self) -> list[Job]:
        return sorted(self._jobs, key=lambda job: job.when)

    def timers(self) -> list[Job]:
        return [job for job in self.jobs() if job.is_timer]

    def reminders(self) -> list[Job]:
        return [job for job in self.jobs() if not job.is_timer]

    # -- running ------------------------------------------------------------

    def due(self, now: float | None = None) -> list[Job]:
        """Jobs to run now, rescheduling or removing them as they are taken.

        A job whose time passed while the PC was asleep fires if it is only a
        little late, and is dropped otherwise - nobody wants Tuesday's reminder
        on Thursday morning.
        """
        # Whatever another process has set since the last tick.
        self.reload_if_changed()

        now = time.time() if now is None else now
        ready: list[Job] = []
        keep: list[Job] = []
        changed = False

        for job in self._jobs:
            if job.when > now:
                keep.append(job)
                continue

            changed = True
            late = now - job.when
            if late <= LATE_TOLERANCE_SECONDS:
                job.fired += 1
                ready.append(job)
            else:
                log.info("dropping %s: it was due %.0f minutes ago", job.id, late / 60)

            following = _next_after(job.when, job.repeat)
            if following is not None:
                # Catch up past any firings missed while the machine was off.
                while following <= now:
                    following = _next_after(following, job.repeat) or following + 86400
                keep.append(
                    Job(**{**job.to_dict(), "when": following, "args": dict(job.args)})
                )

        if changed:
            self._jobs = keep
            self._save()
        return ready


def path_for(config) -> Path:
    return Path(config.state_file).with_name("schedule.json")
