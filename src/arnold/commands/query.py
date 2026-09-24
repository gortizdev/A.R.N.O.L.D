"""Read-only questions Jarvis can ask about the PC.

Every handler sets `speech` to a complete sentence, so the Pi can pass it
straight to text-to-speech without post-processing.
"""

from __future__ import annotations

import time
from pathlib import Path

from ..humanize import (
    bytes_speech,
    clock_speech,
    count_speech,
    drive_speech,
    duration_speech,
    join_speech,
    percent_speech,
    rate_speech,
    sentence_case,
)
from ..monitors.collector import get_metric
from ..state import read_state
from .registry import CommandContext, CommandError, CommandResult, Registry, arg_int, arg_str


def register_all(registry: Registry) -> None:
    registry.register("query.system", _system, "Overall health summary in one sentence.")
    registry.register("query.cpu", _cpu, "Current CPU load and clock speed.")
    registry.register("query.memory", _memory, "RAM used, free and total.")
    registry.register(
        "query.disk", _disk, "Free space on one drive or all of them.", {"drive": "e.g. 'C'"}
    )
    registry.register("query.gpu", _gpu, "GPU load, memory and temperature.")
    registry.register("query.network", _network, "Current upload and download rates.")
    registry.register("query.battery", _battery, "Battery charge and time remaining.")
    registry.register("query.uptime", _uptime, "How long the machine has been up.")
    registry.register(
        "query.processes", _processes, "Top processes by CPU or memory.",
        {"by": "'cpu' or 'memory'", "limit": "how many to list (default 3)"},
    )
    registry.register(
        "query.process", _process, "Whether a named process is running.", {"name": "e.g. 'steam'"}
    )
    registry.register("query.active_window", _active_window, "What is in the foreground right now.")
    registry.register("query.alerts", _alerts, "Which alert rules are currently firing.")
    registry.register("query.volume", _volume, "Current output volume and mute state.")
    registry.register(
        "query.metric", _metric, "Raw value of any telemetry path.", {"path": "e.g. 'disks.C.percent'"}
    )
    registry.register(
        "query.trend",
        _trend,
        "Which way a metric is heading, and when it runs out.",
        {
            "path": "e.g. 'disks.C.free_bytes' (default), 'memory.percent'",
            "days": "how far back to measure (default 14)",
        },
    )
    # Outlook lives in the logged-on desktop session, so an SSH-invoked exec
    # cannot reach it and has to hand these to the agent.
    registry.register(
        "query.mail", _mail, "Unread count and the newest thing in the inbox.", needs_desktop=True
    )
    registry.register(
        "query.notifications",
        _notifications,
        "What has come in on Teams and Outlook lately.",
        {
            "hours": "how far back to look (default 4)",
            "app": "'teams' or 'outlook' to narrow it (default both)",
            "limit": "how many to read out (default 4)",
        },
        needs_desktop=True,
    )
    registry.register(
        "query.next_meeting",
        _next_meeting,
        "The meeting in progress or the next one due.",
        needs_desktop=True,
    )
    registry.register(
        "query.agenda",
        _agenda,
        "Meetings coming up.",
        {"hours": "how far ahead to look (default 8)"},
        needs_desktop=True,
    )


def _cpu(ctx: CommandContext, args: dict) -> CommandResult:
    cpu = ctx.collector.snapshot(include_window=False)["cpu"]
    speech = f"CPU on {ctx.device_name} is at {percent_speech(cpu['percent'])}"
    if cpu.get("freq_mhz"):
        speech += f", running at {cpu['freq_mhz'] / 1000:.1f} gigahertz"
    return CommandResult(speech=speech + ".", result=cpu)


def _memory(ctx: CommandContext, args: dict) -> CommandResult:
    mem = ctx.collector.snapshot(include_window=False)["memory"]
    speech = (
        f"Memory on {ctx.device_name} is at {percent_speech(mem['percent'])}. "
        f"{bytes_speech(mem['used_bytes'])} used of {bytes_speech(mem['total_bytes'])}, "
        f"{bytes_speech(mem['available_bytes'])} available."
    )
    return CommandResult(speech=speech, result=mem)


