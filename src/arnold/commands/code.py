"""Handing work to Claude Code.

The assistant can already answer questions about this machine and put things on
screen. This lets it change things: "have Claude add a dark mode to the
dashboard", "ask Claude why the tests are failing in the assistant project".

Three properties shape the design:

* **It takes minutes, not seconds.** A voice reply cannot wait for it, so a
  task is started in the background and the answer is spoken when it lands.
* **It must run in the long-lived agent.** A one-shot `exec` process invoked
  over SSH exits as soon as it prints, taking any background work with it, so
  these commands are marked `needs_agent` and forwarded to the resident agent.
* **It is the sharpest tool here.** Everything else reads state or opens a
  window; this edits source. So projects are an explicit allowlist mapping a
  spoken name to a directory, and the whole feature is off until switched on.
"""

from __future__ import annotations

import json
import logging
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .. import process
from ..humanize import join_speech
from .registry import (
    CommandContext,
    CommandError,
    CommandResult,
    Registry,
    arg_str,
)

log = logging.getLogger(__name__)

# Where the CLI usually lands on Windows when the config does not say.
_CLI_CANDIDATES = (
    r"%USERPROFILE%\.local\bin\claude.exe",
    r"%LOCALAPPDATA%\Programs\claude\claude.exe",
    r"%APPDATA%\npm\claude.cmd",
)


@dataclass
class Task:
    id: str
    project: str
    prompt: str
    started: float = field(default_factory=time.time)
    finished: float = 0.0
    state: str = "running"  # running | done | failed
    summary: str = ""
    session_id: str = ""
    cost_usd: float = 0.0

    @property
    def seconds(self) -> float:
        return (self.finished or time.time()) - self.started

    def describe(self) -> dict:
        return {
            "id": self.id,
            "project": self.project,
            "prompt": self.prompt,
            "state": self.state,
            "seconds": round(self.seconds, 1),
            "summary": self.summary,
            # What the tokens would have cost at API rates. On a Max
            # subscription nothing is charged - this is a usage gauge, not
            # a bill, and reading it as money is a mistake worth naming.
            "equivalent_cost_usd": round(self.cost_usd, 4),
            "session_id": self.session_id,
        }


# Tasks live in the agent process for as long as it runs. Bounded so a long
# uptime cannot accumulate them without limit.
_tasks: dict[str, Task] = {}
_order: list[str] = []
_lock = threading.Lock()
MAX_REMEMBERED = 40


def register_all(registry: Registry) -> None:
    registry.register(
        "code.task",
        _task,
        "Give Claude Code a job in one of the allowlisted projects.",
        {
            "project": "allowlisted project name",
            "prompt": "what to do, in plain English",
            "continue": "true to carry on the project's last session",
        },
        needs_agent=True,
    )
    registry.register(
        "code.status",
        _status,
        "How a Claude Code task is getting on, or its result.",
        {"id": "task id, or omit for the most recent"},
        needs_agent=True,
    )
    registry.register(
        "code.projects", _projects, "List the projects Claude Code may work in."
    )


# -- setup -------------------------------------------------------------------


def _cli(ctx: CommandContext) -> str:
    configured = ctx.config.code.cli_path.strip()
    if configured:
        if not Path(configured).exists():
            raise CommandError(f"Claude Code isn't at {configured}.")
        return configured

    found = shutil.which("claude")
    if found:
        return found
    import os

    for candidate in _CLI_CANDIDATES:
        expanded = Path(os.path.expandvars(candidate))
        if expanded.exists():
            return str(expanded)
    raise CommandError(
        "I can't find the Claude Code CLI. Set code.cli_path in the config."
    )


def _project_dir(ctx: CommandContext, name: str) -> tuple[str, Path]:
    projects = {k.lower(): v for k, v in ctx.config.code.projects.items()}
    key = (name or "").strip().lower()

    if not projects:
        raise CommandError(
            "No projects are allowlisted for Claude Code. "
            "Add them under code.projects in the config."
        )
    if not key:
        if len(projects) == 1:
            key = next(iter(projects))
        else:
            raise CommandError(
                f"Which project? I can work in {join_speech(sorted(projects), 'or')}."
            )
    if key not in projects:
        raise CommandError(
            f"{name} isn't an allowlisted project. I can work in "
            f"{join_speech(sorted(projects), 'or')}."
        )

    path = Path(projects[key])
    if not path.is_dir():
        raise CommandError(f"{key} is allowlisted but {path} isn't a directory.")
    return key, path


# -- running -----------------------------------------------------------------


# Claude Code prefers an API key over the signed-in account when one is
# present in the environment, which would quietly move this work off the
# subscription and onto metered billing.
_API_KEY_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


def _child_env(cfg) -> dict[str, str] | None:
    """Environment for the CLI, with API credentials removed when asked.

    The agent inherits whatever the logon session has. Nothing sets an API key
    today, but any other project might tomorrow, and the failure would be
    silent - jobs would keep working and quietly bill per token instead of
    drawing on the subscription.
    """
    import os

    if not cfg.use_subscription:
        return None  # inherit as-is
    if not any(var in os.environ for var in _API_KEY_VARS):
        return None  # nothing to strip; let the child inherit normally
    env = {k: v for k, v in os.environ.items() if k not in _API_KEY_VARS}
    log.info("hiding API credentials so Claude Code uses the signed-in account")
    return env


