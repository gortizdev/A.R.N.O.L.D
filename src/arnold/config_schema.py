"""What the console's settings form is drawn from.

The form is generated, not written by hand: every section of Config is a
dataclass, so its fields, types and defaults are already here, and the
comment above each field in config.py is its help text. Only what the types
cannot say is listed by hand - which strings have a fixed set of values
(CHOICES) and which hold secrets.

Values come from the file as written, not from the loaded Config: a profile
lays its fields over the flat ones at load time, and showing those merged
values would write the profile's identity into the flat fields the moment
anything else in the section was saved.
"""

from __future__ import annotations

import ast
import dataclasses
import functools
import inspect
import textwrap
import typing
from typing import Any

from .config import Config

# Strings with a fixed set of values. "" in a list means blank is allowed and
# means something (usually "follow the older setting" or "pick for me").
CHOICES: dict[str, list[str]] = {
    "log_level": ["DEBUG", "INFO", "WARNING", "ERROR"],
    "assistant.voice": ["alloy", "ash", "ballad", "coral", "echo", "sage", "shimmer",
                        "verse", "marin", "cedar"],
    "jarvis.speech_route": ["home_assistant", "ssh", "none"],
    "speech.route": ["", "jarvis", "local", "both", "auto", "none"],
    "printer.filament": ["PLA", "PETG", "ABS", "ASA", "TPU"],
    "printer.sculpt_views": ["front-side-back", "front-back", "front"],
    "printer.sculpt_backend": ["auto", "local", "space"],
    "speech.backend": ["auto", "openai", "piper"],
    "face.style": ["holo", "orb"],
    "face.position": ["top-left", "top-right", "bottom-left", "bottom-right", "center"],
    "face.palette": ["", "gold", "steel"],
    "face.design": ["", "core", "lattice"],
    "voice.mode": ["realtime", "pipeline"],
    "voice.wake_ack": ["speech", "chime", "off"],
    "voice.turn_detection": ["semantic", "server"],
    "voice.turn_eagerness": ["low", "medium", "high", "auto"],
    "voice.stt_device": ["auto", "cpu", "cuda"],
    "voice.tts_backend": ["openai", "piper"],
    "voice.openai_tts_voice": ["alloy", "ash", "ballad", "coral", "echo", "fable", "onyx",
                               "nova", "sage", "shimmer", "verse", "marin", "cedar"],
    "voice.brain": ["jarvis", "local"],
    "weather.units": ["auto", "metric", "imperial"],
    "code.permission_mode": ["acceptEdits", "plan", "default", "bypassPermissions"],
    "claude.permission_mode": ["acceptEdits", "plan", "default", "bypassPermissions"],
    "claude.assume_permission_class": ["bypass", "prompting"],
    "proactive.judge": ["rules", "model"],
    "todo.weekday": ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"],
    "game.power_plan": ["", "high", "ultimate", "balanced"],
    "game.game_priority": ["", "high", "above_normal"],
}
# Values that are choices but may also be anything else (a plan's name, a
# pixel position): the picker offers the list and allows typing.
OPEN_CHOICES = {"face.position", "game.power_plan"}

SECRET_WORDS = ("password", "token", "secret", "access_code")
# Runtime bookkeeping, not settings.
HIDDEN = {"source_path", "active_profile"}
# Sections shown first, in this order; the rest follow as Config declares them.
ORDER = ["general", "assistant", "game", "speech", "voice", "face", "ui"]

TITLES = {
    "general": "General", "ui": "Console", "mqtt": "MQTT", "claude": "Claude Code",
    "code": "Code", "jarvis.home_assistant": "Jarvis: Home Assistant", "jarvis.ssh": "Jarvis: SSH",
    "todo.mail": "To-do: mail", "todo": "To-do", "game": "Game mode", "printer": "3D printer",
}


@functools.lru_cache(maxsize=None)
def _comments(cls: type) -> dict[str, tuple[str, str]]:
    """For each field of a dataclass: the comment block above it, as one
    line, and the heading of a ruler comment ("# -- Voice -----") that opens
    a group of fields there, if one does."""
    try:
        lines = inspect.getsource(cls).splitlines()
    except (OSError, TypeError):
        return {}
    tree = ast.parse(textwrap.dedent("\n".join(lines)))
    out: dict[str, tuple[str, str]] = {}
    body = tree.body[0].body if tree.body and isinstance(tree.body[0], ast.ClassDef) else []
    for node in body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            i, block, heading = node.lineno - 2, [], ""
            while i >= 0 and lines[i].strip().startswith("#"):
                text = lines[i].strip().lstrip("#").strip()
                if text.startswith("--") or text.startswith("=="):
                    heading = heading or text.strip("-= ").strip()
                elif text:
                    block.insert(0, text)
                i -= 1
            out[node.target.id] = (" ".join(block), heading)
    return out


