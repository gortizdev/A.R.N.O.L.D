"""Timers and alarms that ring here.

"Set a timer for twenty minutes" is the single most ordinary thing anybody
asks an assistant, and until now this one handed it to Jarvis on the Pi - so
with the Pi off it simply failed. A timer for the person sitting at this desk
should ring at this desk.

Separate commands from `schedule.*` rather than more arguments on it, for
three reasons. The model routes far better on a verb that matches the request.
The arguments genuinely differ: a timer is a *length* and a reminder is a
*time*. And "cancel the pasta" as a timer must not be able to delete a nine
o'clock reminder that happens to mention pasta - `timer.cancel` is scoped to
timers, and that scoping is the point.

The store underneath is the same one: one file, one thing for the agent's tick
to read, and one honest answer to "what have I got on".
"""

from __future__ import annotations

import logging

from ..humanize import duration_speech, join_speech
from ..scheduler import Schedule, ScheduleError, parse_duration, parse_when, path_for
from .registry import (
    CommandContext,
    CommandError,
    CommandResult,
    Registry,
    arg_str,
)

log = logging.getLogger(__name__)

TIMER_KINDS = ("timer",)


def register_all(registry: Registry) -> None:
    registry.register(
        "timer.set",
        _set,
        "Set a timer or an alarm that rings on this PC.",
        {
            "minutes": "how long, as a number",
            "seconds": "or how long in seconds",
            "hours": "or how long in hours",
            "for": "or a spoken length: 'twenty minutes', 'an hour and a half'",
            "when": "or a clock time, for an alarm: 'at 7', 'every weekday at 7'",
            "name": "what it is for - 'pasta' - so it can be read back and cancelled",
        },
    )
    registry.register(
        "timer.list",
        _list,
        "Timers and alarms running now, with what is left on each.",
    )
    registry.register(
        "timer.cancel",
        _cancel,
        "Stop a timer or alarm by name, or 'all'.",
        {"which": "'pasta', its id, or 'all'"},
    )


def _schedule(ctx: CommandContext) -> Schedule:
    cfg = ctx.config.schedule
    if not cfg.enabled:
        raise CommandError("Scheduling is switched off on this PC.")
    if not cfg.timers:
        raise CommandError("Timers are switched off on this PC.")
    return Schedule(path_for(ctx.config), cfg.max_jobs)


def _number(args: dict, key: str) -> float | None:
    """A numeric argument, tolerating the string a voice model sends."""
    value = args.get(key)
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        raise CommandError(f"I couldn't read {value!r} as a number of {key}.") from None


def _length_from(args: dict) -> float | None:
    """How long the timer is for, from whichever way it was asked."""
    total = 0.0
    given = False
    for key, unit in (("hours", 3600.0), ("minutes", 60.0), ("seconds", 1.0)):
        value = _number(args, key)
        if value is not None:
            total += value * unit
            given = True
    if given:
        # The same floor parse_duration applies. Without it `minutes=0` sets a
        # timer already in the past, which rings on the very next tick.
        if total < 1:
            raise CommandError("That's too short to be worth a timer.")
        return total

    spoken = str(args.get("for") or args.get("duration") or "").strip()
    if spoken:
        try:
            return parse_duration(spoken)
        except ScheduleError as exc:
            raise CommandError(str(exc)) from exc
    return None


def _set(ctx: CommandContext, args: dict) -> CommandResult:
    schedule = _schedule(ctx)
    name = str(args.get("name") or "").strip()
    seconds = _length_from(args)
    when_text = str(args.get("when") or "").strip()

    if seconds is None and not when_text:
        raise CommandError("How long for? Say 'twenty minutes', or give me a time.")

    subject = f"the {name} timer" if name else "your timer"
    if seconds is not None:
        limit = ctx.config.schedule.max_timer_hours * 3600
        if seconds > limit:
            raise CommandError(
                f"That's longer than {duration_speech(limit)} - "
                "set it as a reminder instead."
            )
        spoken = f"{subject.capitalize()} is up." if name else "Your timer is up."
        job = schedule.add(
            spoken,
            "",
            kind="timer",
            seconds=seconds,
            name=name,
            said=f"{duration_speech(seconds)}{f' for {name}' if name else ''}",
        )
        return CommandResult(
            speech=(
                f"Right - {duration_speech(seconds)} on the {name}."
                if name
                else f"Right - {duration_speech(seconds)}."
            ),
            result={"timer": job.to_dict(), "describes": job.describe()},
        )

    # A clock time, which is an alarm: it keeps total_seconds at 0 so it reads
    # back as a time rather than as a countdown nobody chose.
    try:
        parse_when(when_text)
    except ScheduleError as exc:
        raise CommandError(str(exc)) from exc
    spoken = f"{subject.capitalize()}." if name else "Your alarm."
    job = schedule.add(
        spoken, when_text, kind="timer", name=name, said=name or "your alarm"
    )
    return CommandResult(
        speech=f"Set - {job.describe()}.",
        result={"timer": job.to_dict(), "describes": job.describe()},
    )


def _list(ctx: CommandContext, args: dict) -> CommandResult:
    timers = _schedule(ctx).timers()
    if not timers:
        return CommandResult(speech="Nothing running.", result={"timers": []})

    described = [job.describe() for job in timers]
    if len(timers) == 1:
        speech = f"{described[0].capitalize()}."
    else:
        speech = f"{len(timers)}: " + join_speech(described, "and") + "."
    return CommandResult(
        speech=speech,
        result={"timers": [job.to_dict() for job in timers], "describes": described},
    )


def _cancel(ctx: CommandContext, args: dict) -> CommandResult:
    which = arg_str(args, "which")
    try:
        # Scoped to timers: cancelling "the pasta" must never take a reminder.
        dropped = _schedule(ctx).cancel(which, kinds=TIMER_KINDS)
    except ScheduleError as exc:
        raise CommandError(str(exc)) from exc

    if not dropped:
        return CommandResult.failure(
            "Nothing running." if which in ("all", "everything")
            else f"I don't have a timer for {which}."
        )
    if len(dropped) == 1:
        job = dropped[0]
        label = f"the {job.name} timer" if job.name else "your timer"
        return CommandResult(
            speech=f"Stopped {label}.",
            result={"cancelled": [job.to_dict() for job in dropped]},
        )
    return CommandResult(
        speech=f"Stopped all {len(dropped)} of them.",
        result={"cancelled": [job.to_dict() for job in dropped]},
    )
