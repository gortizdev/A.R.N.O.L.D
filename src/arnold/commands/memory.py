"""Remembering things between conversations.

These are the only commands here that write something the user said rather
than something the machine measured, so they are phrased to be spoken: a
confirmation short enough not to interrupt the conversation that produced it,
and a refusal that says what to do instead.

Storage, matching and the prompt block live in `arnold.memory`;
this is the surface Jarvis and the realtime session call.
"""

from __future__ import annotations

import logging

from .. import memory as store_module
from ..humanize import join_speech
from .registry import (
    CommandContext,
    CommandError,
    CommandResult,
    Registry,
    arg_int,
    arg_str,
)

log = logging.getLogger(__name__)

# Long enough for anything worth keeping, short enough that a mis-heard
# monologue cannot become a fact the size of a page.
MAX_TEXT = 400


def register_all(registry: Registry) -> None:
    registry.register(
        "memory.remember",
        _remember,
        "Keep something in mind for future conversations.",
        {
            "text": "the fact, phrased so it still makes sense next month",
            "tags": "optional labels, comma separated or a list",
        },
    )
    registry.register(
        "memory.recall",
        _recall,
        "Look up what is known about something.",
        {"query": "what to look for; omit for the most recent", "limit": "how many"},
    )
    registry.register(
        "memory.forget",
        _forget,
        "Drop a fact.",
        {"query": "which one", "id": "or its id, from memory.list"},
    )
    registry.register(
        "memory.list",
        _list,
        "Everything currently remembered, newest first.",
        {"limit": "how many"},
    )


def _store(ctx: CommandContext):
    if not ctx.config.memory.enabled:
        raise CommandError("My memory is switched off in the config.")
    return store_module.store_for(ctx.config)


def _tags(args: dict) -> list[str]:
    raw = args.get("tags") or []
    if isinstance(raw, str):
        raw = raw.replace(";", ",").split(",")
    return [str(tag).strip() for tag in raw if str(tag).strip()]


def _remember(ctx: CommandContext, args: dict) -> CommandResult:
    text = arg_str(args, "text")
    if len(text) > MAX_TEXT:
        raise CommandError("That's more than I can keep as one fact - give me the short version.")

    fact, is_new = _store(ctx).remember(text, _tags(args), source=str(args.get("source") or "voice"))
    log.info("remembered %s: %s", fact.id, fact.text)
    return CommandResult(
        speech="I'll remember that." if is_new else "I already had that, but it's fresh now.",
        result={"remembered": fact.describe(), "new": is_new},
    )


def _recall(ctx: CommandContext, args: dict) -> CommandResult:
    query = str(args.get("query") or "").strip()
    limit = arg_int(args, "limit", 5, minimum=1, maximum=25)
    found = _store(ctx).recall(query, limit)

    if not found:
        return CommandResult(
            speech=(
                f"I don't have anything about {query}." if query
                else "I haven't been told anything worth keeping yet."
            ),
            result={"query": query, "facts": []},
        )

    # Spoken back as sentences rather than a list: this is read aloud, and
    # "one, ... two, ..." is how a database talks, not an assistant.
    speech = join_speech([fact.text.rstrip(".") for fact in found[:3]])
    return CommandResult(
        speech=speech + ".",
        result={"query": query, "facts": [fact.describe() for fact in found]},
    )


def _forget(ctx: CommandContext, args: dict) -> CommandResult:
    query = str(args.get("query") or "").strip()
    fact_id = str(args.get("id") or "").strip()
    if not query and not fact_id:
        raise CommandError("Tell me what to forget.")

    gone = _store(ctx).forget(query, fact_id)
    if not gone:
        return CommandResult.failure(
            f"I don't have anything about {query}." if query else "There's no fact with that id."
        )
    if len(gone) == 1:
        return CommandResult(
            speech=f"Forgotten: {gone[0].text}",
            result={"forgotten": [fact.describe() for fact in gone]},
        )
    return CommandResult(
        speech=f"Forgotten all {len(gone)} of them.",
        result={"forgotten": [fact.describe() for fact in gone]},
    )


def _list(ctx: CommandContext, args: dict) -> CommandResult:
    limit = arg_int(args, "limit", 20, minimum=1, maximum=200)
    facts = _store(ctx).recent(limit)
    total = _store(ctx).count()
    if not facts:
        return CommandResult(speech="I'm not holding on to anything yet.", result={"facts": []})
    return CommandResult(
        speech=f"I'm keeping {total} thing{'s' if total != 1 else ''} in mind.",
        result={"facts": [fact.describe() for fact in facts], "count": total},
    )
