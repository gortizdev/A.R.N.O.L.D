"""Claude Code sessions, watched from the outside.

Claude Code writes every session to a transcript under
``~/.claude/projects/<slug>/<session-id>.jsonl`` - the desktop app, the VS Code
extension and the terminal all use the same files. Since 2.1.27x a running
session also registers itself in ``~/.claude/sessions/<pid>.json`` with its
status and, when it accepts messages from other sessions, the named pipe it
listens on. Neither is an API, so this reads them the way a colleague would
read over a shoulder: what was asked, what it is doing now, what it said last.

Three sources feed one picture:

* **Transcripts** - always there. Tailed rather than re-read, so a session in
  the middle of a long job costs a few kilobytes a poll, not seven megabytes.
* **The session registry** - only for a session whose process is alive. It is
  what makes "busy" and "idle" certain rather than inferred, and what gives us
  the pipe to send a prompt down (see ``claude_link.py``).
* **Hook events** - optional, installed with ``arnold claude hooks
  install``. Claude Code runs them inside the session, so they know things the
  transcript does not record: that a permission prompt is on screen, that a
  turn just ended. Written to a file this watch tails.

The dev servers a session started are matched to it by working directory: a
``vite`` listening on 5173 whose cwd is inside the Ellipse-Hub folder belongs
to the Ellipse-Hub session. Ports the transcript mentions (``localhost:5173``)
count as a second vote.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

log = logging.getLogger(__name__)

# The transcript entries that mark a turn's edges and its content.
_PROMPT_KINDS = ("human",)
_REPLY_STOPS = ("end_turn", "stop_sequence", "max_tokens")
_INPUT_TOOLS = ("AskUserQuestion",)
_PORT_RE = re.compile(r"(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\]):(\d{2,5})\b")
_ENTRYPOINT_NAMES = {
    "claude-desktop": "Desktop",
    "claude-vscode": "VS Code",
    "cli": "terminal",
    "sdk-cli": "SDK",
}


def claude_home() -> Path:
    """Where Claude Code keeps its state; honours the same override it does."""
    configured = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    return Path(configured) if configured else Path.home() / ".claude"


def _now() -> float:
    return time.time()


def _ts(value: Any) -> float:
    """A transcript timestamp ('2026-09-22T15:42:34.123Z') as epoch seconds."""
    if isinstance(value, (int, float)):
        return float(value) / (1000.0 if value > 1e11 else 1.0)
    if not isinstance(value, str) or not value:
        return 0.0
    from datetime import datetime, timezone

    try:
        text = value.replace("Z", "+00:00")
        when = datetime.fromisoformat(text)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return when.timestamp()
    except ValueError:
        return 0.0


def _excerpt(text: str, limit: int = 220) -> str:
    """The first sentence or so, on one line, for a card or a spoken summary."""
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    for mark in (". ", "! ", "? ", "; "):
        at = cut.rfind(mark)
        if at > limit // 2:
            return cut[: at + 1]
    return cut.rstrip() + "\u2026"


def _strip_markup(text: str) -> str:
    """Markdown headings, bold, code fences and links, for speaking aloud."""
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.M)
    text = re.sub(r"[*_]{1,3}([^*_]+)[*_]{1,3}", r"\1", text)
    text = re.sub(r"^\s*[-*|]\s*", "", text, flags=re.M)
    return text


def project_name(cwd: str) -> str:
    """'C:/.../Ellipse-Hub' -> 'Ellipse Hub': the folder, made speakable."""
    base = Path(cwd).name if cwd else ""
    return re.sub(r"[-_]+", " ", base).strip() or "an unnamed project"


def tool_summary(name: str, inputs: dict[str, Any]) -> str:
    """One short line about a tool call, the way a status bar would put it."""
    inputs = inputs or {}
    if name == "Bash":
        return str(inputs.get("description") or inputs.get("command") or "a command")[:120]
    if name in ("Read", "Edit", "Write", "MultiEdit", "NotebookEdit"):
        path = str(inputs.get("file_path") or inputs.get("notebook_path") or "")
        return f"{name.lower()} {Path(path).name}" if path else name.lower()
    if name in ("Grep", "Glob"):
        return f"search {inputs.get('pattern', '')}"[:120]
    if name in ("Agent", "Task"):
        return f"agent: {inputs.get('description') or inputs.get('prompt', '')}"[:120]
    if name == "AskUserQuestion":
        questions = inputs.get("questions") or []
        first = questions[0].get("question") if questions and isinstance(questions[0], dict) else ""
        return f"asking: {first}"[:160] if first else "asking you a question"
    if name.startswith("mcp__"):
        return name.split("__")[-1].replace("_", " ")
    if name == "WebFetch":
        return f"fetch {inputs.get('url', '')}"[:120]
    if name == "WebSearch":
        return f"search the web for {inputs.get('query', '')}"[:120]
    return name


# -- one session ------------------------------------------------------------


@dataclass
class Turn:
    prompt: str = ""
    started: float = 0.0
    finished: float = 0.0
    reply: str = ""
    tool_calls: int = 0

    @property
    def seconds(self) -> float:
        return (self.finished or _now()) - self.started if self.started else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "prompt": _excerpt(self.prompt, 160),
            "started": self.started,
            "finished": self.finished,
            "seconds": round(self.seconds, 1),
            "reply": _excerpt(self.reply, 300),
            "tool_calls": self.tool_calls,
        }


@dataclass
class Session:
    """Everything known about one Claude Code session, from all three sources."""

    id: str
    path: Path
    cwd: str = ""
    entrypoint: str = ""
    version: str = ""
    git_branch: str = ""
    model: str = ""
    title: str = ""
    # bypassPermissions, acceptEdits, default, plan - as the transcript records
    # it on each prompt. A message into the session must attest the same class.
    permission_mode: str = ""
    # The desktop app's own id for the conversation this transcript belongs
    # to, and whether a newer transcript has taken over that conversation.
    desktop_id: str = ""
    superseded: bool = False
    # Registry facts, present only while the process is alive.
    pid: int = 0
    registry_name: str = ""
    registry_status: str = ""  # busy | idle | ""
    socket: str = ""
    # Turn state, from the transcript and the hooks.
    turn: str = "idle"  # busy | idle | waiting
    waiting_for: str = ""  # a question, a permission - what "waiting" is about
    activity: str = ""  # the tool call in flight, in words
    activity_tool: str = ""
    current: Turn = field(default_factory=Turn)
    turns: deque = field(default_factory=lambda: deque(maxlen=12))
    last_prompt: str = ""
    last_prompt_at: float = 0.0
    last_reply: str = ""
    last_reply_at: float = 0.0
    last_event_at: float = 0.0  # anything at all written to the transcript
    subagents: int = 0
    mentioned_ports: set = field(default_factory=set)
    # Hook-sourced, overriding what the transcript implies.
    hook_at: float = 0.0
    hook_state: str = ""
    apps: list = field(default_factory=list)
    first_seen: float = field(default_factory=_now)
    # Tailing state.
    _offset: int = 0
    _pending_tools: dict = field(default_factory=dict)
    _seeded: bool = False
    # A reply's blocks land as separate lines, each carrying the message's
    # stop reason, so the turn is not declared over on the first of them: it
    # closes when another message starts, or after a grace period.
    _closing_msg: str = ""
    _closing_wall: float = 0.0
    _closing_seeded: bool = False

    # -- reading ----------------------------------------------------------

    @property
    def live(self) -> bool:
        return self.pid > 0

    @property
    def name(self) -> str:
        return self.title or self.registry_name or project_name(self.cwd)

    @property
    def where(self) -> str:
        return _ENTRYPOINT_NAMES.get(self.entrypoint, self.entrypoint or "unknown")

    @property
    def mode_class(self) -> str:
        """'bypass' or 'prompting' - the two classes Claude Code's inbox tells
        apart - or '' when the transcript has not said."""
        if not self.permission_mode:
            return ""
        return "bypass" if self.permission_mode in ("bypassPermissions", "dontAsk") else "prompting"

    @property
    def quiet_seconds(self) -> float:
        return _now() - self.last_event_at if self.last_event_at else 0.0

    def labels(self) -> list[str]:
        """Every name a person might use for this session, lowercased."""
        out = {self.name.lower(), project_name(self.cwd).lower()}
        if self.title:
            out.add(self.title.lower())
        if self.registry_name:
            out.add(self.registry_name.lower())
        if self.cwd:
            out.add(Path(self.cwd).name.lower())
        return [label for label in out if label]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "title": self.title,
            "project": project_name(self.cwd),
            "cwd": self.cwd,
            "where": self.where,
            "entrypoint": self.entrypoint,
            "live": self.live,
            "pid": self.pid,
            "can_message": bool(self.socket),
            "turn": self.turn,
            "waiting_for": self.waiting_for,
            "activity": self.activity,
            "activity_tool": self.activity_tool,
            "last_prompt": _excerpt(self.last_prompt, 200),
            "last_prompt_at": self.last_prompt_at,
            "last_reply": _excerpt(self.last_reply, 400),
            "last_reply_at": self.last_reply_at,
            "last_event_at": self.last_event_at,
            "quiet_seconds": round(self.quiet_seconds),
            "turn_seconds": round(self.current.seconds) if self.turn != "idle" else 0,
            "tool_calls": self.current.tool_calls,
            "subagents": self.subagents,
            "model": self.model,
            "git_branch": self.git_branch,
            "permission_mode": self.permission_mode,
            "desktop_id": self.desktop_id,
            "turns": [t.to_dict() for t in list(self.turns)[-5:]],
            "apps": list(self.apps),
        }

    # -- the transcript ---------------------------------------------------

    def absorb(self, row: dict[str, Any]) -> list[dict[str, Any]]:
        """Fold one transcript line in. Returns the events it implies."""
        events: list[dict[str, Any]] = []
        kind = row.get("type")
        when = _ts(row.get("timestamp")) or (self.last_event_at if kind in ("custom-title", "ai-title", "last-prompt") else _now())
        if row.get("timestamp"):
            self.last_event_at = max(self.last_event_at, when)

        for key, attr in (
            ("cwd", "cwd"), ("entrypoint", "entrypoint"), ("version", "version"),
            ("gitBranch", "git_branch"), ("permissionMode", "permission_mode"),
        ):
            value = row.get(key)
            if isinstance(value, str) and value:
                setattr(self, attr, value)

        if kind == "custom-title" and row.get("customTitle"):
            self.title = str(row["customTitle"])
        elif kind == "ai-title" and row.get("aiTitle") and not self.title:
            self.title = str(row["aiTitle"])
        elif kind == "last-prompt" and row.get("lastPrompt") and not self.last_prompt:
            # A long session's opening prompt is far behind the part that
            # was read; Claude Code repeats the latest one in these rows.
            self.last_prompt = str(row["lastPrompt"])
        elif kind == "user":
            events += self._absorb_user(row, when)
        elif kind == "assistant":
            events += self._absorb_assistant(row, when)
        elif kind == "system" and row.get("subtype") == "api_error":
            self.activity = "retrying after an API error"
        return events

    def _absorb_user(self, row: dict[str, Any], when: float) -> list[dict[str, Any]]:
        message = row.get("message") or {}
        content = message.get("content")
        origin = row.get("origin") or {}
        kind = str(origin.get("kind") or "human") if isinstance(origin, dict) else "human"
        events: list[dict[str, Any]] = []

        texts: list[str] = []
        results = 0
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_result":
                    results += 1
                    self._pending_tools.pop(block.get("tool_use_id"), None)
                elif block.get("type") == "text":
                    texts.append(str(block.get("text") or ""))

        if results:
            # A result landing means the tool finished and the session moved
            # on - including past a question or a permission it was waiting
            # for. The next tool_use sets a new activity.
            if not self._pending_tools:
                self.activity, self.activity_tool = "thinking", ""
            self.turn, self.waiting_for = "busy", ""
            return events

        prompt = "\n".join(t for t in texts if t).strip()
        if not prompt or row.get("isMeta"):
            return events
        head = prompt.lstrip()[:40].lower()
        if head.startswith(("<system-reminder", "<command-", "<local-command", "caveat: the messages below")):
            return events

        events += self._finalize(when)
        # A new turn. Close the previous one if it never got a reply.
        if self.current.started and not self.current.finished:
            self.current.finished = when
            self.turns.append(self.current)
        if kind in _PROMPT_KINDS:
            self.current = Turn(prompt=prompt, started=when)
            self.last_prompt, self.last_prompt_at = prompt, when
            if self._seeded:
                events.append({"kind": "turn_started", "prompt": _excerpt(prompt, 160)})
        else:
            # A subagent finishing, a scheduled trigger: the session wakes
            # and works, but nobody typed anything.
            self.current = Turn(prompt=f"[{kind}]", started=when)
        self.turn, self.waiting_for = "busy", ""
        self.activity, self.activity_tool = "thinking", ""
        self._pending_tools.clear()
        return events

    def _finalize(self, when: float) -> list[dict[str, Any]]:
        """Close the turn a stop reason announced, now that its blocks are in."""
        if not self._closing_msg:
            return []
        self._closing_msg, self._closing_wall = "", 0.0
        if not self.current.started or self.current.finished:
            return []
        self.current.finished = max(self.current.finished, self.last_reply_at or when)
        self.turns.append(self.current)
        if not self._closing_seeded:
            return []
        return [{
            "kind": "turn_done",
            "seconds": self.current.seconds,
            "prompt": _excerpt(self.current.prompt, 160),
            "reply": _excerpt(_strip_markup(self.current.reply), 300),
            "tool_calls": self.current.tool_calls,
        }]

    def settle(self, grace: float = 6.0) -> list[dict[str, Any]]:
        """Close a turn whose last block arrived a while ago and nothing since."""
        if self._closing_msg and _now() - self._closing_wall >= grace:
            return self._finalize(_now())
        return []

    def _absorb_assistant(self, row: dict[str, Any], when: float) -> list[dict[str, Any]]:
        message = row.get("message") or {}
        if message.get("model"):
            self.model = str(message["model"])
        events: list[dict[str, Any]] = []
        msg_id = str(message.get("id") or row.get("uuid") or "")
        if self._closing_msg and msg_id != self._closing_msg:
            events += self._finalize(when)
        text_parts: list[str] = []
        for block in message.get("content") or []:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "tool_use":
                name = str(block.get("name") or "tool")
                summary = tool_summary(name, block.get("input") or {})
                self._pending_tools[block.get("id")] = name
                self.current.tool_calls += 1
                self.activity, self.activity_tool = summary, name
                if name in _INPUT_TOOLS:
                    self.turn, self.waiting_for = "waiting", "a question: " + summary.removeprefix("asking: ")
                    if self._seeded:
                        events.append({"kind": "needs_input", "what": "question", "detail": summary})
                else:
                    self.turn = "busy"
            elif btype == "text" and block.get("text"):
                text_parts.append(str(block["text"]))

        if text_parts:
            text = "\n".join(text_parts).strip()
            if text:
                self.last_reply, self.last_reply_at = text, when
                self.current.reply = text

        stop = message.get("stop_reason")
        if stop in _REPLY_STOPS:
            # The turn is over as far as the process goes - a model that ends
            # on a question asked in prose is idle, not waiting. The turn
            # itself closes once its remaining blocks have been written.
            if not self._closing_msg:
                self._closing_msg = msg_id or "?"
                self._closing_wall = _now()
                self._closing_seeded = self._seeded
            self.turn, self.waiting_for = "idle", ""
            self.activity, self.activity_tool = "", ""
            self._pending_tools.clear()
        return events

    def note_ports(self, text: str) -> None:
        for match in _PORT_RE.findall(text or ""):
            try:
                port = int(match)
            except ValueError:
                continue
            if 1024 <= port <= 65535:
                self.mentioned_ports.add(port)


# -- the registry -------------------------------------------------------------


def read_registry(home: Path | None = None, alive: Callable[[int], bool] | None = None) -> list[dict[str, Any]]:
    """Live sessions from ``~/.claude/sessions/<pid>.json``, dead pids skipped."""
    home = home or claude_home()
    folder = home / "sessions"
    if alive is None:
        try:
            import psutil

            alive = psutil.pid_exists
        except Exception:  # pragma: no cover - psutil is a hard dependency
            alive = lambda pid: True  # noqa: E731
    out: list[dict[str, Any]] = []
    try:
        names = os.listdir(folder)
    except OSError:
        return out
    for name in names:
        if not name.endswith(".json") or not name[:-5].isdigit():
            continue
        try:
            with open(folder / name, "r", encoding="utf-8") as fh:
                record = json.load(fh)
        except (OSError, ValueError):
            continue
        if not isinstance(record, dict) or not record.get("sessionId"):
            continue
        pid = int(record.get("pid") or name[:-5])
        if not alive(pid):
            continue
        record["pid"] = pid
        out.append(record)
    return out


def read_desktop_titles(root: Path | None = None) -> dict[str, str]:
    """Titles the desktop app gave its sessions, keyed by CLI session id."""
    return {cli_id: rec["title"] for cli_id, rec in read_desktop_records(root).items() if rec["title"]}


def read_desktop_records(root: Path | None = None) -> dict[str, dict[str, Any]]:
    """What the desktop app knows about each CLI session id it has used.

    The desktop keeps one record per conversation in its sidebar, with the
    title, the CLI session id it is running *now* and every id it used
    before: each time the person sends a message after the process has gone,
    it starts a fresh CLI session forked from the last, so one conversation
    is a chain of transcripts. Only the current one is where new turns land.
    The record is under the packaged app's redirected AppData on Windows, so
    the path is looked up rather than assumed; a miss simply means the
    transcript's own title is used and nothing is marked superseded.
    """
    candidates: list[Path] = []
    if root is not None:
        candidates.append(root)
    else:
        local = Path(os.environ.get("LOCALAPPDATA", "")) if os.environ.get("LOCALAPPDATA") else None
        roaming = Path(os.environ.get("APPDATA", "")) if os.environ.get("APPDATA") else None
        if local:
            packages = local / "Packages"
            try:
                for entry in os.scandir(packages):
                    if entry.is_dir() and entry.name.startswith("Claude_"):
                        candidates.append(Path(entry.path) / "LocalCache" / "Roaming" / "Claude" / "claude-code-sessions")
            except OSError:
                pass
        if roaming:
            candidates.append(roaming / "Claude" / "claude-code-sessions")

    out: dict[str, dict[str, Any]] = {}
    for base in candidates:
        if not base.is_dir():
            continue
        for path in base.rglob("local_*.json"):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    record = json.load(fh)
            except (OSError, ValueError):
                continue
            if not isinstance(record, dict):
                continue
            title = str(record.get("title") or "").strip()
            desktop_id = str(record.get("sessionId") or path.stem)
            current = record.get("cliSessionId") if isinstance(record.get("cliSessionId"), str) else ""
            for cli_id in record.get("priorCliSessionIds") or []:
                if isinstance(cli_id, str):
                    out[cli_id] = {"title": title, "desktop": desktop_id, "current": current, "superseded": bool(current and cli_id != current)}
            if current:
                out[current] = {"title": title, "desktop": desktop_id, "current": current, "superseded": False}
    return out


# -- listening ports ----------------------------------------------------------


@dataclass
class ListeningApp:
    port: int
    pid: int
    process: str
    cwd: str
    cmdline: str
    hosted: bool  # descends from a Claude Code host process
    host: str = "127.0.0.1"  # the loopback address it bound; vite picks ::1
    url: str = ""
    status: str = "unknown"  # up | down | unknown
    http_status: int = 0
    title: str = ""
    checked_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "port": self.port,
            "pid": self.pid,
            "process": self.process,
            "cwd": self.cwd,
            "url": self.url or f"http://localhost:{self.port}/",
            "status": self.status,
            "http_status": self.http_status,
            "title": self.title,
        }


_HOST_NAMES = frozenset({"claude.exe", "claude", "code.exe", "code", "cursor.exe", "windsurf.exe"})
_DEV_NAMES = frozenset({"node.exe", "node", "python.exe", "python", "pythonw.exe", "dotnet.exe", "dotnet", "deno.exe", "bun.exe", "ruby.exe", "php.exe", "java.exe", "go.exe", "cargo.exe", "flask", "uvicorn", "gunicorn"})
# This project's own processes: the dashboard listens, and it is not a dev server.
_OWN_MARKS = ("arnold",)


def listening_apps(skip_pids: Iterable[int] = ()) -> list[ListeningApp]:
    """Local TCP listeners that look like something a coding session started."""
    import psutil

    skip = set(skip_pids)
    out: list[ListeningApp] = []
    seen: set[tuple[int, int]] = set()
    try:
        connections = psutil.net_connections(kind="tcp")
    except (psutil.AccessDenied, OSError) as exc:
        log.debug("cannot list listeners: %s", exc)
        return out
    for conn in connections:
        if conn.status != psutil.CONN_LISTEN or not conn.pid or conn.pid in skip:
            continue
        port = conn.laddr.port if conn.laddr else 0
        if not port or (port, conn.pid) in seen:
            continue
        seen.add((port, conn.pid))
        try:
            proc = psutil.Process(conn.pid)
            with proc.oneshot():
                name = proc.name()
                try:
                    cwd = proc.cwd() or ""
                except (psutil.AccessDenied, OSError):
                    cwd = ""
                try:
                    cmdline = " ".join(proc.cmdline())
                except (psutil.AccessDenied, OSError):
                    cmdline = ""
            lowered = name.lower()
            if lowered in _HOST_NAMES or any(mark in cmdline.lower() for mark in _OWN_MARKS):
                continue  # the IDE itself, or us
            hosted = False
            for parent in proc.parents():
                if parent.name().lower() in _HOST_NAMES:
                    hosted = True
                    break
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            continue
        if lowered not in _DEV_NAMES and not hosted:
            continue
        ip = conn.laddr.ip if conn.laddr else ""
        host = "::1" if ip in ("::", "::1") else "127.0.0.1"
        out.append(ListeningApp(port=port, pid=conn.pid, process=name, cwd=cwd, cmdline=cmdline, hosted=hosted, host=host))
    return out


def _same_or_under(path: str, root: str) -> bool:
    if not path or not root:
        return False
    try:
        p = Path(path).resolve()
        r = Path(root).resolve()
    except OSError:
        return False
    return p == r or r in p.parents


def attribute_apps(apps: list[ListeningApp], sessions: list[Session]) -> dict[str, list[ListeningApp]]:
    """Which session each listener belongs to: by cwd first, then by mention.

    A listener nobody claims but that descends from a Claude host lands under
    the empty key, so the console can still show "something on 3000" rather
    than nothing.
    """
    by_session: dict[str, list[ListeningApp]] = {}
    # Two sessions in the same folder both fit; the live or most recent one
    # is the one the person means.
    sessions = sorted(sessions, key=lambda s: (not s.live, -(s.last_event_at or 0)))
    for app in apps:
        owner: Session | None = None
        best = -1
        for session in sessions:
            if not session.cwd:
                continue
            root = session.cwd
            if _same_or_under(app.cwd, root) or (app.cmdline and root.lower() in app.cmdline.lower()):
                depth = len(Path(root).parts)
                if depth > best:
                    owner, best = session, depth
        if owner is None:
            for session in sessions:
                if app.port in session.mentioned_ports:
                    owner = session
                    break
        key = owner.id if owner else ""
        if owner is None and not app.hosted:
            continue
        by_session.setdefault(key, []).append(app)
    for key in by_session:
        by_session[key].sort(key=lambda a: a.port)
    return by_session


def probe_http(port: int, timeout: float = 0.6, host: str = "127.0.0.1") -> tuple[str, int, str]:
    """Is the thing on this port answering, and what does it call itself?

    Tried on the address it bound first, then the other loopback: a server on
    ``::1`` is not on ``127.0.0.1``, and vite binds the former by default.
    """
    import http.client

    hosts = [host] + [h for h in ("127.0.0.1", "::1") if h != host]
    response = body = None
    for attempt in hosts:
        try:
            conn = http.client.HTTPConnection(attempt, port, timeout=timeout)
            conn.request("GET", "/", headers={"User-Agent": "arnold", "Accept": "text/html"})
            response = conn.getresponse()
            body = response.read(16384) if "html" in (response.getheader("Content-Type") or "") else b""
            conn.close()
            break
        except (OSError, http.client.HTTPException):
            continue
    if response is None:
        return "down", 0, ""
    title = ""
    if body:
        match = re.search(rb"<title[^>]*>(.*?)</title>", body, flags=re.I | re.S)
        if match:
            title = " ".join(match.group(1).decode("utf-8", "replace").split())[:80]
    return "up", int(response.status), title


# -- the watch --------------------------------------------------------------


class ClaudeWatch:
    """Polls the transcripts, registry and hook file on its own thread."""

    MIN_POLL = 1.0
    # How much of a transcript is read the first time it is seen. Titles and
    # the last few turns are always inside this; the rest is history.
    SEED_BYTES = 768 * 1024
    HEAD_BYTES = 128 * 1024
    MAX_LINE = 4 * 1024 * 1024

    def __init__(
        self,
        config: Any,
        *,
        home: Path | None = None,
        events_file: Path | str | None = None,
        apps_reader: Callable[[], list[ListeningApp]] | None = None,
        prober: Callable[..., tuple[str, int, str]] | None = None,
        registry_reader: Callable[[], list[dict[str, Any]]] | None = None,
        desktop_titles: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self.config = config
        self.home = home or (Path(config.home) if getattr(config, "home", "") else claude_home())
        self.events_file = Path(events_file) if events_file else (Path(config.events_file) if getattr(config, "events_file", "") else None)
        self._apps_reader = apps_reader or listening_apps
        self._prober = prober or probe_http
        self._registry_reader = registry_reader or (lambda: read_registry(self.home))
        self._desktop_titles = desktop_titles or read_desktop_records
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.sessions: dict[str, Session] = {}
        self._pending: list[dict[str, Any]] = []
        self._error = ""
        self._polled_at = 0.0
        self._scanned_at = 0.0
        self._apps_at = 0.0
        self._titles_at = 0.0
        self._titles: dict[str, dict[str, Any]] = {}
        self._events_offset = 0
        # The hook file is read from the top on every start so the state is
        # right, but what happened before the watch began is history, not news.
        self._started_at = _now()
        self._apps: dict[int, ListeningApp] = {}
        self._apps_seeded = False
        self._stray: list[dict[str, Any]] = []
        # The dashboard's own port is not a dev server, whichever folder the
        # agent happens to run from.
        self._skip_pids = {os.getpid(), os.getppid()}

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        if not self.config.enabled or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="claude-watch", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _run(self) -> None:
        interval = max(self.MIN_POLL, float(self.config.poll_seconds))
        while not self._stop.is_set():
            started = time.monotonic()
            self.poll()
            self._stop.wait(max(0.25, interval - (time.monotonic() - started)))

    # -- polling ----------------------------------------------------------

    def poll(self) -> None:
        """One pass over everything. Never raises."""
        try:
            self._poll()
            with self._lock:
                self._error = ""
        except Exception as exc:
            with self._lock:
                if str(exc) != self._error:
                    log.warning("claude session watch failed: %s", exc)
                self._error = str(exc)
        finally:
            with self._lock:
                self._polled_at = _now()

    def _poll(self) -> None:
        now = _now()
        fresh: list[dict[str, Any]] = []

        if now - self._scanned_at >= max(2.0, float(self.config.scan_seconds)):
            self._discover()
            self._scanned_at = now
        if now - self._titles_at >= 20.0:
            try:
                raw = self._desktop_titles() or {}
                # Accept the plain {id: title} shape too, for a simpler reader.
                self._titles = {
                    k: (v if isinstance(v, dict) else {"title": str(v), "desktop": "", "current": "", "superseded": False})
                    for k, v in raw.items()
                }
            except Exception as exc:
                log.debug("desktop titles unavailable: %s", exc)
            self._titles_at = now

        registry = {}
        try:
            for record in self._registry_reader():
                registry[str(record.get("sessionId"))] = record
        except Exception as exc:
            log.debug("session registry unreadable: %s", exc)

        with self._lock:
            for record in registry.values():
                self._adopt_registered(record)
            for session in list(self.sessions.values()):
                fresh += self._tail(session)
                for event in session.settle():
                    event["session"] = session.id
                    fresh.append(event)
                self._apply_registry(session, registry.get(session.id))
                record = self._titles.get(session.id)
                if record is not None:
                    if not session.title and record.get("title"):
                        session.title = record["title"]
                    session.desktop_id = record.get("desktop") or ""
                    session.superseded = bool(record.get("superseded")) and not session.live
            fresh += self._read_hooks()
            self._forget_old(now)

        if now - self._apps_at >= max(3.0, float(self.config.apps_seconds)):
            fresh += self._refresh_apps()
            self._apps_at = now

        with self._lock:
            for event in fresh:
                session = self.sessions.get(event.get("session", ""))
                if session is not None:
                    event.setdefault("name", session.name)
                    event.setdefault("where", session.where)
                event.setdefault("ts", now)
            self._pending.extend(fresh)
            del self._pending[:-200]

    def _discover(self) -> None:
        """Transcripts touched recently become sessions worth watching."""
        projects = self.home / "projects"
        cutoff = _now() - max(60.0, float(self.config.recent_minutes) * 60.0)
        try:
            folders = [entry for entry in os.scandir(projects) if entry.is_dir()]
        except OSError:
            return
        found: list[tuple[str, Path, float]] = []
        for folder in folders:
            try:
                for entry in os.scandir(folder.path):
                    if not entry.is_file() or not entry.name.endswith(".jsonl"):
                        continue
                    try:
                        mtime = entry.stat().st_mtime
                    except OSError:
                        continue
                    if mtime < cutoff:
                        continue
                    found.append((entry.name[:-6], Path(entry.path), mtime))
            except OSError:
                continue
        with self._lock:
            for session_id, path, mtime in found:
                session = self.sessions.get(session_id)
                if session is None:
                    self.sessions[session_id] = Session(id=session_id, path=path, last_event_at=mtime)
                elif session.path != path:
                    session.path = path

    def _adopt_registered(self, record: dict[str, Any]) -> None:
        session_id = str(record.get("sessionId"))
        if session_id in self.sessions:
            return
        cwd = str(record.get("cwd") or "")
        path = self._transcript_for(session_id, cwd)
        if path is None:
            return
        self.sessions[session_id] = Session(id=session_id, path=path, cwd=cwd)

    def _transcript_for(self, session_id: str, cwd: str) -> Path | None:
        projects = self.home / "projects"
        if cwd:
            slug = re.sub(r"[^A-Za-z0-9]", "-", cwd)
            candidate = projects / slug / f"{session_id}.jsonl"
            if candidate.is_file():
                return candidate
        try:
            for folder in os.scandir(projects):
                candidate = Path(folder.path) / f"{session_id}.jsonl"
                if candidate.is_file():
                    return candidate
        except OSError:
            pass
        return None

    def _apply_registry(self, session: Session, record: dict[str, Any] | None) -> None:
        if record is None:
            if session.pid:
                session.pid, session.socket, session.registry_status = 0, "", ""
            return
        session.pid = int(record.get("pid") or 0)
        session.socket = str(record.get("messagingSocketPath") or "")
        session.registry_name = str(record.get("name") or "")
        session.registry_status = str(record.get("status") or "")
        if not session.cwd and record.get("cwd"):
            session.cwd = str(record["cwd"])
        if not session.entrypoint and record.get("entrypoint"):
            session.entrypoint = str(record["entrypoint"])
        # The registry knows whether the process is mid-turn; the transcript
        # only knows what has been written so far. Where they disagree, the
        # process wins, unless it is the hooks that said "waiting".
        if session.registry_status == "idle" and session.turn == "busy":
            session.turn, session.activity, session.activity_tool = "idle", "", ""
        elif session.registry_status == "busy" and session.turn == "idle" and _now() - session.last_reply_at > 2.0:
            session.turn = "busy"
            if not session.activity:
                session.activity = "working"

    def _tail(self, session: Session) -> list[dict[str, Any]]:
        """New lines since last time, or the tail of the file the first time."""
        events: list[dict[str, Any]] = []
        try:
            size = session.path.stat().st_size
        except OSError:
            return events
        if size < session._offset:
            session._offset = 0  # rewritten (a compaction, say); start over
        if size == session._offset:
            self._count_subagents(session)
            return events

        try:
            with open(session.path, "rb") as fh:
                if not session._seeded and size - session._offset > self.SEED_BYTES:
                    # Only the prompt rows say which permission mode the
                    # session runs in, and a long session's last prompt can
                    # be megabytes behind its tail. The first one is at the
                    # top, so read that much of the head as well.
                    self._seed_head(session, fh.read(self.HEAD_BYTES))
                    fh.seek(size - self.SEED_BYTES)
                    fh.readline()  # discard the partial line
                else:
                    fh.seek(session._offset)
                data = fh.read(size - fh.tell())
                session._offset = fh.tell()
        except OSError as exc:
            log.debug("cannot read %s: %s", session.path, exc)
            return events

        # Keep an unfinished last line for next time rather than parsing half of it.
        if data and not data.endswith(b"\n"):
            cut = data.rfind(b"\n")
            if cut < 0:
                session._offset -= len(data)
                return events
            session._offset -= len(data) - cut - 1
            data = data[: cut + 1]

        for raw in data.split(b"\n"):
            if not raw or len(raw) > self.MAX_LINE:
                continue
            try:
                row = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(row, dict):
                continue
            for event in session.absorb(row):
                event["session"] = session.id
                events.append(event)
            self._note_ports(session, row)
        session._seeded = True
        self._count_subagents(session)
        return events

    @staticmethod
    def _seed_head(session: Session, head: bytes) -> None:
        """Facts from the top of a transcript: cwd, entrypoint, permission mode."""
        for raw in head.split(b"\n"):
            if not raw.startswith(b'{"') or b'"user"' not in raw or b'"permissionMode"' not in raw:
                continue
            try:
                row = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(row, dict) or row.get("type") != "user":
                continue
            for key, attr in (
                ("cwd", "cwd"), ("entrypoint", "entrypoint"), ("version", "version"),
                ("gitBranch", "git_branch"), ("permissionMode", "permission_mode"),
            ):
                value = row.get(key)
                if isinstance(value, str) and value and not getattr(session, attr):
                    setattr(session, attr, value)
            if session.permission_mode:
                return

    def _note_ports(self, session: Session, row: dict[str, Any]) -> None:
        if row.get("type") not in ("user", "assistant"):
            return
        message = row.get("message") or {}
        content = message.get("content")
        if isinstance(content, str):
            session.note_ports(content)
            return
        for block in content or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                session.note_ports(str(block.get("text") or ""))
            elif block.get("type") == "tool_result":
                inner = block.get("content")
                if isinstance(inner, str):
                    session.note_ports(inner[:20000])
                elif isinstance(inner, list):
                    for part in inner:
                        if isinstance(part, dict) and part.get("type") == "text":
                            session.note_ports(str(part.get("text") or "")[:20000])
            elif block.get("type") == "tool_use":
                session.note_ports(json.dumps(block.get("input") or {})[:4000])

    def _count_subagents(self, session: Session) -> None:
        folder = session.path.with_suffix("") / "subagents"
        try:
            recent = _now() - 600
            session.subagents = sum(
                1 for entry in os.scandir(folder)
                if entry.name.endswith(".jsonl") and entry.stat().st_mtime >= recent
            )
        except OSError:
            session.subagents = 0

    def _read_hooks(self) -> list[dict[str, Any]]:
        """Events the installed hooks appended since last time."""
        events: list[dict[str, Any]] = []
        if self.events_file is None:
            return events
        try:
            size = self.events_file.stat().st_size
        except OSError:
            return events
        if size < self._events_offset:
            self._events_offset = 0
        if size == self._events_offset:
            return events
        try:
            with open(self.events_file, "rb") as fh:
                fh.seek(self._events_offset)
                data = fh.read(size - self._events_offset)
                self._events_offset = size
        except OSError:
            return events
        for raw in data.split(b"\n"):
            if not raw.strip():
                continue
            try:
                row = json.loads(raw)
            except ValueError:
                continue
            if isinstance(row, dict):
                out = self._apply_hook(row)
                try:
                    stale = bool(row.get("ts")) and float(row["ts"]) < self._started_at
                except (TypeError, ValueError):
                    stale = False
                if not stale:
                    events += out
        return events

    def _apply_hook(self, row: dict[str, Any]) -> list[dict[str, Any]]:
        session_id = str(row.get("session_id") or "")
        session = self.sessions.get(session_id)
        if session is None:
            path = row.get("transcript_path")
            if path and Path(path).is_file() and _same_or_under(str(Path(path).parent), str(self.home)):
                session = Session(id=session_id, path=Path(path), cwd=str(row.get("cwd") or ""))
                self.sessions[session_id] = session
            else:
                return []
        event = str(row.get("event") or "")
        when = float(row.get("ts") or _now())
        session.hook_at = when
        out: list[dict[str, Any]] = []
        if event == "Notification":
            kind = str(row.get("notification_type") or "")
            message = str(row.get("message") or "")
            if kind == "permission_prompt":
                session.turn = "waiting"
                session.waiting_for = "permission: " + (message or "to use a tool")
                session.hook_state = "permission"
                out.append({"kind": "needs_input", "what": "permission", "detail": message, "session": session.id})
            elif kind == "idle_prompt":
                session.turn, session.waiting_for = "idle", ""
        elif event == "UserPromptSubmit":
            session.turn, session.waiting_for = "busy", ""
            session.activity, session.activity_tool = "thinking", ""
            session.hook_state = "busy"
        elif event == "Stop":
            # The reply is finished. The transcript closes the turn itself
            # once the last block is written; this only settles the state.
            session.turn, session.waiting_for = "idle", ""
            session.activity, session.activity_tool = "", ""
            session.hook_state = "idle"
        elif event == "SessionStart":
            session.hook_state = "started"
            out.append({"kind": "session_started", "session": session.id})
        elif event == "SessionEnd":
            session.hook_state = "ended"
            session.pid, session.socket = 0, ""
            out.append({"kind": "session_ended", "session": session.id})
        return out

    def _forget_old(self, now: float) -> None:
        keep = max(60.0, float(self.config.recent_minutes) * 60.0)
        for session_id, session in list(self.sessions.items()):
            if session.live:
                continue
            if now - max(session.last_event_at, session.first_seen) > keep:
                del self.sessions[session_id]

    def _refresh_apps(self) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        try:
            found = [a for a in self._apps_reader() if a.pid not in self._skip_pids]
        except Exception as exc:
            log.debug("listing dev servers failed: %s", exc)
            return events
        with self._lock:
            sessions = list(self.sessions.values())
        owned = attribute_apps(found, sessions)

        previous = dict(self._apps)
        current: dict[int, ListeningApp] = {}
        for key, apps in owned.items():
            for app in apps:
                earlier = previous.get(app.port)
                if earlier is not None and earlier.pid == app.pid:
                    app.status, app.http_status, app.title, app.checked_at = (
                        earlier.status, earlier.http_status, earlier.title, earlier.checked_at
                    )
                if _now() - app.checked_at >= 15.0:
                    try:
                        app.status, app.http_status, app.title = self._prober(app.port, host=app.host)
                    except TypeError:
                        app.status, app.http_status, app.title = self._prober(app.port)
                    except Exception:
                        app.status = "unknown"
                    app.checked_at = _now()
                app.url = f"http://localhost:{app.port}/"
                current[app.port] = app

        with self._lock:
            for session in sessions:
                session.apps = [a.to_dict() for a in owned.get(session.id, [])]
            self._stray = [a.to_dict() for a in owned.get("", [])]
            # What was already listening when the watch started is context,
            # not news - the same rule the transcripts follow.
            if not self._apps_seeded:
                self._apps_seeded = True
                self._apps = current
                return events
            for port, app in current.items():
                if port not in previous:
                    owner = next((s for s in sessions if any(a["port"] == port for a in s.apps)), None)
                    events.append({"kind": "app_up", "port": port, "url": app.url, "process": app.process,
                                   "session": owner.id if owner else "", "title": app.title})
            for port, app in previous.items():
                if port not in current:
                    owner = next((s for s in sessions if any(a["port"] == port for a in s.apps)), None)
                    events.append({"kind": "app_down", "port": port, "url": app.url, "process": app.process,
                                   "session": owner.id if owner else ""})
            self._apps = current
        return events

    # -- reading ----------------------------------------------------------

    def drain_new(self) -> list[dict[str, Any]]:
        """Everything that happened since the last drain, oldest first."""
        with self._lock:
            out, self._pending = self._pending, []
        return out

    def find(self, which: str) -> Session | None:
        """The session a person means by 'ellipse hub', or None if unclear."""
        with self._lock:
            sessions = list(self.sessions.values())
        return match_session(which, sessions)

    def get(self, session_id: str) -> Session | None:
        with self._lock:
            return self.sessions.get(session_id)

    def listed(self) -> list[Session]:
        """Sessions worth showing: live first, then busiest, then most recent.

        A transcript the desktop app has moved on from is left out, so one
        conversation is one entry - the newest transcript speaks for it.
        """
        with self._lock:
            sessions = [s for s in self.sessions.values() if not s.superseded]
        order = {"waiting": 0, "busy": 1, "idle": 2}
        return sorted(
            sessions,
            key=lambda s: (not s.live, order.get(s.turn, 3), -(s.last_event_at or 0)),
        )

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """The `claude_sessions` section, or nothing when switched off."""
        if not self.config.enabled:
            return {}
        if self._thread is None and _now() - self._polled_at > self.MIN_POLL:
            self.poll()
        sessions = self.listed()
        with self._lock:
            error = self._error
            polled_at = self._polled_at
            stray = list(self._stray)
        rows = [s.to_dict() for s in sessions]
        section: dict[str, Any] = {
            "available": not error,
            "sessions": rows,
            "count": len(rows),
            "busy": sum(1 for r in rows if r["turn"] == "busy"),
            "waiting": sum(1 for r in rows if r["turn"] == "waiting"),
            "live": sum(1 for r in rows if r["live"]),
            "apps": [a for r in rows for a in r["apps"]] + stray,
            "hooks": bool(self.events_file and self.events_file.exists()),
            "polled_at": polled_at,
        }
        if error:
            section["error"] = error
        return {"claude_sessions": section}


def match_session(which: str, sessions: list[Session]) -> Session | None:
    """Pick the session a spoken name refers to.

    Empty means "the obvious one": the only session, else the only one that
    is busy or waiting. Otherwise every word of the name must appear in one
    of the session's labels, and the best-covered label wins.
    """
    wanted = re.sub(r"[^a-z0-9 ]+", " ", (which or "").lower()).split()
    stop = {"the", "session", "project", "one", "claude", "code", "my", "in", "on", "a", "an", "app", "that", "this", "it"}
    words = [w for w in wanted if w not in stop]
    if not words:
        if len(sessions) == 1:
            return sessions[0]
        active = [s for s in sessions if s.turn in ("busy", "waiting")]
        if len(active) == 1:
            return active[0]
        live = [s for s in sessions if s.live]
        if len(live) == 1:
            return live[0]
        return None

    best: tuple[float, Session] | None = None
    for session in sessions:
        for label in session.labels():
            tokens = re.sub(r"[^a-z0-9 ]+", " ", label).split()
            if not tokens:
                continue
            hits = sum(1 for w in words if any(t.startswith(w) or w.startswith(t) for t in tokens))
            if hits == 0:
                continue
            score = hits / len(words) + (0.5 * hits / len(tokens))
            if hits < len(words) and score < 0.9:
                continue
            if best is None or score > best[0]:
                best = (score, session)
    return best[1] if best else None