def _disk(ctx: CommandContext, args: dict) -> CommandResult:
    disks = ctx.collector.snapshot(include_window=False)["disks"]
    if not disks:
        raise CommandError("I'm not monitoring any drives on this machine.")

    requested = args.get("drive")
    if requested:
        key = str(requested).rstrip(":\\/").upper()
        entry = disks.get(key)
        if entry is None:
            available = join_speech([drive_speech(d) for d in sorted(disks)], "and")
            raise CommandError(f"I'm not monitoring drive {key}. I have {available}.")
        speech = sentence_case(
            f"{drive_speech(key)} has {bytes_speech(entry['free_bytes'])} free "
            f"of {bytes_speech(entry['total_bytes'])}, {percent_speech(entry['percent'])} used."
        )
        return CommandResult(speech=speech, result={key: entry})

    parts = [
        f"{drive_speech(name)} has {bytes_speech(entry['free_bytes'])} free"
        for name, entry in sorted(disks.items())
    ]
    return CommandResult(speech=sentence_case(join_speech(parts, "and") + "."), result=disks)


def _gpu(ctx: CommandContext, args: dict) -> CommandResult:
    gpus = ctx.collector.snapshot(include_window=False)["gpus"]
    if not gpus:
        return CommandResult(
            speech="I can't read a GPU on this machine. nvidia-smi isn't available.",
            result={"gpus": []},
        )

    parts = []
    for gpu in gpus:
        bits = [str(gpu.get("name") or f"GPU {gpu.get('index')}")]
        if gpu.get("utilization_percent") is not None:
            bits.append(f"at {percent_speech(gpu['utilization_percent'])}")
        if gpu.get("temperature_c") is not None:
            bits.append(f"{gpu['temperature_c']:.0f} degrees")
        if gpu.get("memory_used_mb") is not None and gpu.get("memory_total_mb"):
            bits.append(
                f"using {gpu['memory_used_mb'] / 1024:.1f} of "
                f"{gpu['memory_total_mb'] / 1024:.1f} gigabytes of video memory"
            )
        parts.append(" ".join(bits))
    return CommandResult(speech=join_speech(parts, "and") + ".", result={"gpus": gpus})


def _network(ctx: CommandContext, args: dict) -> CommandResult:
    net = ctx.collector.snapshot(include_window=False)["network"]
    if net.get("recv_rate_bps") is None:
        # A rate needs two samples; a one-shot call only ever takes one. Borrow
        # the running agent's most recent figures.
        state = _agent_state(ctx)
        if state and (state.get("network") or {}).get("recv_rate_bps") is not None:
            net = {**net, **state["network"]}
    if net.get("recv_rate_bps") is None:
        return CommandResult(
            speech=f"I can't measure the network rate on {ctx.device_name} right now. "
            f"The monitoring agent may not be running.",
            result=net,
        )
    speech = (
        f"{ctx.device_name} is downloading at {rate_speech(net['recv_rate_bps'])} "
        f"and uploading at {rate_speech(net['sent_rate_bps'])}."
    )
    return CommandResult(speech=speech, result=net)


def _battery(ctx: CommandContext, args: dict) -> CommandResult:
    battery = ctx.collector.snapshot(include_window=False).get("battery")
    if battery is None:
        return CommandResult(
            speech=f"{ctx.device_name} doesn't have a battery.", result={"battery": None}
        )
    speech = f"Battery is at {percent_speech(battery['percent'])}"
    if battery["plugged_in"]:
        speech += " and charging."
    elif battery.get("seconds_left"):
        speech += f", about {duration_speech(battery['seconds_left'])} remaining."
    else:
        speech += " on battery power."
    return CommandResult(speech=speech, result=battery)


def _uptime(ctx: CommandContext, args: dict) -> CommandResult:
    snap = ctx.collector.snapshot(include_window=False)
    seconds = snap["uptime_seconds"]
    return CommandResult(
        speech=f"{ctx.device_name} has been up for {duration_speech(seconds)}.",
        result={"uptime_seconds": seconds, "boot_time": snap["boot_time"]},
    )


