"""Command registration and dispatch.

Handlers return a `CommandResult` carrying both machine-readable `result` data
and a `speech` string. The speech string is the point of the whole design: it
lets Jarvis answer "how much disk is left?" by reading a field rather than
having to summarise JSON itself.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from ..alerts import AlertEngine
from ..config import Config
from ..monitors.collector import Collector
from ..security import DESTRUCTIVE_COMMANDS

log = logging.getLogger(__name__)


class CommandError(Exception):
    """Raised by a handler when the request is bad or cannot be carried out.

    The message is spoken back to the user, so phrase it as a sentence.
    """


@dataclass(slots=True)
class CommandContext:
    config: Config
    collector: Collector
    alerts: AlertEngine
    jarvis: Any = None  # JarvisClient; typed loosely to avoid a circular import
    # speech.Voice: says things in whichever room speech.route calls for.
    # `jarvis` stays the Pi specifically, because tell_jarvis means the Pi.
    speech: Any = None

    @property
    def device_name(self) -> str:
        return self.config.device.friendly_name


@dataclass(slots=True)
class CommandResult:
    ok: bool = True
    speech: str = ""
    result: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"ok": self.ok}
        if self.speech:
            payload["speech"] = self.speech
        if self.result:
            payload["result"] = self.result
        if self.error:
            payload["error"] = self.error
        return payload

    @classmethod
    def failure(cls, message: str) -> "CommandResult":
        return cls(ok=False, error=message, speech=message)


Handler = Callable[[CommandContext, dict[str, Any]], CommandResult]


@dataclass(slots=True)
class Command:
    name: str
    handler: Handler
    description: str
    args: dict[str, str] = field(default_factory=dict)
    # True when the handler only works on the logged-on desktop. A command
    # arriving over SSH runs in session 0, where a window opens invisibly and
    # the clipboard is a different clipboard; `exec` hands these to the agent
    # running in the logon session instead. See platform_win.session.
    needs_desktop: bool = False
    # True when the handler starts background work, so it has to run in
    # the resident agent rather than a one-shot process that is about to
    # exit. See arnold.runtime.
    needs_agent: bool = False

    @property
    def destructive(self) -> bool:
        return self.name in DESTRUCTIVE_COMMANDS


class Registry:
    def __init__(self) -> None:
        self._commands: dict[str, Command] = {}

    def register(
        self,
        name: str,
        handler: Handler,
        description: str,
        args: dict[str, str] | None = None,
        *,
        needs_desktop: bool = False,
        needs_agent: bool = False,
    ) -> None:
        if name in self._commands:
            raise ValueError(f"command {name!r} is already registered")
        self._commands[name] = Command(
            name, handler, description, args or {}, needs_desktop, needs_agent
        )

    def command(
        self,
        name: str,
        description: str,
        args: dict[str, str] | None = None,
        *,
        needs_desktop: bool = False,
    ):
        """Decorator form of `register`."""

        def wrap(handler: Handler) -> Handler:
            self.register(name, handler, description, args, needs_desktop=needs_desktop)
            return handler

        return wrap

    def names(self) -> list[str]:
        return sorted(self._commands)

    def get(self, name: str) -> Command | None:
        return self._commands.get(name)

    def describe(self) -> list[dict[str, Any]]:
        return [
            {
                "name": cmd.name,
                "description": cmd.description,
                "args": cmd.args,
                "destructive": cmd.destructive,
                "needs_desktop": cmd.needs_desktop,
                "needs_agent": cmd.needs_agent,
            }
            for cmd in sorted(self._commands.values(), key=lambda c: c.name)
        ]

    def dispatch(
        self, name: str, args: dict[str, Any], ctx: CommandContext
    ) -> CommandResult:
        command = self._commands.get(name)
        if command is None:
            close = [n for n in self._commands if n.split(".")[-1] == name.split(".")[-1]]
            hint = f" Did you mean {close[0]}?" if close else ""
            return CommandResult.failure(f"I don't know the command {name}.{hint}")

        try:
            return command.handler(ctx, args or {})
        except CommandError as exc:
            log.warning("command %s rejected: %s", name, exc)
            return CommandResult.failure(str(exc))
        except Exception as exc:  # a handler bug must not take the service down
            log.exception("command %s raised", name)
            return CommandResult.failure(f"{name} failed: {exc}")


def build_registry() -> Registry:
    """Construct the registry with every built-in command attached."""
    from . import (
        artifact, claude, clock, code, control, desktop, memory, printer, profile, query,
        schedule, system, timer, todo, weather, web,
    )

    registry = Registry()
    for module in (
        system, query, control, desktop, web, artifact, code, claude, memory,
        schedule, profile, timer, todo, clock, weather, printer,
    ):
        module.register_all(registry)
    return registry


# -- shared argument helpers ------------------------------------------------


def arg_str(args: dict[str, Any], key: str, default: str | None = None) -> str:
    value = args.get(key, default)
    if value is None:
        raise CommandError(f"I need a {key} for that.")
    if not isinstance(value, str):
        value = str(value)
    value = value.strip()
    if not value:
        raise CommandError(f"I need a {key} for that.")
    return value


def arg_int(
    args: dict[str, Any],
    key: str,
    default: int | None = None,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    raw = args.get(key, default)
    if raw is None:
        raise CommandError(f"I need a {key} for that.")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise CommandError(f"{key} should be a number, but I got {raw!r}.") from None
    if minimum is not None and value < minimum:
        raise CommandError(f"{key} must be at least {minimum}.")
    if maximum is not None and value > maximum:
        raise CommandError(f"{key} must be at most {maximum}.")
    return value


def arg_bool(args: dict[str, Any], key: str, default: bool | None = None) -> bool | None:
    raw = args.get(key, default)
    if raw is None or isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        lowered = raw.strip().lower()
        if lowered in ("true", "yes", "on", "1"):
            return True
        if lowered in ("false", "no", "off", "0"):
            return False
    raise CommandError(f"{key} should be true or false, but I got {raw!r}.")