def _doc(cls: type) -> str:
    doc = inspect.getdoc(cls) or ""
    return doc.split("\n\n")[0].replace("\n", " ") if cls.__doc__ else ""


def _kind(hint: Any) -> tuple[str, bool]:
    """The control a type gets, and whether blank (None) is allowed."""
    nullable = False
    origin = typing.get_origin(hint)
    if origin in (typing.Union, getattr(__import__("types"), "UnionType", None)):
        args = [a for a in typing.get_args(hint) if a is not type(None)]
        nullable = len(args) < len(typing.get_args(hint))
        hint = args[0] if len(args) == 1 else hint
        origin = typing.get_origin(hint)
    if hint is bool:
        return "bool", nullable
    if hint is int:
        return "int", nullable
    if hint is float:
        return "float", nullable
    if hint is str:
        return "str", nullable
    if origin is list and typing.get_args(hint) == (str,):
        return "list", nullable
    return "yaml", nullable  # mappings, lists of rules: edited as YAML


def _default(field: dataclasses.Field) -> Any:
    if field.default is not dataclasses.MISSING:
        return field.default
    if field.default_factory is not dataclasses.MISSING:  # type: ignore[misc]
        return field.default_factory()  # type: ignore[misc]
    return None


def _fields(cls: type, prefix: str) -> tuple[list[dict[str, Any]], list[tuple[str, type]]]:
    hints = typing.get_type_hints(cls)
    notes = _comments(cls)
    fields, nested = [], []
    for field in dataclasses.fields(cls):
        if field.name in HIDDEN:
            continue
        hint = hints[field.name]
        if dataclasses.is_dataclass(hint):
            nested.append((field.name, hint))
            continue
        path = prefix + field.name
        kind, nullable = _kind(hint)
        default = _default(field)
        help_text, heading = notes.get(field.name, ("", ""))
        entry: dict[str, Any] = {
            "path": path,
            "key": field.name,
            "type": kind,
            "nullable": nullable,
            "default": default if kind != "yaml" else None,
            "help": help_text,
        }
        if heading:
            entry["group"] = heading[:1].upper() + heading[1:]
        if path in CHOICES:
            entry["choices"] = CHOICES[path]
            entry["open"] = path in OPEN_CHOICES
        if kind == "str" and any(w in field.name for w in SECRET_WORDS):
            entry["secret"] = True
        fields.append(entry)
    return fields, nested


def schema(
    profiles: list[str] | None = None, extra_choices: dict[str, list[str]] | None = None
) -> list[dict[str, Any]]:
    """Every section of the config, each with its fields, for the form.

    `extra_choices` adds what only this machine knows - its power plans, say
    - to a field's list."""
    sections: list[dict[str, Any]] = []
    top, nested = _fields(Config, "")
    sections.append({"key": "general", "title": "General", "doc": "Logging and where state is kept.",
                     "fields": top})

    def add(name: str, cls: type) -> None:
        fields, children = _fields(cls, name + ".")
        sections.append({"key": name, "title": TITLES.get(name, name.replace("_", " ").capitalize()),
                         "doc": _doc(cls), "fields": fields})
        for child, child_cls in children:
            add(f"{name}.{child}", child_cls)

    for name, cls in nested:
        add(name, cls)
    for section in sections:
        for field in section["fields"]:
            if profiles is not None and field["path"] == "assistant.profile":
                field["choices"] = [""] + sorted(profiles)
                field["open"] = False
            more = (extra_choices or {}).get(field["path"])
            if more:
                base = field.get("choices", [])
                known = {c.lower() for c in base}
                field["choices"] = base + [c for c in more if c.lower() not in known]
    rank = {key: i for i, key in enumerate(ORDER)}
    sections.sort(key=lambda s: rank.get(s["key"], len(ORDER)))
    return sections


def values(raw: Any, sections: list[dict[str, Any]]) -> dict[str, Any]:
    """The value of every field as the file has it; absent = not in the file."""
    out: dict[str, Any] = {}
    root = raw if isinstance(raw, dict) else {}
    for section in sections:
        for field in section["fields"]:
            node: Any = root
            for part in field["path"].split("."):
                if not isinstance(node, dict) or part not in node:
                    break
                node = node[part]
            else:
                out[field["path"]] = node
    return out