def _processes(ctx: CommandContext, args: dict) -> CommandResult:
    by = str(args.get("by", "cpu")).lower()
    if by not in ("cpu", "memory"):
        raise CommandError("Ask for processes by cpu or by memory.")
    limit = arg_int(args, "limit", 3, minimum=1, maximum=20)

    procs = ctx.collector.snapshot(include_window=False)["processes"]
    rows = procs["top_cpu" if by == "cpu" else "top_memory"][:limit]
    if not rows:
        raise CommandError("I couldn't read the process list.")

    if by == "cpu":
        parts = [f"{r['name']} at {percent_speech(r['cpu_percent'])}" for r in rows]
    else:
        parts = [f"{r['name']} using {bytes_speech(r['memory_bytes'])}" for r in rows]

    speech = f"Top {len(rows)} by {by} on {ctx.device_name}: {join_speech(parts, 'and')}."
    return CommandResult(speech=speech, result={"by": by, "processes": rows, "count": procs["count"]})


def _process(ctx: CommandContext, args: dict) -> CommandResult:
    import psutil

    name = arg_str(args, "name").lower()
    needle = name[:-4] if name.endswith(".exe") else name

    matches = []
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            candidate = (proc.info["name"] or "").lower()
            if needle in candidate.removesuffix(".exe"):
                matches.append({"pid": proc.info["pid"], "name": proc.info["name"]})
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    if matches:
        count = len(matches)
        plural = f"{count} processes" if count > 1 else "one process"
        speech = f"Yes, {matches[0]['name']} is running on {ctx.device_name}, {plural}."
    else:
        speech = f"No, {args['name']} is not running on {ctx.device_name}."
    return CommandResult(
        speech=speech, result={"running": bool(matches), "matches": matches[:20]}
    )


def _foreground(ctx: CommandContext, snapshot: dict | None = None) -> dict:
    """Foreground window, falling back to the agent's view.

    A command arriving over SSH runs outside the interactive desktop session, so
    it sees no foreground window at all. The agent runs in that session and does.
    """
    window = (snapshot or ctx.collector.snapshot())["active_window"] or {}
    if window.get("title") or window.get("process"):
        return window
    state = _agent_state(ctx)
    if state and (state.get("active_window") or {}).get("title"):
        return state["active_window"]
    return window


def _active_window(ctx: CommandContext, args: dict) -> CommandResult:
    window = _foreground(ctx)
    if not window.get("title") and not window.get("process"):
        return CommandResult(
            speech=f"I can't see what's in the foreground on {ctx.device_name}. "
            f"It may be locked, or the monitoring agent isn't running.",
            result=window,
        )
    title = window.get("title") or "an untitled window"
    process = window.get("process")
    speech = f"{ctx.device_name} is showing {title}"
    speech += f", running in {process}." if process else "."
    return CommandResult(speech=speech, result=window)


def _agent_state(ctx: CommandContext) -> dict | None:
    """The running agent's last published state, if it is running and fresh."""
    return read_state(Path(ctx.config.state_file))


def _active_alerts(ctx: CommandContext) -> tuple[list[str], bool]:
    """Current alerts, plus whether the answer is trustworthy.

    A live agent's own engine is authoritative. A one-shot `exec` builds a fresh
    engine that has never evaluated, so it falls back to the state file the
    agent writes each tick - and reports uncertainty if that is missing too.
    """
    if ctx.alerts.evaluated:
        return ctx.alerts.active(), True

    state = read_state(Path(ctx.config.state_file))
    if state is None:
        return [], False
    active = state.get("alerts", {}).get("active", [])
    return (active if isinstance(active, list) else []), True


