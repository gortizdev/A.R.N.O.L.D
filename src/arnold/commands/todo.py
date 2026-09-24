"""The to-do list: what is on it, adding to it, ticking things off, and
pulling in the week's document.

Everything the dashboard's list can do goes through here, so the page has no
route of its own into the file and "what's on my list" works the same from
the voice session, the shell, and the Pi.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..graph_mail import MailUnavailable
from ..humanize import count_speech, join_speech
from ..todos import (
    MANUAL_SOURCE,
    DocumentError,
    TodoList,
    TodoSync,
    claude_projects,
    compose_prompt,
    match_project,
    todo_path,
)
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
        "todo.list",
        _list,
        "What is on the to-do list, open items first.",
        {"all": "true to include what is already done"},
    )
    registry.register(
        "todo.add",
        _add,
        "Put something on the to-do list.",
        {"text": "what needs doing", "section": "optional group it belongs under"},
    )
    registry.register(
        "todo.done",
        _done,
        "Tick something off the list, or untick it.",
        {"which": "its words or id", "done": "false to reopen it"},
    )
    registry.register(
        "todo.remove",
        _remove,
        "Take something off the list altogether, or 'done' to clear everything finished.",
        {"which": "its words, its id, or 'done'"},
    )
    registry.register(
        "todo.import",
        _import,
        "Read a document (Word, PDF or text) and put its items on the list.",
        {"path": "the file", "source": "what to call it; blank = the weekly document"},
    )
    registry.register(
        "todo.sync",
        _sync,
        "Look for this week's document, in the inbox and the watched folders, and take it in.",
    )
    registry.register(
        "todo.projects",
        _projects,
        "Which Claude Code project each group on the list belongs to, and the projects it could be.",
    )
    registry.register(
        "todo.link",
        _link,
        "Tie a group on the list to a Claude Code project, or 'none' to untie it.",
        {"section": "the group, as the list shows it", "project": "the project's name or folder, or 'none'"},
    )
    registry.register(
        "todo.send",
        _send,
        "Hand a task to the Claude Code session of its project.",
        {"which": "the task's words or id", "text": "optional: what to say instead of the task itself"},
    )
    registry.register(
        "todo.mail_login",
        _mail_login,
        "Sign in to the inbox so the weekly document can be read straight from the email. "
        "Gives a code to enter at microsoft.com/devicelogin.",
    )
    registry.register(
        "todo.mail_status",
        _mail_status,
        "Whether the inbox is signed in, and what it last found.",
    )
    registry.register(
        "todo.mail_logout",
        _mail_logout,
        "Forget the inbox sign-in on this PC.",
    )


def _list_for(ctx: CommandContext) -> TodoList:
    if not ctx.config.todo.enabled:
        raise CommandError("The to-do list is switched off on this PC.")
    return TodoList(todo_path(ctx.config), ctx.config.todo.max_items)


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def _list(ctx: CommandContext, args: dict) -> CommandResult:
    todos = _list_for(ctx)
    include_done = _truthy(args.get("all"))
    items = todos.items()
    open_items = [i for i in items if not i.done]
    shown = items if include_done else open_items
    result = {
        "items": [i.to_dict() for i in shown],
        "open": len(open_items),
        "done": len(items) - len(open_items),
    }
    if not open_items:
        speech = (
            "Nothing on the list."
            if not items
            else f"Everything on the list is done, all {len(items)} of it."
        )
        return CommandResult(speech=speech, result=result)
    names = [i.text for i in open_items[:5]]
    more = len(open_items) - len(names)
    speech = f"{count_speech(len(open_items), 'thing', 'things')} to do: {join_speech(names)}"
    speech += f", and {more} more." if more > 0 else "."
    return CommandResult(speech=speech, result=result)


def _add(ctx: CommandContext, args: dict) -> CommandResult:
    todos = _list_for(ctx)
    text = arg_str(args, "text")
    section = str(args.get("section") or "").strip()
    try:
        item = todos.add(text, section=section, source=MANUAL_SOURCE)
    except ValueError as exc:
        raise CommandError(str(exc)) from exc
    return CommandResult(speech=f"Added {item.text}.", result={"item": item.to_dict()})


def _done(ctx: CommandContext, args: dict) -> CommandResult:
    todos = _list_for(ctx)
    which = arg_str(args, "which")
    done = True if args.get("done") in (None, "") else _truthy(args.get("done"))
    item = todos.set_done(which, done)
    if item is None:
        raise CommandError(f"I couldn't find {which} on the list.")
    verb = "Ticked off" if done else "Reopened"
    return CommandResult(speech=f"{verb} {item.text}.", result={"item": item.to_dict()})


def _remove(ctx: CommandContext, args: dict) -> CommandResult:
    todos = _list_for(ctx)
    which = arg_str(args, "which")
    if which.lower() in ("done", "finished", "completed"):
        count = todos.clear_done()
        return CommandResult(
            speech=f"Cleared {count_speech(count, 'finished item', 'finished items')}.",
            result={"removed": count},
        )
    item = todos.remove(which)
    if item is None:
        raise CommandError(f"I couldn't find {which} on the list.")
    return CommandResult(speech=f"Removed {item.text}.", result={"item": item.to_dict()})


def _import(ctx: CommandContext, args: dict) -> CommandResult:
    todos = _list_for(ctx)
    path = Path(arg_str(args, "path")).expanduser()
    source = str(args.get("source") or "").strip() or ctx.config.todo.source_name
    try:
        report = todos.import_file(path, source, force=True)
    except DocumentError as exc:
        raise CommandError(str(exc)) from exc
    speech = (
        f"Took {count_speech(report.total, 'item', 'items')} from {path.name}"
        + (f", the update dated {report.dated}" if report.dated else "")
        + (f", {report.added} of them new." if report.kept else ".")
    )
    return CommandResult(speech=speech, result=_report_dict(report))


def _sync(ctx: CommandContext, args: dict) -> CommandResult:
    if not ctx.config.todo.enabled:
        raise CommandError("The to-do list is switched off on this PC.")
    sync = TodoSync(ctx.config)
    report = sync.sync(force=True)
    status = sync.status()
    if report is None:
        mail = status.get("mail") or {}
        if status.get("error"):
            raise CommandError(status["error"])
        if mail.get("error"):
            raise CommandError(f"The inbox could not be read: {mail['error']}")
        last = status.get("last") or {}
        if last:
            speech = f"Nothing new. The list is from {last.get('name')}."
        elif mail and not mail.get("signed_in"):
            speech = (
                f"No {ctx.config.todo.source_name} document yet, and the inbox is not signed in. "
                "Say todo.mail_login, or run arnold mail login, and I'll read it from the email."
            )
        elif mail:
            speech = (
                f"No {ctx.config.todo.source_name} email in the inbox in the last "
                f"{ctx.config.todo.mail.lookback_days} days, and no document in the folders."
            )
        else:
            speech = (
                f"No {ctx.config.todo.source_name} document yet. Open its attachment in "
                "Outlook, or save it to the email attachments folder, and I'll pick it up."
            )
        return CommandResult(speech=speech, result={"status": status})
    if report.skipped:
        speech = f"Already have that one: {Path(report.file).name}."
    else:
        speech = (
            f"Took {count_speech(report.total, 'item', 'items')} from "
            f"{Path(report.file).name}"
            + (f", the update dated {report.dated}" if report.dated else "")
            + f", {report.added} new."
        )
    return CommandResult(speech=speech, result={**_report_dict(report), "status": status})


def _claude_section(ctx: CommandContext) -> dict:
    watch = getattr(ctx.collector, "claude", None)
    if watch is None or not ctx.config.claude.enabled:
        return {}
    try:
        if getattr(watch, "_thread", None) is None:
            watch.poll()
        return watch.snapshot().get("claude_sessions", {})
    except Exception as exc:  # the list must still answer without it
        log.debug("could not read the Claude sessions: %s", exc)
        return {}


def _projects(ctx: CommandContext, args: dict) -> CommandResult:
    todos = _list_for(ctx)
    projects = claude_projects(ctx.config, _claude_section(ctx))
    links = todos.resolve_links(projects)
    lines = []
    for section in todos.sections():
        link = links.get(section)
        if link:
            lines.append(f"{section} is {link['project']}" + (" (a guess)" if link["auto"] else ""))
        else:
            lines.append(f"{section} has no project")
    speech = ("; ".join(lines) + ".") if lines else "Nothing on the list has a group."
    return CommandResult(speech=speech, result={"links": links, "projects": projects})


def _link(ctx: CommandContext, args: dict) -> CommandResult:
    todos = _list_for(ctx)
    section = arg_str(args, "section")
    wanted = str(args.get("project") or "").strip()
    known = [s for s in todos.sections() if s.lower() == section.lower()]
    if not known:
        found = [s for s in todos.sections() if section.lower() in s.lower()]
        if len(found) != 1:
            raise CommandError(f"I don't see a group called {section} on the list.")
        known = found
    section = known[0]
    if wanted.lower() in ("", "none", "nothing", "no project", "unlink"):
        todos.link(section, "", "")  # an explicit nothing, so it is not guessed either
        return CommandResult(speech=f"{section} is no longer tied to a project.", result={"section": section})
    projects = claude_projects(ctx.config, _claude_section(ctx))
    chosen = next((p for p in projects if p["cwd"].lower() == wanted.lower()), None) or match_project(wanted, projects)
    if chosen is None:
        names = [p["project"] for p in projects[:8]]
        raise CommandError(
            f"I don't know a project called {wanted}."
            + (f" I can see {join_speech(names, 'and')}." if names else "")
        )
    record = todos.link(section, chosen["project"], chosen["cwd"])
    return CommandResult(
        speech=f"{section} is now {chosen['project']}.",
        result={"section": section, "link": record},
    )


def _send(ctx: CommandContext, args: dict) -> CommandResult:
    from .claude import _prompt

    todos = _list_for(ctx)
    which = arg_str(args, "which")
    item = todos.find(which)
    if item is None:
        raise CommandError(f"I couldn't find {which} on the list.")
    projects = claude_projects(ctx.config, _claude_section(ctx))
    link = todos.resolve_links(projects).get(item.section) if item.section else None
    if not link:
        raise CommandError(
            f"{item.section or 'That'} isn't tied to a Claude Code project yet. "
            "Say todo.link to pick one."
        )
    text = str(args.get("text") or "").strip() or compose_prompt(item, ctx.config.todo.source_name)
    prompt_args = {"text": text, "which": link["project"]}
    if link.get("session_id"):
        prompt_args["session"] = link["session_id"]
    result = _prompt(ctx, prompt_args)
    return CommandResult(
        speech=f"Sent {item.text[:60]} to {link['project']}. {result.speech}",
        result={"item": item.to_dict(), "project": link, **(result.result or {})},
    )


def _mail_for(ctx: CommandContext):
    if not ctx.config.todo.enabled:
        raise CommandError("The to-do list is switched off on this PC.")
    if not ctx.config.todo.mail.enabled:
        raise CommandError("Reading the inbox is switched off (todo.mail.enabled).")
    return TodoSync(ctx.config).mail


def _spoken_code(code: str) -> str:
    return " ".join(code)


def _mail_login(ctx: CommandContext, args: dict) -> CommandResult:
    from .. import runtime

    mail = _mail_for(ctx)
    try:
        if mail.signed_in():
            account = (mail.account() or {}).get("username", "")
            return CommandResult(
                speech=f"Already signed in{' as ' + account if account else ''}.",
                result={"signed_in": True, "account": account},
            )
        info = mail.begin_login()
    except MailUnavailable as exc:
        raise CommandError(str(exc)) from exc

    if not runtime.IS_RESIDENT:
        # A one-shot process would exit before the browser step finished, so
        # wait for it here; the CLI's `mail login` prints the code first.
        ok = mail.login_blocking()
        status = mail.status()
        if not ok:
            raise CommandError(f"The sign-in did not complete: {status.get('error') or 'no answer'}.")
        return CommandResult(
            speech=f"Signed in to the inbox as {status.get('account') or 'you'}.",
            result={"signed_in": True, "account": status.get("account", "")},
        )
    return CommandResult(
        speech=(
            f"Go to {info['verification_uri']} and enter the code "
            f"{_spoken_code(info['user_code'])}. I'll say when it's done."
        ),
        result={
            "signed_in": False,
            "user_code": info["user_code"],
            "verification_uri": info["verification_uri"],
            "message": info.get("message", ""),
            "expires_at": info.get("expires_at"),
        },
    )


def _mail_status(ctx: CommandContext, args: dict) -> CommandResult:
    sync = TodoSync(ctx.config)
    if sync.mail is None:
        raise CommandError("Reading the inbox is switched off (todo.mail.enabled).")
    status = sync.status()
    mail = status.get("mail") or {}
    if not mail.get("available"):
        speech = f"The inbox can't be read: {mail.get('error')}"
    elif mail.get("pending"):
        speech = (
            f"A sign-in is waiting: go to {mail['pending']['verification_uri']} and enter "
            f"{_spoken_code(mail['pending']['user_code'])}."
        )
    elif not mail.get("signed_in"):
        speech = "Not signed in to the inbox. Say todo.mail_login to start."
    else:
        speech = f"Signed in as {mail.get('account') or 'you'}."
        last = mail.get("last_message") or {}
        if last:
            speech += f" The newest {ctx.config.todo.source_name} email is '{last.get('subject')}' from {last.get('from')}."
        if mail.get("error"):
            speech += f" Last problem: {mail['error']}"
    return CommandResult(speech=speech, result={"mail": mail, "week": status})


def _mail_logout(ctx: CommandContext, args: dict) -> CommandResult:
    mail = _mail_for(ctx)
    try:
        mail.logout()
    except MailUnavailable as exc:
        raise CommandError(str(exc)) from exc
    return CommandResult(speech="Forgot the inbox sign-in.", result={"signed_in": False})


def _report_dict(report) -> dict:
    return {
        "source": report.source, "file": report.file, "added": report.added,
        "kept": report.kept, "dropped": report.dropped, "skipped": report.skipped,
        "total": report.total, "dated": report.dated,
    }
