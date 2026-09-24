"""Command surface shared by every transport (MQTT, SSH CLI, local selftest)."""

from .registry import CommandContext, CommandResult, Registry, build_registry

__all__ = ["CommandContext", "CommandResult", "Registry", "build_registry"]
