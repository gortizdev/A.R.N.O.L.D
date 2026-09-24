"""The Claude Code sessions open on this machine: what they are doing, and a
word in their ear.

`code.*` hands Claude a job of its own in the background. This is the other
direction: the sessions the person already has open in the desktop app, in
VS Code or in a terminal - read from their transcripts by the watch in
monitors/claude.py - and the means to speak into one of them.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .. import claude_link
from ..humanize import count_speech, duration_speech, join_speech
from ..monitors.claude import Session, claude_home
from .registry import CommandContext, CommandError, CommandResult, Registry, arg_str

log = logging.getLogger(__name__)


def register_all(registry: Registry) -> None:
    registry.register(
        "claude.list",
        _list,
        "The Claude Code sessions open on this PC and what each is doing.",
    )
    registry.register(
        "claude.status",
        _status,
        "What one session is doing: the last ask, the current step, the last reply.",
        {"which": "the session, by title or project name; omit for the busy one"},
    )
    registry.register(
        "claude.prompt",
        _prompt,
        "Say something to a running Claude Code session, as if typed into it.",
        {
            "which": "the session, by title or project name",
            "text": "what to tell it",
            "session": "or the exact session id, as the dashboard sends",
        },
    )
    registry.register(
        "claude.apps",
        _apps,
        "The local dev servers those sessions have running, and whether they answer.",
    )
    registry.register(
        "claude.open",
        _open,
        "Open a session's dev server in the browser.",
        {"which": "the session or app name; omit when there is only one"},
        needs_desktop=True,
    )


# -- helpers ------------------------------------------------------------------


def _watch(ctx: CommandContext):
    watch = getattr(ctx.collector, "claude", None)
    if watch is None or not ctx.config.claude.enabled:
        raise CommandError("Watching Claude Code sessions is switched off. Set claude.enabled in the config.")
    return watch


def _sessions(ctx: CommandContext) -> list[Session]:
    watch = _watch(ctx)
    # A one-shot exec has no watch thread; make sure the picture is current.
    if watch._thread is None:
        watch.poll()
    return watch.listed()


def _pick(ctx: CommandContext, which: str, session_id: str = "") -> Session:
    from ..monitors.claude import match_session

    sessions = _sessions(ctx)
    if not sessions:
        raise CommandError("There are no Claude Code sessions open at the moment.")
    if session_id:
        # The dashboard knows exactly which block it is sending from.
        for session in sessions:
            if session.id == session_id:
                return session
        raise CommandError("That session is no longer listed.")
    found = match_session(which, sessions)
    if found is not None:
        return found
    names = [s.name for s in sessions[:6]]
    if which.strip():
        raise CommandError(
            f"I don't see a session called {which}. I can see {join_speech(names, 'and')}."
        )
    raise CommandError(f"Which one? I can see {join_speech(names, 'and')}.")


def _doing(session: Session, *, brief: bool = False) -> str:
    """'is running the tests, 2 minutes in' - the state as a clause."""
    if session.turn == "waiting":
        what = session.waiting_for or "your answer"
        return f"is waiting on you, {what}"
    if session.turn == "busy":
        step = session.activity or "working"
        if step == "thinking":
            step = "thinking"
        elapsed = duration_speech(session.current.seconds) if session.current.started else ""
        tail = f", {elapsed} in" if elapsed and not brief else ""
        return f"is {step}{tail}" if step.startswith(("thinking", "working", "retrying")) else f"is on {step}{tail}"
    if session.last_reply_at:
        ago = duration_speech(max(0.0, session.quiet_seconds))
        return f"is idle, last answered {ago} ago"
    return "is idle"


def _apps_speech(apps: list[dict]) -> str:
    parts = []
    for app in apps:
        state = "answering" if app.get("status") == "up" else "not answering" if app.get("status") == "down" else "listening"
        label = app.get("title") or app.get("process") or "something"
        parts.append(f"{label} on port {app['port']}, {state}")
    return join_speech(parts, "and")


# -- handlers -----------------------------------------------------------------


def _list(ctx: CommandContext, args: dict) -> CommandResult:
    sessions = _sessions(ctx)
    rows = [s.to_dict() for s in sessions]
    if not sessions:
        return CommandResult(
            speech="No Claude Code sessions have been active lately.",
            result={"sessions": [], "count": 0},
        )
    lines = []
    for session in sessions[:5]:
        where = f" in {session.where}" if session.where and session.where != "unknown" else ""
        lines.append(f"{session.name}{where} {_doing(session, brief=True)}")
    more = len(sessions) - 5
    speech = f"{count_speech(len(sessions), 'Claude Code session')}: " + "; ".join(lines) + "."
    if more > 0:
        speech += f" And {more} more."
    return CommandResult(speech=speech, result={"sessions": rows, "count": len(rows)})


def _status(ctx: CommandContext, args: dict) -> CommandResult:
    session = _pick(ctx, str(args.get("which") or ""))
    bits = [f"{session.name} {_doing(session)}"]
    if session.last_prompt:
        bits.append(f"You last asked it to {session.last_prompt[:160]}".rstrip("."))
    if session.turn == "idle" and session.last_reply:
        from ..monitors.claude import _excerpt, _strip_markup

        bits.append(f"It said: {_excerpt(_strip_markup(session.last_reply), 240)}")
    if session.apps:
        bits.append(f"It has {_apps_speech(session.apps)}")
    speech = ". ".join(b.rstrip(".") for b in bits) + "."
    return CommandResult(speech=speech, result=session.to_dict())


def _prompt(ctx: CommandContext, args: dict) -> CommandResult:
    cfg = ctx.config.claude
    if not cfg.allow_prompt:
        raise CommandError("Prompting sessions is switched off. Set claude.allow_prompt in the config.")
    text = arg_str(args, "text")
    if len(text) < 3:
        raise CommandError("Tell me what to say to it.")
    session = _pick(ctx, str(args.get("which") or ""), str(args.get("session") or ""))

    home = Path(cfg.home) if cfg.home else claude_home()
    log_dir = Path(ctx.config.state_file).parent if ctx.config.state_file else None
    try:
        outcome = claude_link.deliver(
            session, text, cfg, home=home, log_dir=log_dir, sender=ctx.config.assistant_name(),
        )
    except claude_link.LinkError as exc:
        raise CommandError(str(exc)) from exc

    log.info("prompted the %s session (%s) via %s", session.name, session.id[:8], outcome.get("via"))
    if outcome.get("via") == "pipe":
        speech = f"Told the {session.name} session. I'll let you know when it answers."
    else:
        speech = (
            f"The {session.name} session isn't running, so I sent that through Claude Code "
            "directly. It'll be in the conversation when you open it, and I'll say when it's done."
        )
    return CommandResult(
        speech=speech,
        result={"session": session.id, "name": session.name, **outcome},
    )


def _apps(ctx: CommandContext, args: dict) -> CommandResult:
    sessions = _sessions(ctx)
    watch = _watch(ctx)
    section = watch.snapshot().get("claude_sessions", {})
    apps = section.get("apps", [])
    if not apps:
        return CommandResult(
            speech="None of the sessions has a dev server up right now.",
            result={"apps": []},
        )
    by_owner: list[str] = []
    for session in sessions:
        if session.apps:
            by_owner.append(f"{session.name} has {_apps_speech(session.apps)}")
    owned = {a["port"] for s in sessions for a in s.apps}
    stray = [a for a in apps if a["port"] not in owned]
    if stray:
        by_owner.append(f"and unattributed, {_apps_speech(stray)}")
    speech = f"{count_speech(len(apps), 'dev server')} up: " + "; ".join(by_owner) + "."
    return CommandResult(speech=speech, result={"apps": apps})


def _open(ctx: CommandContext, args: dict) -> CommandResult:
    which = str(args.get("which") or "").strip()
    sessions = _sessions(ctx)
    candidates: list[tuple[str, dict]] = []
    for session in sessions:
        for app in session.apps:
            candidates.append((session.name, app))
    if not candidates:
        raise CommandError("There is no dev server up to open.")

    chosen: tuple[str, dict] | None = None
    if which:
        lowered = which.lower()
        for name, app in candidates:
            hay = " ".join([name, app.get("title") or "", app.get("process") or "", str(app["port"])]).lower()
            if all(word in hay for word in lowered.split()):
                chosen = (name, app)
                break
        if chosen is None:
            session = _pick(ctx, which)
            ups = [a for a in session.apps if a.get("status") == "up"] or session.apps
            if not ups:
                raise CommandError(f"The {session.name} session has no dev server up.")
            chosen = (session.name, ups[0])
    elif len(candidates) == 1:
        chosen = candidates[0]
    else:
        ups = [c for c in candidates if c[1].get("status") == "up"]
        if len(ups) == 1:
            chosen = ups[0]
        else:
            names = [f"{n} on {a['port']}" for n, a in candidates[:5]]
            raise CommandError(f"Which one? There's {join_speech(names, 'and')}.")

    name, app = chosen
    import webbrowser

    webbrowser.open(app["url"])
    return CommandResult(
        speech=f"Opening {name} on port {app['port']}.",
        result={"url": app["url"], "session": name, "port": app["port"]},
    )
