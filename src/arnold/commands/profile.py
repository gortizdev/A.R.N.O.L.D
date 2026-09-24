"""Which assistant this PC is.

A profile bundles everything that makes the assistant someone - name, voice,
persona, wake word, the face's colour and shape - under one name, so "be
Jarvis for a while" is one change rather than six. `profile.use` is the only
writer: it edits the `profile:` line in config.yaml and applies the profile
to the running config in the same breath. Every long-lived process watches
the file, so the voice session, the face and the agent follow by themselves.

Reachable from the CLI (`arnold profile use jarvis`), from
Jarvis over SSH like any other command, from the dashboard, and from the
voice session through the `switch_profile` tool.
"""

from __future__ import annotations

import logging

from ..config import ConfigError, set_active_profile_in_file
from ..humanize import join_speech
from .registry import CommandContext, CommandError, CommandResult, Registry, arg_str

log = logging.getLogger(__name__)


def register_all(registry: Registry) -> None:
    registry.register(
        "profile.list",
        _list,
        "Every identity this PC can be, and which one it is now.",
        {},
    )
    registry.register(
        "profile.show",
        _show,
        "The identity currently in use: name, voice, wake word, face.",
        {},
    )
    registry.register(
        "profile.use",
        _use,
        "Become a different identity. Saved to config; running processes follow.",
        {"name": "which profile, from profile.list"},
    )


def _describe(ctx: CommandContext, name: str, profile) -> dict:
    config = ctx.config
    builtin = name in _builtin_names()
    return {
        "name": name,
        "active": name == config.active_profile,
        "builtin": builtin,
        "display_name": profile.name or ("Jarvis" if profile.mirror_jarvis else config.assistant.name),
        "voice": profile.voice,
        "wake_word": profile.wake_word,
        "palette": profile.palette,
        "design": profile.design,
        "mirror_jarvis": profile.mirror_jarvis,
    }


def _builtin_names() -> set[str]:
    from ..config import BUILTIN_PROFILES

    return set(BUILTIN_PROFILES)


def _profiles(ctx: CommandContext) -> dict:
    try:
        return ctx.config.profiles()
    except ConfigError as exc:
        raise CommandError(f"The profiles in the config are not usable: {exc}") from exc


def _current(ctx: CommandContext) -> dict:
    config = ctx.config
    return {
        "profile": config.active_profile,
        "name": config.assistant_name(),
        "voice": config.assistant.voice if not config.assistant.mirror_jarvis else "",
        "wake_word": config.voice.wake_word,
        "palette": config.face_palette(),
        "design": config.face_design(),
        "mirror_jarvis": bool(config.assistant.mirror_jarvis),
    }


def _list(ctx: CommandContext, args: dict) -> CommandResult:
    profiles = _profiles(ctx)
    entries = [_describe(ctx, name, profile) for name, profile in sorted(profiles.items())]
    names = [entry["display_name"] or entry["name"] for entry in entries]
    current = ctx.config.assistant_name()
    return CommandResult(
        speech=f"I can be {join_speech(names, 'or')}; I am {current} at the moment.",
        result={"profiles": entries, "current": _current(ctx)},
    )


def _show(ctx: CommandContext, args: dict) -> CommandResult:
    current = _current(ctx)
    which = f" (the {current['profile']} profile)" if current["profile"] else ""
    return CommandResult(
        speech=(
            f"I'm {current['name']}{which}, answering to {_spoken(current['wake_word'])}."
        ),
        result=current,
    )


def _use(ctx: CommandContext, args: dict) -> CommandResult:
    wanted = arg_str(args, "name").strip().lower().replace(" ", "_").replace("-", "_")
    profiles = _profiles(ctx)
    if wanted not in profiles:
        # "be jarvis" arrives as the display name as often as the key.
        by_display = {
            (p.name or "").strip().lower(): key for key, p in profiles.items() if p.name
        }
        wanted = by_display.get(wanted, wanted)
    if wanted not in profiles:
        raise CommandError(
            f"I don't have a profile called {args.get('name')}. "
            f"I have: {join_speech(sorted(profiles), 'and')}."
        )

    config = ctx.config
    if config.source_path is None:
        raise CommandError("I don't know which config file to change - this config was not loaded from disk.")
    try:
        set_active_profile_in_file(config.source_path, wanted)
    except (ConfigError, OSError) as exc:
        raise CommandError(f"I couldn't update the config file: {exc}") from exc

    # In place, so everything holding this Config sees the new identity now.
    config.apply_profile(wanted)
    current = _current(ctx)
    log.info("profile switched to %s (%s, wake word %s)", wanted, current["name"], current["wake_word"])
    return CommandResult(
        speech=(
            f"Done - I'm {current['name']} from the next conversation. "
            f"Say {_spoken(current['wake_word'])}."
        ),
        result={**current, "restart_required": False},
    )


def _spoken(wake_word: str) -> str:
    """`hey_athena` -> 'hey Athena'; a path -> its stem, the same way."""
    stem = wake_word.replace("\\", "/").rsplit("/", 1)[-1]
    for suffix in (".onnx", ".tflite"):
        if stem.lower().endswith(suffix):
            stem = stem[: -len(suffix)]
    words = [w for w in stem.replace("-", "_").split("_") if w]
    if not words:
        return "the wake word"
    return " ".join([words[0]] + [w.capitalize() for w in words[1:]])
