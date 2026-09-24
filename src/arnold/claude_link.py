"""Talking to a running Claude Code session.

A live session listens on a named pipe for messages from its peers - that is
how one Claude Code session's SendMessage reaches another on the same machine.
The protocol is two JSON lines: an auth line carrying the token the session
published beside its registry entry, then the message itself. The session
takes it as a user turn, so "Mycroft, tell the Ellipse Hub session to run the
tests" lands in that conversation exactly as if it had been typed there.

When the session has no live process - the desktop app starts one per turn and
lets it go afterwards - there is no pipe to write to. The fallback runs the
prompt through the CLI against the same transcript (``claude -p --resume``),
which appends the exchange to the session so it is there next time the app
resumes it. That is what the watch will see and announce.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

from . import process

log = logging.getLogger(__name__)

_KEY_RE = re.compile(r"^(\d+)\.[0-9a-f]{64}\.key$")
_PIPE_RE = re.compile(r"^[\\/]{2}[.?][\\/]pipe[\\/]", re.I)


class LinkError(Exception):
    """Could not deliver; the message is meant to be spoken."""


def read_peer_token(home: Path, pid: int) -> str:
    """The token a session published for its inbox, or '' if it has none."""
    folder = home / "sessions"
    try:
        names = os.listdir(folder)
    except OSError:
        return ""
    for name in names:
        match = _KEY_RE.match(name)
        if not match or int(match.group(1)) != pid:
            continue
        try:
            with open(folder / name, "r", encoding="utf-8") as fh:
                record = json.load(fh)
        except (OSError, ValueError):
            continue
        token = record.get("peerToken") if isinstance(record, dict) else None
        if isinstance(token, str) and re.fullmatch(r"[0-9a-f]{32}", token):
            return token
    return ""


ENVELOPE = "cross-session-message"
_LABEL_RE = re.compile(r"[^A-Za-z0-9:_/.\\-]")


def envelope(body: str, *, sender: str, name: str, mode: str) -> str:
    """Wrap a message the way one session's SendMessage wraps it for another.

    The receiving session holds a bare message for the person to review when
    it bypasses permission prompts and the sender has not said which class it
    runs in - and a headless or unattended host has nobody to review it, so
    it expires. These words are the person's own, spoken to the assistant, so
    they go in carrying the session's own class: delivered, not parked.

    The shape must be exact - the reader rebuilds it and compares - so the
    attributes come in the harness's order and the body must not close the
    tag itself.
    """
    label = _LABEL_RE.sub("-", sender.strip() or "assistant")[:80]
    shown = re.sub(r'["<>\r\n]', "", name.strip())[:120] or label
    mode = mode if mode in ("bypass", "prompting") else "prompting"
    body = body.replace(f"</{ENVELOPE}", f"<\\/{ENVELOPE}").replace(f"<{ENVELOPE}", f"<\\{ENVELOPE}")
    return f'<{ENVELOPE} from="{label}" from-name="{shown}" from-mode="{mode}">\n{body}\n</{ENVELOPE}>'


def frame(token: str, text: str) -> bytes:
    """The two lines the inbox expects, newline-terminated."""
    lines = []
    if token:
        lines.append(json.dumps({"type": "auth", "token": token}))
    lines.append(json.dumps({"type": "user", "message": {"role": "user", "content": text}}))
    return ("\n".join(lines) + "\n").encode("utf-8")


def _open_pipe(path: str, attempts: int = 8):
    """Open a named pipe as a byte stream, waiting out a busy instance."""
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            return open(path, "r+b", buffering=0)
        except OSError as exc:
            last = exc
            # ERROR_PIPE_BUSY (231): every instance is taken; the server will
            # free one shortly. Anything else is a real failure.
            if getattr(exc, "winerror", None) == 231:
                time.sleep(0.15 * (attempt + 1))
                continue
            break
    raise LinkError(f"the session's inbox is not reachable ({last})")


def send_over_pipe(path: str, token: str, text: str, *, settle: float = 0.3) -> dict[str, Any]:
    """Write the message and hang up.

    Write-only on purpose. The inbox acknowledges to a *reply address*, not
    on this connection, and a blocking read on a Windows pipe cannot be
    cancelled cleanly from another thread - so nothing here waits for words
    that are not coming. A broken pipe on write is the one failure that can
    be seen, and it is reported.
    """
    if not _PIPE_RE.match(path) and not path.endswith(".sock"):
        raise LinkError("that session's inbox address is not one I know how to open")
    handle = _open_pipe(path)
    try:
        try:
            handle.write(frame(token, text))
            handle.flush()
        except OSError as exc:
            raise LinkError(f"the session hung up before taking the message ({exc})") from exc
        # Give the server a moment to read the lines before the handle goes:
        # closing immediately after the write can race the read on some
        # pipe servers and be seen as a client that sent nothing.
        time.sleep(max(0.0, settle))
    finally:
        try:
            handle.close()
        except OSError:
            pass
    return {"delivered": True, "via": "pipe"}


# -- the CLI fallback ---------------------------------------------------------

_CLI_CANDIDATES = (
    r"%USERPROFILE%\.local\bin\claude.exe",
    r"%LOCALAPPDATA%\Programs\claude\claude.exe",
    r"%APPDATA%\npm\claude.cmd",
)


_version_cache: dict[str, tuple[int, ...]] = {}


def cli_version(path: str) -> tuple[int, ...]:
    """What `claude --version` says, as a tuple; (0,) if it will not say."""
    if path in _version_cache:
        return _version_cache[path]
    try:
        out = process.run([path, "--version"], timeout=20).stdout or ""
    except Exception:
        out = ""
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", out)
    version = tuple(int(n) for n in match.groups()) if match else (0,)
    _version_cache[path] = version
    return version


def find_cli(configured: str = "") -> str:
    """The Claude Code executable.

    A configured path wins. Otherwise every copy on the machine is a
    candidate - PATH, the usual installs, the ones the VS Code extension and
    the desktop app carry - and the newest is used. The desktop app updates
    its own copy and leaves a standalone install behind, and an old copy
    refuses the current models outright, so "first found" is the wrong rule.
    """
    configured = (configured or "").strip()
    if configured:
        if not Path(configured).exists():
            raise LinkError(f"Claude Code isn't at {configured}.")
        return configured
    candidates: list[str] = []
    found = shutil.which("claude")
    if found:
        candidates.append(found)
    for candidate in _CLI_CANDIDATES:
        expanded = Path(os.path.expandvars(candidate))
        if expanded.exists():
            candidates.append(str(expanded))
    candidates += [str(p) for p in _bundled_clis()]
    if not candidates:
        raise LinkError("I can't find the Claude Code CLI. Set claude.cli_path in the config.")
    best = max(candidates, key=cli_version)
    log.debug("claude cli: %s (%s)", best, ".".join(map(str, cli_version(best))))
    return best


def _bundled_clis() -> list[Path]:
    """Copies that ship inside the VS Code extension or the desktop app, newest first."""
    out: list[tuple[str, Path]] = []
    home = Path.home()
    for ext in (home / ".vscode" / "extensions").glob("anthropic.claude-code-*"):
        binary = ext / "resources" / "native-binary" / "claude.exe"
        if binary.is_file():
            out.append((ext.name, binary))
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        for package in Path(local, "Packages").glob("Claude_*"):
            folder = package / "LocalCache" / "Roaming" / "Claude" / "claude-code"
            for version in folder.glob("*"):
                binary = version / "claude.exe"
                if binary.is_file():
                    out.append((version.name, binary))
    out.sort(key=lambda item: _version_key(item[0]), reverse=True)
    return [path for _, path in out]


def _version_key(name: str) -> tuple[int, ...]:
    numbers = re.findall(r"\d+", name)
    return tuple(int(n) for n in numbers[:4]) or (0,)


def resume_headless(cli: str, session_id: str, cwd: str, text: str, *, permission_mode: str = "acceptEdits", log_dir: Path | None = None) -> dict[str, Any]:
    """Run one turn of the session through the CLI, detached.

    Nothing waits on it: the transcript gains the exchange as it happens, and
    the watch sees the reply land the same way it sees any other turn.
    """
    argv = [cli, "-p", text, "--resume", session_id, "--output-format", "json", "--permission-mode", permission_mode]
    # The signed-in account, never a metered key that happens to be in the
    # environment - the same rule code.py applies.
    env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
    output: Any = None
    log_path: Path | None = None
    if log_dir is not None:
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            log_path = log_dir / f"claude-resume-{session_id[:8]}-{int(time.time())}.log"
            output = open(log_path, "ab")
        except OSError:
            output = None
    try:
        proc = process.launch_quiet(argv, cwd=cwd or None, env=env, output=output)
    except OSError as exc:
        raise LinkError(f"Claude Code would not start: {exc}") from exc
    finally:
        if output is not None:
            try:
                output.close()
            except OSError:
                pass
    return {"delivered": True, "via": "resume", "pid": proc.pid, "log": str(log_path) if log_path else ""}


def _not_running(session: Any) -> str:
    where = getattr(session, "where", "") or ""
    if where == "Desktop":
        return (
            f"The {session.name} session isn't running right now - the desktop app only "
            "keeps it alive while it works. Open it there and send it anything, then tell me again."
        )
    return f"The {session.name} session isn't running right now, so there is nothing to speak to."


# -- one door -----------------------------------------------------------------


def deliver(
    session: Any,
    text: str,
    config: Any,
    *,
    home: Path,
    log_dir: Path | None = None,
    sender: str = "assistant",
) -> dict[str, Any]:
    """Get `text` into `session` by whichever route is open.

    `session` is a monitors.claude.Session. The pipe when the process is alive
    and listening; otherwise the CLI, if the config allows it. `sender` is
    the assistant's name, shown to the session as who the words came from.
    """
    text = (text or "").strip()
    if not text:
        raise LinkError("There is nothing to send.")
    prefix = (getattr(config, "prompt_prefix", "") or "").strip()
    body = f"{prefix} {text}".strip() if prefix else text

    if session.live:
        # A live process owns the transcript; nothing else may write to it,
        # so the pipe is the only door and its failure is final.
        if not session.socket:
            raise LinkError(f"The {session.name} session is running but not taking messages.")
        token = read_peer_token(home, session.pid)
        mode = getattr(session, "mode_class", "") or getattr(config, "assume_permission_class", "bypass")
        wrapped = envelope(body, sender=sender.lower(), name=sender, mode=mode)
        return send_over_pipe(session.socket, token, wrapped)

    if not getattr(config, "resume_fallback", True):
        raise LinkError(_not_running(session))
    if getattr(session, "desktop_id", ""):
        # The desktop app forks a fresh transcript for every turn it starts
        # after the process has gone. A turn appended to this one through
        # the CLI would live in a file the app never reads again - invisible
        # in the app, and not part of what it sends next. So no.
        raise LinkError(_not_running(session))
    cli = find_cli(getattr(config, "cli_path", ""))
    return resume_headless(
        cli, session.id, session.cwd, body,
        permission_mode=getattr(config, "permission_mode", "acceptEdits") or "acceptEdits",
        log_dir=log_dir,
    )