def _alerts(ctx: CommandContext, args: dict) -> CommandResult:
    firing, known = _active_alerts(ctx)
    if not known:
        return CommandResult(
            ok=False,
            speech=f"I can't tell - the monitoring agent doesn't seem to be running on "
            f"{ctx.device_name}.",
            result={"agent_running": False},
            error="no fresh state file; the agent is not running",
        )
    if not firing:
        return CommandResult(
            speech=f"No alerts are active on {ctx.device_name}.",
            result={"active": [], "rule_count": len(ctx.alerts.rules)},
        )
    speech = (
        f"{len(firing)} alert{'s are' if len(firing) > 1 else ' is'} active on "
        f"{ctx.device_name}: {join_speech([f.replace('_', ' ') for f in firing], 'and')}."
    )
    return CommandResult(speech=speech, result={"active": firing})


def _volume(ctx: CommandContext, args: dict) -> CommandResult:
    from ..platform_win import audio

    state = audio.get_volume()
    if state["level"] is None:
        return CommandResult(
            speech="I can change the volume but can't read the exact level. "
            "Install the audio extra for that.",
            result=state,
        )
    speech = f"Volume on {ctx.device_name} is {state['level']} percent"
    speech += ", muted." if state["muted"] else "."
    return CommandResult(speech=speech, result=state)


def _metric(ctx: CommandContext, args: dict) -> CommandResult:
    path = arg_str(args, "path")
    snapshot = ctx.collector.snapshot()
    value = get_metric(snapshot, path)
    if value is None:
        raise CommandError(f"I don't have a metric at {path}.")
    return CommandResult(
        speech=f"{path.replace('.', ' ')} is {value}.", result={"path": path, "value": value}
    )


def _trend(ctx: CommandContext, args: dict) -> CommandResult:
    """Where a number is heading, read off the agent's history.

    The interesting answer is almost never the gradient - it is the date the
    line meets zero, which is why free bytes is the default rather than
    percent used.
    """
    from ..history import History, path_for

    if not ctx.config.history.enabled:
        raise CommandError("I'm not keeping a history on this PC, so I can't see trends.")

    path = str(args.get("path") or "").strip()
    if not path:
        # "How's the disk doing" means the busiest drive, which is the one
        # anybody is actually worried about.
        disks = ctx.collector.snapshot().get("disks") or {}
        busiest = max(disks.items(), key=lambda kv: kv[1].get("percent") or 0, default=None)
        if busiest is None:
            raise CommandError("I can't see any drives to look at.")
        path = f"disks.{busiest[0]}.free_bytes"

    days = arg_int(args, "days", 14, minimum=1, maximum=90)
    trend = History(path_for(ctx.config)).trend(path, hours=days * 24)
    if trend is None:
        raise CommandError(
            "I haven't been watching long enough to tell you which way that's going. "
            "Give me a day or so."
        )

    per_day = trend["per_day"]
    is_bytes = path.endswith("_bytes")
    size = bytes_speech(abs(per_day)) if is_bytes else f"{abs(per_day):.1f}"
    direction = "gaining" if per_day > 0 else "losing"
    subject = path.replace("disks.", "drive ").replace(".free_bytes", "").replace(".", " ")

    speech = (
        f"{sentence_case(subject)} is {direction} about {size} a day, "
        f"measured over the last {round(trend['span_hours'] / 24) or 1} days."
    )
    if trend.get("days_to_zero") is not None:
        left = trend["days_to_zero"]
        speech += (
            " At that rate it runs out today."
            if left < 1
            else f" At that rate it's empty in about {round(left)} days."
        )

    return CommandResult(speech=speech, result=trend)


# -- mail and calendar -------------------------------------------------------


def mailbox_section(ctx: CommandContext, key: str) -> dict:
    """One of the Outlook sections of the snapshot, or an explanation.

    Three failures worth telling apart, because the fix differs: the section is
    absent when the feature is switched off, present-but-unavailable when
    Outlook cannot be reached, and otherwise real.
    """
    section = ctx.collector.snapshot(include_window=False).get(key)
    if section is None:
        what = "mail" if key == "mail" else "calendar"
        raise CommandError(
            f"I'm not set up to read your {what} on {ctx.device_name}. "
            f"Switch on the outlook section in my config."
        )
    if not section.get("available"):
        raise CommandError(section.get("error") or "I couldn't reach Outlook on this PC.")
    return section


