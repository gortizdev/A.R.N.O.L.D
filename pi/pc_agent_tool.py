"""A richer PC tool for Jarvis.

`computer_command` drives a closed set of PowerShell templates - lock, sleep,
volume, launch. Useful, but it cannot answer a question: there is no way for
Jarvis to find out how much disk is left or what is on screen.

The PC runs `arnold`, which exposes ~35 commands that return
structured JSON *and* a ready-to-speak sentence. This forwards to it over the
same SSH connection and key `_pc_ssh` already uses, so nothing new needs
provisioning.

Wiring into assistant.py takes three lines - see pi/README.md.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import time
from typing import Any, Dict

# Written by the PC while its own voice assistant is listening, so both
# machines do not answer the same wake word. On tmpfs, so a reboot clears it.
CLAIM_FILE = "/run/user/1000/pc_wake_claim"
# The PC refreshes the claim periodically. Treating an old one as expired is
# what makes a sleeping or crashed PC hand control back on its own, rather
# than muting Jarvis forever.
CLAIM_MAX_AGE_SECONDS = 90.0


def wake_claimed() -> str:
    """Device id currently handling voice, or '' if Jarvis should answer.

    Deliberately just a stat and a small read: this runs on every wake-word
    hit, inside the audio loop, so it must never block.
    """
    try:
        stat = os.stat(CLAIM_FILE)
    except OSError:
        return ""
    if time.time() - stat.st_mtime > CLAIM_MAX_AGE_SECONDS:
        return ""
    try:
        with open(CLAIM_FILE, "r", encoding="utf-8") as handle:
            return handle.read().strip()[:64]
    except OSError:
        return ""

# Full path: the PC's SSH sessions run PowerShell and do not have the
# project's virtualenv on PATH.
AGENT_EXE = (
    "C:/Users/geogo/Downloads/Projects/ARNOLD/.venv/Scripts/arnold.exe"
)

# Commands Jarvis may invoke. Deliberately a closed list: the model picks a
# name from it rather than composing a command line, so this stays a typed tool
# surface rather than remote shell.
READ_COMMANDS = [
    "query.system", "query.cpu", "query.memory", "query.disk", "query.gpu",
    "query.network", "query.battery", "query.uptime", "query.processes",
    "query.process", "query.active_window", "query.alerts", "query.volume",
    "query.metric", "system.info", "system.ping",
]
WRITE_COMMANDS = [
    "desktop.notify", "desktop.clipboard_get", "desktop.clipboard_set",
    "control.volume_set", "control.volume_adjust", "control.mute",
    "control.media", "control.launch", "control.lock",
    "control.cancel_shutdown",
    "web.open", "web.search", "web.sites",
    "artifact.create", "artifact.open", "artifact.list",
    "code.task", "code.status", "code.projects",
]
DESTRUCTIVE_COMMANDS = [
    "control.shutdown", "control.restart", "control.sleep", "control.kill_process",
]
ALL_COMMANDS = READ_COMMANDS + WRITE_COMMANDS + DESTRUCTIVE_COMMANDS

SCHEMA = {
    "type": "function",
    "function": {
        "name": "pc_agent",
        "description": (
            "Query or control Geo's Windows desktop in detail. The desktop also "
            "runs its own assistant, Mycroft; this tool is how you reach that "
            "machine, and desktop.notify is how you leave Mycroft or Geo a note "
            "there. Use this for any "
            "QUESTION about the PC - disk space, CPU, memory, GPU temperature, "
            "network speed, uptime, what is running, what window is open, active "
            "alerts - and for notifications, clipboard and volume. Returns a "
            "'speech' field already phrased for reading aloud; prefer it verbatim.\n"
            "For simple lock/sleep/launch, computer_command is also fine.\n"
            "command must be one of: " + ", ".join(ALL_COMMANDS) + ".\n"
            "code.task gives Claude Code a real job in one of Geo's "
            "projects: {\"project\":\"assistant\",\"prompt\":\"...\"}. It "
            "runs for minutes and Geo is told aloud when it finishes, so "
            "confirm you have set it going rather than waiting for it. "
            "code.projects lists what it may touch.\n"
            "To SHOW Geo something you have made - a chart, a table, a "
            "checklist, a summary - use artifact.create {\"title\":\"...\","
            "\"html\":\"<h2>...\"} and it appears on his screen. Write real, "
            "self-contained HTML (tables, inline SVG, inline style/script; no "
            "external libraries). Use {\"markdown\":\"...\"} for plain prose. "
            "Prefer this over reading a long list aloud.\n"
            "Also opens web pages on the PC's screen: web.open "
            "{\"site\":\"youtube\"} or {\"url\":\"https://...\"}, and web.search "
            "{\"query\":\"lofi beats\",\"site\":\"youtube\"} (site defaults to "
            "google). Both accept {\"browser\":\"firefox\"}. Use these whenever "
            "Geo asks to look something up or pull something up on the PC.\n"
            "args examples: query.disk {\"drive\":\"C\"}; query.process "
            "{\"name\":\"steam\"}; query.processes {\"by\":\"memory\",\"limit\":3}; "
            "desktop.notify {\"message\":\"...\"}; control.volume_set {\"level\":40}."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "one of the listed commands"},
                "args": {"type": "object", "additionalProperties": True},
            },
            "required": ["command"],
        },
    },
}


def run_pc_agent(config: Any, command: str, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """Execute one agent command on the PC. Never raises; returns {"error": ...}."""
    command = (command or "").strip()
    if command not in ALL_COMMANDS:
        return {
            "error": f"unknown command '{command}'",
            "valid": ALL_COMMANDS,
        }

    pc = config.pc_control
    if not getattr(pc, "enabled", False) or not getattr(pc, "host", ""):
        return {"error": "PC control is not configured"}

    argv = [
        "ssh", "-i", pc.key_path,
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", f"ConnectTimeout={int(pc.ssh_timeout)}",
        f"{pc.user}@{pc.host}",
        AGENT_EXE, "exec", command,
    ]
    if args:
        # base64, not raw JSON: ssh joins argv into a single string that the
        # PC's PowerShell then re-parses, and JSON's quotes and spaces do not
        # survive that. Base64 is bare alphanumerics, so nothing can be mangled
        # or injected on the way.
        blob = base64.b64encode(json.dumps(args).encode("utf-8")).decode("ascii")
        argv += ["--json-b64", blob]

    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=pc.ssh_timeout + 20
        )
    except subprocess.TimeoutExpired:
        return {"error": "the PC did not respond (asleep or offline?)"}
    except Exception as exc:
        return {"error": f"could not reach the PC: {exc}"}

    stdout = (proc.stdout or "").strip()
    if not stdout:
        stderr = (proc.stderr or "").strip()[:200]
        if "not recognized" in stderr:
            return {"error": "the assistant agent is not installed on the PC"}
        return {"error": stderr or "no reply from the PC"}

    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return {"error": f"unexpected reply from the PC: {stdout[:160]}"}

    if not payload.get("ok", True):
        return {"error": payload.get("error") or "the command failed"}

    # `speech` first: it is what the model should read out.
    return {
        "speech": payload.get("speech", ""),
        "result": payload.get("result", {}),
        "command": command,
    }
