"""Reminders and standing jobs.

"Remind me in twenty minutes" is a different kind of request from everything
else here: it is not answered, it is *kept*. The job goes in a file the agent's
tick reads, so it survives this conversation, the voice session, and a reboot.

Commands may be scheduled too, but only ones named in
`schedule.allow_commands`. A mis-heard "shut down at midnight" that becomes a
standing shutdown order every midnight is a far worse failure than a
mis-heard one that shuts down once, because nobody is there to see it happen.
"""

from __future__ import annotations

import logging

from ..humanize import join_speech
from ..scheduler import Schedule, ScheduleError, path_for
from .registry import (
    CommandContext,
    CommandError,
    CommandResult,
    Registry,
    arg_str,
)

log = logging.getLogger(__name__)


def register_all(registry: Registry) -> None:
    registry.register(
        "schedule.add",
        _add,
        "Remind the user of something, or run a command, at a time.",
        {
            "text": "what to say when it fires, e.g. 'check the render'",
            "when": "'in 20 minutes', 'at half four', 'every day at 9'",
            "command": "optional: a command to run instead of speaking",
            "args": "optional: arguments for that command",
        },
    )
    registry.register(
        "schedule.list", _list, "What is scheduled: reminders and standing jobs."
    )
    registry.register(
        "schedule.cancel",
        _cancel,
        "Cancel a reminder by what it is for, or 'all'.",
        {"which": "words from the reminder, its id, or 'all'"},
    )


def _schedule(ctx: CommandContext) -> Schedule:
    if not ctx.config.schedule.enabled:
        raise CommandError("Scheduling is switched off on this PC.")
    return Schedule(path_for(ctx.config), ctx.config.schedule.max_jobs)


def _add(ctx: CommandContext, args: dict) -> CommandResult:
    when_text = arg_str(args, "when")
    command = str(args.get("command") or "").strip()
    schedule = _schedule(ctx)

    if command:
        allowed = ctx.config.schedule.allow_commands
        if command not in allowed:
            raise CommandError(
                f"I'm not allowed to run {command} on a schedule. "
                + (
                    f"I can schedule {join_speech(sorted(allowed), 'and')}."
                    if allowed
                    else "No commands are allowed on a schedule on this PC."
                )
            )
        kind, what = "command", command
        said = str(args.get("text") or "").strip() or f"run {command}"
    else:
        kind = "say"
        what = arg_str(args, "text")
        said = what

    try:
        job = schedule.add(
            what, when_text, kind=kind, args=dict(args.get("args") or {}), said=said
        )
    except ScheduleError as exc:
        raise CommandError(str(exc)) from exc

    return CommandResult(
        speech=f"Right - {job.describe()}.",
        result={"job": job.to_dict(), "describes": job.describe()},
    )


def _list(ctx: CommandContext, args: dict) -> CommandResult:
    schedule = _schedule(ctx)
    jobs = schedule.jobs()
    if not jobs:
        return CommandResult(speech="You have nothing scheduled.", result={"jobs": []})

    described = [job.describe() for job in jobs]
    # Timers and reminders are different questions with one answer, so the
    # count names both rather than lumping them together as "things".
    timers = sum(1 for job in jobs if job.is_timer)
    reminders = len(jobs) - timers

    if len(jobs) == 1:
        speech = f"One thing: {described[0]}."
    else:
        counted = join_speech(
            [
                f"{n} {noun}{'' if n == 1 else 's'}"
                for n, noun in ((timers, "timer"), (reminders, "reminder"))
                if n
            ],
            "and",
        )
        speech = f"{counted}. " + ". ".join(
            line.capitalize() for line in described[:4]
        ) + ("." if len(jobs) <= 4 else f". And {len(jobs) - 4} more.")

    return CommandResult(
        speech=speech,
        result={
            "jobs": [job.to_dict() for job in jobs],
            "describes": described,
            "timers": timers,
            "reminders": reminders,
        },
    )


def _cancel(ctx: CommandContext, args: dict) -> CommandResult:
    which = arg_str(args, "which")
    try:
        dropped = _schedule(ctx).cancel(which)
    except ScheduleError as exc:
        raise CommandError(str(exc)) from exc

    if not dropped:
        raise CommandError(f"I can't find anything scheduled about {which}.")
    if len(dropped) == 1:
        return CommandResult(
            speech=f"Cancelled: {dropped[0].said or dropped[0].what}.",
            result={"cancelled": [job.to_dict() for job in dropped]},
        )
    return CommandResult(
        speech=f"Cancelled {len(dropped)} of them.",
        result={"cancelled": [job.to_dict() for job in dropped]},
    )
