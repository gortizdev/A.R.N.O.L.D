"""Meta commands: liveness and capability discovery."""

from __future__ import annotations

import platform
import time

from .. import __version__
from .registry import CommandContext, CommandResult, Registry


def register_all(registry: Registry) -> None:
    registry.register("system.ping", _ping, "Check the agent is alive and responding.")
    registry.register(
        "system.capabilities",
        _capabilities,
        "List every command this agent accepts, with arguments.",
    )
    registry.register("system.info", _info, "Static facts about the machine (OS, hostname, CPU).")


def _ping(ctx: CommandContext, args: dict) -> CommandResult:
    return CommandResult(
        speech=f"{ctx.device_name} is online.",
        result={"pong": True, "ts": time.time(), "device": ctx.config.device.id},
    )


def _capabilities(ctx: CommandContext, args: dict) -> CommandResult:
    from .registry import build_registry

    commands = build_registry().describe()
    allowed = [c for c in commands if not c["destructive"] or ctx.config.security.allow_destructive]
    return CommandResult(
        speech=f"I support {len(allowed)} commands on {ctx.device_name}.",
        result={
            "commands": commands,
            "destructive_enabled": ctx.config.security.allow_destructive,
            "agent_version": __version__,
        },
    )


def _info(ctx: CommandContext, args: dict) -> CommandResult:
    snap = ctx.collector.snapshot(include_window=False)
    cpu = snap["cpu"]
    info = {
        "device_id": ctx.config.device.id,
        "friendly_name": ctx.config.device.friendly_name,
        "hostname": platform.node(),
        "os": f"{platform.system()} {platform.release()}",
        "os_version": platform.version(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "cores_physical": cpu["cores_physical"],
        "cores_logical": cpu["cores_logical"],
        "python": platform.python_version(),
        "agent_version": __version__,
    }
    speech = (
        f"{ctx.device_name} runs {info['os']} on {info['cores_physical']} physical cores, "
        f"{info['cores_logical']} logical."
    )
    return CommandResult(speech=speech, result=info)