def _lead_speech(minutes: float | None) -> str:
    """How far off something is, phrased as a warning rather than a clock time."""
    if minutes is None:
        return "at an unknown time"
    if minutes < 1:
        return "starting now"
    if minutes < 60:
        return f"{round(minutes)} minutes away"
    return f"{duration_speech(minutes * 60)} away"


def _describe_meeting(event: dict) -> str:
    """'the standup at 9:30 AM' plus what kind of meeting it is."""
    text = f"{event.get('subject') or 'an untitled meeting'} at {clock_speech(event.get('start_ts'))}"
    provider = event.get("provider")
    if provider == "teams":
        text += ", on Teams"
    elif provider:
        text += f", on {provider.title()}"
    elif event.get("location"):
        text += f", in {event['location']}"
    return text


def _mail(ctx: CommandContext, args: dict) -> CommandResult:
    mail = mailbox_section(ctx, "mail")
    unread = int(mail.get("unread") or 0)
    important = int(mail.get("unread_important") or 0)
    latest = mail.get("latest") or {}

    if unread:
        speech = f"You have {count_speech(unread, 'unread message')}"
        if important:
            speech += f", {important} flagged important"
        speech += "."
    else:
        speech = "Nothing unread in your inbox."

    if latest:
        elapsed = mail.get("seconds_since_latest")
        when = f"{duration_speech(elapsed)} ago" if elapsed and elapsed >= 60 else "just now"
        speech += f" The newest is from {latest.get('from') or 'someone'}"
        if latest.get("subject"):
            speech += f", {latest['subject']},"
        speech += f" {when}."

    return CommandResult(speech=speech, result=mail)


def _notifications(ctx: CommandContext, args: dict) -> CommandResult:
    section = ctx.collector.snapshot(include_window=False).get("work_notifications")
    if section is None:
        raise CommandError(
            f"I'm not set up to read Teams and Outlook notifications on {ctx.device_name}. "
            "Switch on the notifications section in my config."
        )
    if not section.get("available"):
        raise CommandError(section.get("error") or "I couldn't read the notification centre.")

    hours = max(0.25, float(args.get("hours") or 4))
    app = str(args.get("app") or "").strip().lower()
    limit = max(1, arg_int(args, "limit", 4))
    cutoff = time.time() - hours * 3600
    notes = [
        n for n in section.get("recent") or []
        if float(n.get("ts") or 0) >= cutoff and (not app or str(n.get("app", "")).lower() == app)
    ]
    window = "hour" if hours == 1 else f"{hours:g} hours"
    where = app.title() if app else "Teams or Outlook"
    if not notes:
        return CommandResult(
            speech=f"Nothing from {where} in the last {window}.",
            result={"count": 0, "hours": hours, "notifications": []},
        )

    counts: dict[str, int] = {}
    for n in notes:
        counts[n["app"]] = counts.get(n["app"], 0) + 1
    summary = " and ".join(
        count_speech(c, "Teams message") if a == "Teams"
        else count_speech(c, "mail") if a == "Outlook"
        else count_speech(c, f"{a} notification")
        for a, c in counts.items()
    )
    speech = f"In the last {window}: {summary}."
    read = []
    for n in notes[:limit]:
        elapsed = time.time() - float(n.get("ts") or 0)
        when = f"{duration_speech(elapsed)} ago" if elapsed >= 60 else "just now"
        who = n.get("title") or "someone"
        body = (n.get("body") or "").strip()
        if n["app"] == "Teams":
            line = f"{who} on Teams"
        elif n["app"] == "Outlook":
            line = f"mail from {who}"
        else:
            line = f"{n['app']} from {who}"
        if body:
            line += f", {body}"
        read.append(f"{line}, {when}")
    speech += " " + "; ".join(read) + "."
    return CommandResult(
        speech=speech,
        result={"count": len(notes), "hours": hours, "by_app": counts, "notifications": notes[:limit]},
    )


