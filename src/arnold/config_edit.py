"""Editing config.yaml from the console.

The text is checked the way the agent itself would load it - YAML, every
section's keys, the alert rules - before anything is written, so a typo
cannot leave a config the agent refuses to start with. The old file is kept
as config.yaml.bak, and a save is refused if the file changed on disk since
the page read it, so an edit made elsewhere is not silently overwritten.

Most settings are read once at startup; restart_later() restarts whichever
tasks are running. It is handed to a process of its own, created through
WMI: the dashboard usually lives inside the Arnold task, and stopping that
task ends every process in it - including one we merely started detached.
"""

from __future__ import annotations

import base64
import os
import re
from pathlib import Path
from typing import Any

from . import process

# Restarted by restart_later(), if running. The game watcher reads the
# config at startup too.
RESTART_TASKS = ("ArnoldVoice", "ArnoldFace", "Arnold", "ArnoldGameWatch")


class ConfigEditError(RuntimeError):
    pass


def read(path: Path) -> dict[str, Any]:
    raw = path.read_bytes().decode("utf-8-sig")
    return {
        "path": str(path),
        "text": raw.replace("\r\n", "\n"),
        # A string: nanoseconds since 1970 are past what a JavaScript number
        # holds exactly, and a rounded copy would never match on the way back.
        "mtime": str(path.stat().st_mtime_ns),
    }


def check(text: str, path: Path) -> None:
    """Raise ConfigEditError if `text` is not a config the agent would load.

    Checked from a file beside the real one, so relative paths resolve as
    they will for real.
    """
    from .alerts import AlertEngine, RuleError
    from .config import ConfigError, load_config

    probe = path.with_name("." + path.name + ".check")
    try:
        probe.write_text(text, encoding="utf-8")
        try:
            config = load_config(probe)
            AlertEngine(config.alerts, device_name=config.device.friendly_name)
        except (ConfigError, RuleError) as exc:
            raise ConfigEditError(str(exc).replace(str(probe), path.name)) from None
        except Exception as exc:  # YAML errors and the like
            raise ConfigEditError(f"{type(exc).__name__}: {exc}".replace(str(probe), path.name)) from None
    finally:
        probe.unlink(missing_ok=True)


def save(path: Path, text: str, expected_mtime: int | str | None) -> dict[str, Any]:
    """Check, back up, and write. Returns read() of the new file."""
    if expected_mtime is not None and str(path.stat().st_mtime_ns) != str(expected_mtime):
        raise ConfigEditError(
            f"{path.name} changed on disk since it was loaded; reload it and make the edit again"
        )
    text = text.replace("\r\n", "\n")
    if not text.endswith("\n"):
        text += "\n"
    check(text, path)
    original = path.read_bytes()
    if b"\r\n" in original:
        text = text.replace("\n", "\r\n")
    path.with_name(path.name + ".bak").write_bytes(original)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(text.encode("utf-8"))
    os.replace(tmp, path)
    return read(path)


def _yaml():
    from ruamel.yaml import YAML

    yaml = YAML()  # round-trip: comments, order and quoting survive
    yaml.preserve_quotes = True
    yaml.width = 4096
    yaml.indent(mapping=2, sequence=4, offset=2)
    return yaml


def raw_values(text: str) -> Any:
    import yaml

    return yaml.safe_load(text) or {}


def apply_changes(text: str, changes: dict[str, Any]) -> str:
    """Set dotted paths in the YAML text, leaving everything else - comments
    included - exactly as it was. None removes the key, so the default
    applies again."""
    import io

    from ruamel.yaml.comments import CommentedMap, CommentedSeq

    yaml = _yaml()
    data = yaml.load(text) if text.strip() else None
    if data is None:
        data = CommentedMap()
    if not isinstance(data, dict):
        raise ConfigEditError("the top level of the config is not a mapping")
    existing = set(data)
    for path, value in changes.items():
        parts = [p for p in str(path).split(".") if p]
        if not parts:
            continue
        node = data
        for part in parts[:-1]:
            child = node.get(part)
            if child is None:
                if value is None:
                    break  # removing from a section that is not there
                child = CommentedMap()
                node[part] = child
            if not isinstance(child, dict):
                raise ConfigEditError(f"{'.'.join(parts[:-1])} is not a section in the file")
            node = child
        else:
            key = parts[-1]
            if value is None:
                node.pop(key, None)
            elif isinstance(value, list):
                current = node.get(key)
                if isinstance(current, CommentedSeq):
                    # Whatever follows the list - a blank line, the next
                    # section's comment - hangs off its last item.
                    trailing = current.ca.items.pop(len(current) - 1, None) if len(current) else None
                    current.ca.items.clear()
                    current[:] = value  # keeps its flow or block style
                    if trailing is not None and len(current):
                        current.ca.items[len(current) - 1] = trailing
                else:
                    seq = CommentedSeq(value)
                    seq.fa.set_flow_style()
                    node[key] = seq
            else:
                node[key] = value
    out = io.StringIO()
    yaml.dump(data, out)
    text = out.getvalue()
    # A section new to the file lands at the end, hard against whatever came
    # before it; give it the blank line the others have.
    for key in [k for k in data if k not in existing]:
        text = re.sub(rf"(?m)(?<=\S\n)^{re.escape(str(key))}:", "\n" + str(key) + ":", text, count=1)
    return text


def coerce(field: dict[str, Any], value: Any) -> Any:
    """A value from the form, as the field's type; ValueError if it is not one."""
    kind = field["type"]
    if value is None:
        return None
    if kind == "bool":
        if isinstance(value, bool):
            return value
        raise ValueError("expected on or off")
    if kind in ("int", "float"):
        if isinstance(value, str):
            value = value.strip()
            if value == "":
                return None
        if isinstance(value, bool):
            raise ValueError("expected a number")
        number = float(value)
        if kind == "int":
            if number != int(number):
                raise ValueError("expected a whole number")
            return int(number)
        return number
    if kind == "str":
        if not isinstance(value, str):
            raise ValueError("expected text")
        choices = field.get("choices")
        if choices and not field.get("open") and value not in choices:
            raise ValueError("expected one of " + ", ".join(repr(c) for c in choices))
        return value
    if kind == "list":
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ValueError("expected a list of text")
        return [v.strip() for v in value if v.strip()]
    raise ValueError("this setting is edited in the YAML view")


def restart_later(delay: float = 2.0) -> str | None:
    """Restart every running task a moment from now, from outside them all.
    Returns an error message, or None."""
    names = ",".join(f"'{t}'" for t in RESTART_TASKS)
    restart = (
        f"Start-Sleep -Seconds {delay}; "
        f"$running = Get-ScheduledTask -TaskName {names} -ErrorAction SilentlyContinue"
        " | Where-Object State -eq 'Running'; "
        "$running | ForEach-Object { Stop-ScheduledTask -TaskName $_.TaskName }; "
        "Start-Sleep -Seconds 2; "
        "$running | ForEach-Object { Start-ScheduledTask -TaskName $_.TaskName }"
    )
    encoded = base64.b64encode(restart.encode("utf-16-le")).decode("ascii")
    launcher = (
        "$si = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly"
        " -Property @{ShowWindow=[uint16]0}; "
        "$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{"
        f"CommandLine='powershell -NoProfile -NonInteractive -EncodedCommand {encoded}';"
        " ProcessStartupInformation=$si}; exit $r.ReturnValue"
    )
    proc = process.run(["powershell", "-NoProfile", "-Command", launcher], timeout=30)
    if proc.returncode != 0:
        return (proc.stderr or "").strip() or f"exit {proc.returncode}"
    return None
