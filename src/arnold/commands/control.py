"""Commands that change the machine's state.

Launching and script execution are allowlist-only: `security.launch_allowlist`
maps a spoken name to an absolute path, and anything not in the map is refused.
That keeps a mis-transcribed wake-word phrase from turning into arbitrary
process execution. Power commands additionally require
`security.allow_destructive`, enforced in `security.CommandVerifier`.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import psutil

from .. import process
from ..humanize import join_speech
from ..platform_win import audio, power
from .registry import (
    CommandContext,
    CommandError,
    CommandResult,
    Registry,
    arg_bool,
    arg_int,
    arg_str,
)

log = logging.getLogger(__name__)


def register_all(registry: Registry) -> None:
    registry.register(
        "control.launch", _launch, "Start an allowlisted application.", {"name": "e.g. 'steam'"},
        needs_desktop=True,  # an app started from session 0 opens where nobody can see it
    )
    registry.register(
        "control.kill_process",
        _kill,
        "Terminate a process by name or PID.",
        {"name": "process name", "pid": "or a numeric PID", "force": "true to kill immediately"},
    )
    registry.register("control.lock", _lock, "Lock the workstation.")
    registry.register("control.sleep", _sleep, "Put the machine to sleep.")
    registry.register("control.hibernate", _hibernate, "Hibernate the machine.")
    registry.register(
        "control.shutdown", _shutdown, "Shut down, after a cancellable delay.",
        {"delay_seconds": "default 30"},
    )
    registry.register(
        "control.restart", _restart, "Restart, after a cancellable delay.",
        {"delay_seconds": "default 30"},
    )
    registry.register("control.logoff", _logoff, "Log the current user off.")
    registry.register(
        "control.cancel_shutdown", _cancel, "Call off a pending shutdown or restart."
    )
    registry.register(
        "control.volume_set", _volume_set, "Set output volume.", {"level": "0 to 100"}
    )
    registry.register(
        "control.volume_adjust", _volume_adjust, "Nudge volume up or down.",
        {"delta": "percentage points, negative to lower"},
    )
    registry.register(
        "control.mute", _mute, "Mute, unmute, or toggle.", {"muted": "true, false, or omit to toggle"}
    )
    registry.register(
        "control.media", _media, "Media transport control.",
        {"action": "play, pause, next, previous, or stop"},
    )
    registry.register(
        "control.run_script", _run_script, "Run an allowlisted script.", {"name": "allowlist key"}
    )


def _launch(ctx: CommandContext, args: dict) -> CommandResult:
    name = arg_str(args, "name").lower()
    allowlist = {k.lower(): v for k, v in ctx.config.security.launch_allowlist.items()}

    target = allowlist.get(name)
    if target is None:
        if not allowlist:
            raise CommandError(
                "No applications are allowlisted for launching. "
                "Add them under security.launch_allowlist."
            )
        raise CommandError(
            f"{name} isn't allowlisted. I can launch "
            f"{join_speech(sorted(allowlist), 'or')}."
        )

    path = Path(target)
    if not path.exists():
        raise CommandError(f"{name} is allowlisted but {path} doesn't exist on disk.")

    try:
        proc = process.launch([str(path)], cwd=str(path.parent))
    except OSError as exc:
        raise CommandError(f"I couldn't start {name}: {exc}") from exc

    return CommandResult(
        speech=f"Starting {name} on {ctx.device_name}.",
        result={"name": name, "path": str(path), "pid": proc.pid},
    )


def _kill(ctx: CommandContext, args: dict) -> CommandResult:
    force = arg_bool(args, "force", False)
    targets: list[psutil.Process] = []

    if args.get("pid") is not None:
        pid = arg_int(args, "pid", minimum=1)
        try:
            targets = [psutil.Process(pid)]
        except psutil.NoSuchProcess:
            raise CommandError(f"There's no process with PID {pid}.") from None
    else:
        name = arg_str(args, "name").lower()
        needle = name.removesuffix(".exe")
        own_pid = psutil.Process().pid
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                candidate = (proc.info["name"] or "").lower().removesuffix(".exe")
                if needle == candidate and proc.info["pid"] != own_pid:
                    targets.append(proc)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        if not targets:
            raise CommandError(f"{args['name']} isn't running, so there's nothing to close.")

    killed, failed = [], []
    for proc in targets:
        try:
            label = {"pid": proc.pid, "name": proc.name()}
            proc.kill() if force else proc.terminate()
            killed.append(label)
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied:
            failed.append(proc.pid)

    psutil.wait_procs(targets, timeout=3)

    if not killed and failed:
        raise CommandError(
            "I don't have permission to close that. "
            "The agent would need to run elevated."
        )

    label = killed[0]["name"] if killed else "the process"
    speech = f"Closed {label} on {ctx.device_name}."
    if len(killed) > 1:
        speech = f"Closed {len(killed)} {label} processes on {ctx.device_name}."
    if failed:
        speech += f" {len(failed)} couldn't be closed without elevation."
    return CommandResult(speech=speech, result={"killed": killed, "access_denied": failed})


def _lock(ctx: CommandContext, args: dict) -> CommandResult:
    power.lock()
    return CommandResult(speech=f"Locking {ctx.device_name}.", result={"locked": True})


def _sleep(ctx: CommandContext, args: dict) -> CommandResult:
    power.sleep()
    return CommandResult(speech=f"Putting {ctx.device_name} to sleep.", result={"sleeping": True})


def _hibernate(ctx: CommandContext, args: dict) -> CommandResult:
    power.hibernate()
    return CommandResult(speech=f"Hibernating {ctx.device_name}.", result={"hibernating": True})


def _shutdown(ctx: CommandContext, args: dict) -> CommandResult:
    delay = arg_int(args, "delay_seconds", power.DEFAULT_DELAY_SECONDS, minimum=0, maximum=86400)
    power.shutdown(delay, message="Shutdown requested by Jarvis.")
    return CommandResult(
        speech=f"Shutting {ctx.device_name} down in {delay} seconds. "
        f"Tell me to cancel the shutdown if that's wrong.",
        result={"delay_seconds": delay},
    )


def _restart(ctx: CommandContext, args: dict) -> CommandResult:
    delay = arg_int(args, "delay_seconds", power.DEFAULT_DELAY_SECONDS, minimum=0, maximum=86400)
    power.restart(delay, message="Restart requested by Jarvis.")
    return CommandResult(
        speech=f"Restarting {ctx.device_name} in {delay} seconds. "
        f"Tell me to cancel the shutdown if that's wrong.",
        result={"delay_seconds": delay},
    )


def _logoff(ctx: CommandContext, args: dict) -> CommandResult:
    power.logoff()
    return CommandResult(speech=f"Logging off {ctx.device_name}.", result={"logoff": True})


def _cancel(ctx: CommandContext, args: dict) -> CommandResult:
    power.cancel_shutdown()
    return CommandResult(
        speech=f"Cancelled the pending shutdown on {ctx.device_name}.", result={"cancelled": True}
    )


def _volume_set(ctx: CommandContext, args: dict) -> CommandResult:
    level = arg_int(args, "level", minimum=0, maximum=100)
    state = audio.set_volume(level)
    speech = f"Volume set to {level} percent on {ctx.device_name}."
    if not state["precise"]:
        speech += " Roughly, anyway."
    return CommandResult(speech=speech, result=state)


def _volume_adjust(ctx: CommandContext, args: dict) -> CommandResult:
    delta = arg_int(args, "delta", minimum=-100, maximum=100)
    if delta == 0:
        raise CommandError("Tell me how much to change the volume by.")
    state = audio.adjust_volume(delta)
    direction = "up" if delta > 0 else "down"
    speech = f"Volume {direction} {abs(delta)} on {ctx.device_name}"
    speech += f", now at {state['level']} percent." if state.get("level") is not None else "."
    return CommandResult(speech=speech, result=state)


def _mute(ctx: CommandContext, args: dict) -> CommandResult:
    muted = arg_bool(args, "muted", None)
    state = audio.set_mute(muted)
    if state["muted"] is None:
        speech = f"Toggled mute on {ctx.device_name}."
    else:
        speech = f"{'Muted' if state['muted'] else 'Unmuted'} {ctx.device_name}."
    return CommandResult(speech=speech, result=state)


def _media(ctx: CommandContext, args: dict) -> CommandResult:
    action = arg_str(args, "action")
    try:
        audio.press_media_key(action)
    except ValueError as exc:
        raise CommandError(str(exc)) from exc
    return CommandResult(speech=f"{action.capitalize()}.", result={"action": action})


def _run_script(ctx: CommandContext, args: dict) -> CommandResult:
    name = arg_str(args, "name").lower()
    allowlist = {k.lower(): v for k, v in ctx.config.security.script_allowlist.items()}

    command = allowlist.get(name)
    if command is None:
        if not allowlist:
            raise CommandError(
                "No scripts are allowlisted. Add them under security.script_allowlist."
            )
        raise CommandError(
            f"{name} isn't an allowlisted script. I know "
            f"{join_speech(sorted(allowlist), 'or')}."
        )

    try:
        proc = process.run(
            command,
            shell=True,  # the allowlist is the trust boundary, not the shell
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        raise CommandError(f"The script {name} took longer than two minutes, so I stopped it.") from None

    output = (proc.stdout or "").strip()
    ok = proc.returncode == 0
    speech = (
        f"Script {name} finished on {ctx.device_name}."
        if ok
        else f"Script {name} failed with exit code {proc.returncode}."
    )
    return CommandResult(
        ok=ok,
        speech=speech,
        result={
            "name": name,
            "exit_code": proc.returncode,
            "stdout": output[:4000],
            "stderr": (proc.stderr or "").strip()[:2000],
        },
        error="" if ok else f"exit code {proc.returncode}",
    )