def _remember(task: Task) -> None:
    with _lock:
        _tasks[task.id] = task
        _order.append(task.id)
        while len(_order) > MAX_REMEMBERED:
            _tasks.pop(_order.pop(0), None)


def _run(ctx: CommandContext, task: Task, cli: str, directory: Path, resume: str) -> None:
    """Drive the CLI to completion, then speak the outcome."""
    cfg = ctx.config.code
    argv = [
        cli,
        "-p", task.prompt,
        "--output-format", "json",
        "--permission-mode", cfg.permission_mode,
    ]
    if cfg.model:
        argv += ["--model", cfg.model]
    if resume:
        argv += ["--resume", resume]

    try:
        completed = process.run(
            argv,
            cwd=str(directory),
            timeout=cfg.timeout_seconds,
            env=_child_env(cfg),
        )
    except Exception as exc:
        task.state, task.summary = "failed", f"Claude Code didn't finish: {exc}"
        task.finished = time.time()
        log.warning("code task %s failed: %s", task.id, exc)
        _announce(ctx, task)
        return

    task.finished = time.time()
    payload = _parse(completed.stdout or "")
    if payload is None:
        detail = (completed.stderr or completed.stdout or "").strip()[:200]
        task.state = "failed"
        task.summary = detail or "Claude Code returned nothing I could read."
    else:
        task.session_id = str(payload.get("session_id") or "")
        task.cost_usd = float(payload.get("total_cost_usd") or 0.0)
        result = str(payload.get("result") or "").strip()
        if payload.get("is_error"):
            task.state, task.summary = "failed", result or "the task failed"
        else:
            task.state, task.summary = "done", result or "finished with nothing to report"

    log.info(
        "code task %s %s in %.0fs (%s)",
        task.id, task.state, task.seconds, task.project,
    )
    _announce(ctx, task)


def _parse(stdout: str) -> dict | None:
    """The JSON result, tolerating anything the CLI printed around it."""
    text = stdout.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Fall back to the last complete JSON object in the stream.
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return None


def _announce(ctx: CommandContext, task: Task) -> None:
    mouth = ctx.speech or ctx.jarvis
    if not ctx.config.code.speak_when_done or mouth is None:
        return
    verb = "finished" if task.state == "done" else "had trouble with"
    # Spoken aloud, so keep it to a sentence rather than reading a diff out.
    summary = task.summary.split("\n")[0][:240]
    try:
        mouth.say(f"Claude {verb} the {task.project} job. {summary}")
    except Exception as exc:
        log.debug("could not announce the code task: %s", exc)


# -- handlers ----------------------------------------------------------------


def _task(ctx: CommandContext, args: dict) -> CommandResult:
    cfg = ctx.config.code
    if not cfg.enabled:
        raise CommandError(
            "Claude Code isn't switched on for me. Set code.enabled in the config."
        )

    prompt = arg_str(args, "prompt").strip()
    if len(prompt) < 4:
        raise CommandError("Tell me what you'd like Claude to do.")

    project, directory = _project_dir(ctx, str(args.get("project") or ""))
    cli = _cli(ctx)

    with _lock:
        running = [t for t in _tasks.values() if t.state == "running"]
    if len(running) >= cfg.max_concurrent:
        raise CommandError(
            f"Claude is already working on the {running[0].project} job. "
            "One at a time."
        )

    resume = ""
    if str(args.get("continue") or "").lower() in ("1", "true", "yes"):
        with _lock:
            previous = [
                t for t in _tasks.values() if t.project == project and t.session_id
            ]
        if previous:
            resume = max(previous, key=lambda t: t.started).session_id

    task = Task(id=uuid.uuid4().hex[:8], project=project, prompt=prompt)
    _remember(task)
    threading.Thread(
        target=_run,
        args=(ctx, task, cli, directory, resume),
        daemon=True,
        name=f"code-{task.id}",
    ).start()

    log.info("code task %s started in %s: %s", task.id, project, prompt[:80])
    return CommandResult(
        speech=(
            f"I've set Claude to work on {project}. "
            "I'll tell you when it's done."
        ),
        result={"id": task.id, "project": project, "state": "running"},
    )


def _status(ctx: CommandContext, args: dict) -> CommandResult:
    wanted = str(args.get("id") or "").strip()
    with _lock:
        if wanted:
            task = _tasks.get(wanted)
        else:
            task = _tasks[_order[-1]] if _order else None

    if task is None:
        return CommandResult(
            speech="Claude hasn't been given anything to do yet.",
            result={"tasks": []},
        )

    if task.state == "running":
        speech = (
            f"Claude is still working on the {task.project} job, "
            f"{int(task.seconds)} seconds in."
        )
    else:
        verb = "finished" if task.state == "done" else "had trouble with"
        speech = f"Claude {verb} the {task.project} job. {task.summary.splitlines()[0][:240]}"

    return CommandResult(speech=speech, result=task.describe())


def _projects(ctx: CommandContext, args: dict) -> CommandResult:
    names = sorted(ctx.config.code.projects)
    if not names:
        return CommandResult(
            speech="No projects are allowlisted for Claude Code.",
            result={"projects": [], "enabled": ctx.config.code.enabled},
        )
    return CommandResult(
        speech=f"Claude can work in {join_speech(names, 'and')}.",
        result={"projects": names, "enabled": ctx.config.code.enabled},
    )