def _next_meeting(ctx: CommandContext, args: dict) -> CommandResult:
    calendar = mailbox_section(ctx, "calendar")
    current, upcoming = calendar.get("current"), calendar.get("next")

    if current is None and upcoming is None:
        # Bounded by the configured lookahead, so say so rather than implying the
        # whole day is clear when only the next few hours were read.
        window = count_speech(round(ctx.config.outlook.lookahead_hours), "hour")
        return CommandResult(
            speech=f"Nothing on your calendar in the next {window}.", result=calendar
        )

    sentences: list[str] = []
    if current:
        sentences.append(
            f"You're in {current.get('subject') or 'a meeting'} now, "
            f"until {clock_speech(current.get('end_ts'))}"
        )
    if upcoming:
        text = (
            f"{'Then' if current else 'Next up is'} {_describe_meeting(upcoming)}, "
            f"{_lead_speech(calendar.get('minutes_until_next'))}"
        )
        if upcoming.get("organizer"):
            text += f", organised by {upcoming['organizer']}"
        sentences.append(text)

    return CommandResult(speech=". ".join(sentences) + ".", result=calendar)


def _agenda(ctx: CommandContext, args: dict) -> CommandResult:
    hours = arg_int(args, "hours", 8, minimum=1, maximum=48)
    calendar = mailbox_section(ctx, "calendar")

    now = time.time()
    horizon = now + hours * 3600
    upcoming = sorted(
        (
            event
            for event in calendar.get("events") or []
            if now < (event.get("start_ts") or 0) <= horizon
        ),
        key=lambda event: event["start_ts"],
    )

    window = count_speech(hours, "hour")
    if not upcoming:
        return CommandResult(
            speech=f"Nothing on your calendar in the next {window}.",
            result={**calendar, "upcoming": []},
        )

    # Five is about as much as anyone takes in from one spoken sentence.
    listed = upcoming[:5]
    speech = (
        f"{count_speech(len(upcoming), 'meeting')} in the next {window}: "
        f"{join_speech([_describe_meeting(e) for e in listed], 'then')}."
    )
    if len(upcoming) > len(listed):
        speech += f" And {len(upcoming) - len(listed)} more after that."

    return CommandResult(
        speech=sentence_case(speech), result={**calendar, "upcoming": upcoming}
    )


def _system(ctx: CommandContext, args: dict) -> CommandResult:
    """One-sentence health summary - the default 'what's my PC doing?' answer."""
    snap = ctx.collector.snapshot()
    # Fill in what a one-shot process cannot see for itself.
    snap["active_window"] = _foreground(ctx, snap)
    state = _agent_state(ctx)
    if state and snap.get("network", {}).get("recv_rate_bps") is None and state.get("network"):
        snap["network"] = {**snap.get("network", {}), **state["network"]}
    parts = [
        f"CPU {percent_speech(snap['cpu']['percent'])}",
        f"memory {percent_speech(snap['memory']['percent'])}",
    ]

    disks = snap["disks"]
    if disks:
        tightest = min(disks.items(), key=lambda kv: kv[1]["free_bytes"])
        parts.append(f"{bytes_speech(tightest[1]['free_bytes'])} free on {drive_speech(tightest[0])}")

    gpus = snap.get("gpus") or []
    if gpus and gpus[0].get("utilization_percent") is not None:
        parts.append(f"GPU {percent_speech(gpus[0]['utilization_percent'])}")

    battery = snap.get("battery")
    if battery:
        parts.append(f"battery {percent_speech(battery['percent'])}")

    firing, known = _active_alerts(ctx)
    speech = f"{ctx.device_name} is at {join_speech(parts, 'and')}."
    if firing:
        speech += f" {len(firing)} alert{'s are' if len(firing) > 1 else ' is'} active."
    elif known:
        speech += f" Up {duration_speech(snap['uptime_seconds'])}, no active alerts."
    else:
        speech += f" Up {duration_speech(snap['uptime_seconds'])}."

    return CommandResult(speech=speech, result=snap)
