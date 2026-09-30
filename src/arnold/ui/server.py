"""A local web dashboard.

The face says how the assistant feels; this says what it knows. It is a
browser page rather than another tkinter window for three reasons: the machine
already renders artifacts in a browser, a page can be opened from the sofa on a
phone if you ever bind it off loopback, and laying out tables and sparklines in
a canvas widget is work that HTML has already done.

`http.server` rather than a framework: this serves one page and five JSON
routes to a single reader, and the project's install already asks enough of the
machine without a web stack on top.

Everything the page can do, `arnold exec` can already do from a
shell on this PC - so the dashboard is exactly as privileged as a terminal
window, and no more. That equivalence is the whole security model, which is why
it binds to loopback and refuses to leave it without a token.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .. import __version__
from ..alerts import AlertEngine
from ..commands import CommandContext, build_registry
from ..config import Config, is_loopback
from ..face.sources import FaceFeed
from ..jarvis import JarvisClient
from ..voice.brain import BrainError, JarvisBrain
from ..monitors.collector import Collector
from ..security import DESTRUCTIVE_COMMANDS
from ..state import read_state
from ..todos import TodoSync

log = logging.getLogger(__name__)

PAGE_FILE = Path(__file__).with_name("index.html")
# Generous for a command's arguments or the whole config file, mean enough
# that a stray upload cannot make this process hold a large body in memory.
MAX_BODY_BYTES = 256 * 1024
# The one route that takes a file: a document for the to-do list, picked in
# the browser's own Open dialog. A weekly update is a few hundred kilobytes.
MAX_UPLOAD_BYTES = 16 * 1024 * 1024
# The Jarvis route probe talks to Home Assistant or SSHes to the Pi, so it is
# far too slow to run on every poll. Refreshed on this cadence instead.
JARVIS_PROBE_SECONDS = 60.0
# With nobody asking, stop collecting. Closing the browser should quiet the
# machine down even if the server is left running.
IDLE_AFTER_SECONDS = 60.0


def _coerce(value: Any) -> Any:
    """Let obvious scalars through as their real types, as the CLI's --arg does.

    The form fields are all strings, but `artifact.create open=false` and
    `control.volume_set level=40` mean the boolean and the number.
    """
    if not isinstance(value, str):
        return value
    text = value.strip()
    lowered = text.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    if text.lstrip("-").isdigit():
        return int(text)
    return text


class Dashboard:
    """The data behind the page. Owns the collector, registry and feeds."""

    def __init__(
        self,
        config: Config,
        *,
        collector: Collector | None = None,
        registry: Any = None,
        alerts: AlertEngine | None = None,
        jarvis: JarvisClient | None = None,
        speech: Any = None,
    ) -> None:
        # The agent hands in its own: sharing its collector means the dashboard
        # and the telemetry it publishes are the same numbers, and its alert
        # engine is the only one with the history the rules need.
        self.config = config
        self.collector = collector or Collector(config.monitors, config.outlook, config.notifications, config.claude)
        self.registry = registry or build_registry()
        self.alerts = alerts or AlertEngine(config.alerts, device_name=config.device.friendly_name)
        self.jarvis = jarvis or JarvisClient(config.jarvis)
        # The agent hands in its own so both share one queue and one backoff;
        # a standalone dashboard builds one for itself.
        if speech is None:
            from ..speech import Voice

            speech = Voice(config, jarvis=self.jarvis)
        self.speech = speech
        # The same feed the face uses, so the orb in the header and the orb on
        # the desktop are never telling two different stories.
        self.feed = FaceFeed(config)
        # The to-do list and the week's document. A standalone dashboard scans
        # for the file itself; with the agent also running, the hash check
        # means whichever of them sees it first takes it and the other passes.
        self.todos = TodoSync(config) if config.todo.enabled else None

        self._lock = threading.Lock()
        self._probe: dict[str, Any] = {"ok": False, "route": config.jarvis.speech_route,
                                       "detail": "not checked yet"}
        self._stop = threading.Event()

        # Telemetry is collected on a clock rather than per request. A snapshot
        # walks every process on the machine and takes a second or two, and CPU
        # percent is a delta against the previous call for the whole process -
        # so two tabs polling in turn would each get half a reading, and one of
        # them would get 0%. One collector, one reading, everybody shares it.
        self._snap_lock = threading.Lock()
        self._snapshot: dict[str, Any] | None = None
        self._asked_at = 0.0

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        self.feed.start()
        # Collect once before the browser opens, so the first page has numbers
        # on it rather than dashes.
        self._collect_once()
        threading.Thread(target=self._collect_loop, name="ui-collect", daemon=True).start()
        threading.Thread(target=self._probe_loop, name="ui-probe", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        self.feed.stop()

    def _collect_once(self) -> None:
        snapshot = self.collector.snapshot()
        with self._snap_lock:
            self._snapshot = snapshot

    def _watched(self) -> bool:
        """Has anyone asked recently? A closed browser should cost nothing."""
        with self._snap_lock:
            return (time.monotonic() - self._asked_at) < IDLE_AFTER_SECONDS

    def _collect_loop(self) -> None:
        interval = max(1.0, self.config.ui.refresh_seconds)
        while not self._stop.is_set():
            started = time.monotonic()
            if self._watched():
                try:
                    self._collect_once()
                except Exception:
                    log.exception("collecting telemetry failed; continuing")
                if self.todos is not None:
                    try:
                        self.todos.sync()
                    except Exception:
                        log.exception("todo sync failed; continuing")
            # Subtract the collection, which is the slow part, so the cadence
            # is the one asked for rather than that plus two seconds.
            self._stop.wait(max(0.25, interval - (time.monotonic() - started)))

    def _probe_loop(self) -> None:
        while not self._stop.is_set():
            try:
                probe = self.jarvis.check()
            except Exception as exc:  # a probe must never take the page down
                probe = {"ok": False, "route": self.config.jarvis.speech_route,
                         "detail": str(exc)}
            with self._lock:
                self._probe = probe
            self._stop.wait(JARVIS_PROBE_SECONDS)

    @property
    def context(self) -> CommandContext:
        return CommandContext(
            config=self.config,
            collector=self.collector,
            alerts=self.alerts,
            jarvis=self.jarvis,
            speech=self.speech,
        )

    # -- reads --------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """The most recent telemetry. Never collects on the caller's thread.

        Except once: a Dashboard nobody has started still has to answer.
        """
        with self._snap_lock:
            self._asked_at = time.monotonic()
            snapshot = self._snapshot
        if snapshot is None:
            self._collect_once()
            with self._snap_lock:
                snapshot = self._snapshot
        return snapshot or {}

    def state(self) -> dict[str, Any]:
        snapshot = self.snapshot()
        details = self.feed.details()
        # Alerts come from the agent's state file, never from our own engine:
        # rules have duration and hysteresis, and an engine that started thirty
        # seconds ago has no history to evaluate them against.
        agent = read_state(Path(self.config.state_file))
        with self._lock:
            probe = dict(self._probe)

        return {
            "device": {
                "id": self.config.device.id,
                "name": self.config.device.friendly_name,
                "host": socket.gethostname(),
                "version": __version__,
            },
            "agent": {
                "running": agent is not None,
                "ts": (agent or {}).get("ts"),
            },
            # Who lives here. The face and the voice session are this
            # assistant's; Jarvis below is the other one, on the Pi.
            "assistant": {
                "name": self.config.assistant_name(),
                "title": self.config.assistant_title(),
                "motto": self.config.assistant_motto(),
                "wake_word": self.config.voice.wake_word,
                "voice": self.config.assistant.voice if not self.config.assistant.mirror_jarvis else "",
                "palette": self.config.face_palette(),
                "design": self.config.face_design(),
                "mirror_jarvis": bool(self.config.assistant.mirror_jarvis),
                "voice_state": details["voice_state"],
                "can_ask": self.can_ask_jarvis(),
            },
            "jarvis": {
                "enabled": bool(self.config.jarvis.enabled),
                "speech_route": self.speech.route,
                "route": probe.get("route"),
                "ok": bool(probe.get("ok")),
                "detail": probe.get("detail", ""),
                "wake_owner": details["wake_owner"],
            },
            "alerts": (agent or {}).get("alerts", {"active": [], "count": 0}),
            # Written by the agent's tick. Absent when it is not running, which
            # the page already says elsewhere.
            "notices": (agent or {}).get("notices", []),
            "jobs": (agent or {}).get("jobs", []),
            "printer": (agent or {}).get("printer"),
            "todos": self.todo_section(),
            "transcript": details["transcript"][-20:],
            "snapshot": snapshot,
            "ts": time.time(),
        }

    def todo_section(self) -> dict[str, Any]:
        """The list and where this week's document stands. Read from the file
        on every call: the agent, the CLI and this page all write it."""
        if self.todos is None:
            return {"enabled": False, "items": [], "open": 0, "done": 0}
        try:
            from ..todos import claude_projects

            with self._snap_lock:
                claude = (self._snapshot or {}).get("claude_sessions")
            projects = claude_projects(self.config, claude)
            return {
                "enabled": True,
                **self.todos.list.describe(),
                "week": self.todos.status(),
                "projects": projects,
                "resolved": self.todos.list.resolve_links(projects),
            }
        except Exception as exc:  # a bad file must not take the page down
            log.debug("could not read the to-do list: %s", exc)
            return {"enabled": True, "items": [], "open": 0, "done": 0, "note": str(exc)}

    def history(self) -> dict[str, Any]:
        """The agent's stored series, so sparklines start populated.

        Read fresh from disk rather than held: the writer is the agent, in
        another process, and this is asked for once when the page loads.
        """
        if not self.config.history.enabled:
            return {"samples": [], "note": "history is switched off in the config"}
        from ..history import History, path_for

        try:
            return {"samples": History(path_for(self.config)).recent()}
        except Exception as exc:
            log.debug("could not read the history: %s", exc)
            return {"samples": [], "note": str(exc)}

    def commands(self) -> dict[str, Any]:
        return {
            "commands": self.registry.describe(),
            "allow_destructive": self.config.security.allow_destructive,
        }

    def log_tail(self, lines: int) -> dict[str, Any]:
        path = self.config.log_file
        if not path:
            return {"lines": [], "path": "", "note": "log_file is not set in the config"}
        try:
            with open(path, "rb") as fh:
                # Enough for a few hundred lines without reading a log that has
                # been running for a month.
                fh.seek(0, 2)
                size = fh.tell()
                fh.seek(max(0, size - 256 * 1024))
                text = fh.read().decode("utf-8", "replace")
        except OSError as exc:
            return {"lines": [], "path": str(path), "note": f"could not read it: {exc}"}
        return {"lines": text.splitlines()[-max(1, lines):], "path": str(path)}

    # -- writes -------------------------------------------------------------

    def run_command(self, name: str, args: dict[str, Any], speak: bool) -> dict[str, Any]:
        command = self.registry.get(name)
        if command is None:
            return {"ok": False, "cmd": name, "error": f"There is no command called {name}."}

        if name in DESTRUCTIVE_COMMANDS and not self.config.security.allow_destructive:
            return {
                "ok": False,
                "cmd": name,
                "error": (
                    f"{name} is destructive, and security.allow_destructive is false. "
                    "Turn it on in config.yaml to allow it."
                ),
            }

        args = {k: _coerce(v) for k, v in (args or {}).items() if v not in ("", None)}

        delegated = self._delegate(command, args, speak)
        if delegated is not None:
            return delegated

        result = self.registry.dispatch(name, args, self.context)
        payload = {**result.to_dict(), "cmd": name, "ts": time.time()}

        if speak and result.speech:
            self.speech.say(result.speech)
        return payload

    def _delegate(self, command, args: dict[str, Any], speak: bool) -> dict[str, Any] | None:
        """Hand a command to the running agent when this process cannot do it.

        The dashboard is started by a person, so it has a desktop and the
        `needs_desktop` case does not arise - but `needs_agent` does: those
        commands start work that outlives the call, and a request handler is
        the shortest-lived process of all.
        """
        from .. import runtime

        if not command.needs_agent or runtime.IS_AGENT:
            return None
        if not self.config.mqtt.enabled:
            return {
                "ok": False,
                "cmd": command.name,
                "error": (
                    f"{command.name} runs in the background, so the agent has to "
                    "take it, and mqtt is disabled - there is no way to hand it over."
                ),
            }

        from ..cli import _round_trip

        try:
            reply = _round_trip(self.config, command.name, args, speak=speak, timeout=25.0)
        except RuntimeError as exc:
            return {
                "ok": False,
                "cmd": command.name,
                "error": f"the agent did not answer ({exc}). Is `arnold run` going?",
            }
        return {**reply, "cmd": command.name, "via": "agent"}

    def import_upload(self, name: str, data: bytes, source: str = "") -> dict[str, Any]:
        """A document chosen in the browser: saved beside the mailed ones, then
        put through `todo.import` like any other file.

        The name is the browser's, reduced to a safe basename; the extension
        has to be one the reader knows, which is also what keeps this from
        being a way to drop arbitrary files on the disk.
        """
        from ..graph_mail import safe_name
        from ..todos import SUPPORTED_EXTENSIONS

        if self.todos is None:
            return {"ok": False, "error": "the to-do list is switched off (todo.enabled)"}
        name = safe_name(Path(name or "").name)
        suffix = Path(name).suffix.lower()
        if suffix not in SUPPORTED_EXTENSIONS:
            return {
                "ok": False,
                "error": f"I can read {', '.join(SUPPORTED_EXTENSIONS)} files, not {suffix or 'that'}.",
            }
        if not data:
            return {"ok": False, "error": "the file was empty"}
        folder = self.todos.save_folder / "uploads"
        try:
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / name
            target.write_bytes(data)
        except OSError as exc:
            return {"ok": False, "error": f"could not save the file: {exc}"}
        args = {"path": str(target)}
        if source:
            args["source"] = source
        return self.run_command("todo.import", args, False)

    # -- the workshop: parts made for the printer ----------------------------

    def part_file(self, name: str, kind: str) -> tuple[bytes, str] | None:
        """One file of one part, for the viewer and the thumbnails. The
        catalog refuses any name that is not a part's stem, so this cannot
        be turned into a way to read the rest of the disk."""
        from ..commands.printer import catalog
        from ..parts import FILE_TYPES

        path = catalog(self.context).file(name, kind)
        if path is None:
            return None
        try:
            return path.read_bytes(), FILE_TYPES[kind.lower()]
        except OSError:
            return None

    def upload_picture(self, name: str, data: bytes) -> dict[str, Any]:
        """A picture chosen in the browser, to sculpt from. Saved under the
        prints folder with a safe name and a picture's extension only."""
        from ..commands.printer import IMAGE_TYPES, prints_dir
        from ..graph_mail import safe_name

        name = safe_name(Path(name or "").name)
        suffix = Path(name).suffix.lower()
        if suffix not in IMAGE_TYPES:
            return {"ok": False, "error": "I can sculpt from a PNG, JPEG or WebP picture."}
        if not data:
            return {"ok": False, "error": "the picture was empty"}
        folder = prints_dir(self.context) / "uploads"
        try:
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / name
            target.write_bytes(data)
        except OSError as exc:
            return {"ok": False, "error": f"could not save the picture: {exc}"}
        return {"ok": True, "path": str(target)}

    # -- the config file ---------------------------------------------------

    def _config_path(self) -> Path | None:
        return self.config.source_path

    def config_file(self) -> dict[str, Any]:
        from .. import config_edit

        path = self._config_path()
        if path is None:
            return {"ok": False, "error": "this dashboard was not started from a config file"}
        try:
            return {"ok": True, **config_edit.read(path)}
        except OSError as exc:
            return {"ok": False, "error": f"could not read {path}: {exc}"}

    def save_config(self, text: str, mtime: Any, restart: bool) -> dict[str, Any]:
        from .. import config_edit

        path = self._config_path()
        if path is None:
            return {"ok": False, "error": "this dashboard was not started from a config file"}
        try:
            expected = str(int(str(mtime))) if mtime is not None else None
        except (TypeError, ValueError):
            expected = None
        try:
            saved = config_edit.save(path, text, expected)
        except config_edit.ConfigEditError as exc:
            return {"ok": False, "error": str(exc)}
        except OSError as exc:
            return {"ok": False, "error": f"could not write {path}: {exc}"}
        log.info("config saved from the dashboard (%s)", path)
        reply: dict[str, Any] = {"ok": True, **saved, "restarting": False}
        if restart:
            error = config_edit.restart_later()
            if error:
                reply["restart_error"] = error
            else:
                reply["restarting"] = True
        return reply

    def _form_reply(self, text: str) -> dict[str, Any]:
        from .. import config_edit, config_schema

        try:
            profiles = list(self.config.profiles())
        except Exception:  # a broken profiles block should not hide the form
            profiles = None
        extra: dict[str, list[str]] = {}
        try:
            from ..booster import list_plans

            extra["game.power_plan"] = list(list_plans().values())
        except Exception:  # not Windows, or powercfg missing
            pass
        sections = config_schema.schema(profiles, extra)
        try:
            raw = config_edit.raw_values(text)
        except Exception as exc:
            return {"ok": False, "error": f"the file is not valid YAML - fix it in the YAML view: {exc}"}
        return {"ok": True, "sections": sections, "values": config_schema.values(raw, sections)}

    def config_form(self) -> dict[str, Any]:
        loaded = self.config_file()
        if not loaded.get("ok"):
            return loaded
        reply = self._form_reply(loaded["text"])
        if reply.get("ok"):
            reply.update(path=loaded["path"], mtime=loaded["mtime"])
        return reply

    def save_config_fields(self, changes: dict[str, Any], mtime: Any, restart: bool) -> dict[str, Any]:
        from .. import config_edit, config_schema

        loaded = self.config_file()
        if not loaded.get("ok"):
            return loaded
        fields = {f["path"]: f for s in config_schema.schema() for f in s["fields"]}
        clean: dict[str, Any] = {}
        problems = []
        for path, value in changes.items():
            field = fields.get(path)
            if field is None:
                problems.append(f"{path}: no such setting")
                continue
            try:
                clean[path] = config_edit.coerce(field, value)
            except (TypeError, ValueError) as exc:
                problems.append(f"{path}: {exc}")
        if problems:
            return {"ok": False, "error": "; ".join(problems)}
        try:
            text = config_edit.apply_changes(loaded["text"], clean)
        except config_edit.ConfigEditError as exc:
            return {"ok": False, "error": str(exc)}
        reply = self.save_config(text, mtime, restart)
        if reply.get("ok"):
            form = self._form_reply(reply["text"])
            reply.update(sections=form.get("sections"), values=form.get("values"))
        return reply

    def say(self, text: str) -> dict[str, Any]:
        """Say it aloud, wherever speech.route points - the Pi, this PC's own
        speakers, or both. Queued rather than spoken here, so a slow round trip
        does not hold the request open."""
        text = (text or "").strip()[:1000]
        if not text:
            return {"ok": False, "error": "Nothing to say."}
        if not self.speech.say(text):
            return {"ok": False, "error": f"Nothing was said (route is {self.speech.route})."}
        return {"ok": True, "spoke": text, "route": self.speech.route}

    def can_ask_jarvis(self) -> bool:
        """The ask line exists only when this is a separate assistant with the
        Home Assistant relay to reach the other one."""
        return bool(
            not self.config.assistant.mirror_jarvis
            and self.config.jarvis.enabled
            and self.config.jarvis.home_assistant.token
        )

    def ask(self, text: str) -> dict[str, Any]:
        """Put a question to Jarvis and hand back his written reply - the same
        line the voice session's ask_jarvis tool uses, from the page."""
        text = (text or "").strip()[:1000]
        if not text:
            return {"ok": False, "error": "Nothing to ask."}
        if not self.can_ask_jarvis():
            return {
                "ok": False,
                "error": (
                    "this PC is Jarvis at the moment (assistant.mirror_jarvis), so there "
                    "is nobody else to ask"
                    if self.config.assistant.mirror_jarvis
                    else "no Home Assistant token, so the relay to the Pi is not configured"
                ),
            }
        try:
            reply = JarvisBrain(self.config, local=None).ask(text)
        except BrainError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "asked": text, "reply": reply or "(Jarvis had nothing to say)"}


class _Handler(BaseHTTPRequestHandler):
    server_version = f"arnold/{__version__}"
    protocol_version = "HTTP/1.1"

    # -- plumbing -----------------------------------------------------------

    @property
    def dashboard(self) -> Dashboard:
        return self.server.dashboard  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        # BaseHTTPRequestHandler prints every request to stderr; at a poll every
        # two seconds that buries anything worth reading.
        log.debug("%s - %s", self.address_string(), fmt % args)

    def _send(self, status: int, body: bytes, content_type: str, *, cache: str = "no-store") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # Nothing here should be framed elsewhere, and almost nothing is
        # cacheable - a part's files are the exception, since a part's name
        # is new each time it is made and its files never change after.
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _drain(self, length: int, cap: int = 4 * 1024 * 1024) -> None:
        """Swallow a body we are about to refuse, up to a limit."""
        remaining = min(length, cap)
        while remaining > 0:
            chunk = self.rfile.read(min(65536, remaining))
            if not chunk:
                return
            remaining -= len(chunk)

    def _read_body(self) -> dict[str, Any] | None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._json({"ok": False, "error": "bad Content-Length"}, 400)
            return None
        if length > MAX_BODY_BYTES:
            # Answering without reading resets the connection mid-upload, and
            # the browser reports a network error instead of the refusal we
            # took the trouble to write. Take the body, then say no.
            self._drain(length)
            self._json({"ok": False, "error": "that request is too large"}, 413)
            self.close_connection = True
            return None
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._json({"ok": False, "error": "body is not JSON"}, 400)
            return None
        if not isinstance(payload, dict):
            self._json({"ok": False, "error": "body must be a JSON object"}, 400)
            return None
        return payload

    # -- guards -------------------------------------------------------------

    def _refuse(self, message: str, status: int) -> None:
        """Say no - after taking the body. Answering a POST before its body
        has been read resets the connection on Windows, and the browser then
        reports a network error instead of the refusal."""
        if self.command == "POST":
            try:
                self._drain(int(self.headers.get("Content-Length") or 0))
            except ValueError:
                pass
        self._json({"ok": False, "error": message}, status)

    def _guard(self, query: dict[str, list[str]]) -> bool:
        """Reject anything that is not this page talking to this server.

        Three separate doors, because a page bound to loopback is not private:
        every browser on the machine can reach it, and so can any web page they
        happen to have open.

        * **Host** must be one we bound to. Stops DNS rebinding, where a
          hostile site resolves its own name to 127.0.0.1 and talks to us.
        * **Origin**, when the browser sends one, must be ours. Cross-origin
          scripted requests carry it; ours does not.
        * **X-CA-UI** must be present on writes. A form post from another page
          cannot set a custom header without a preflight we would refuse, so
          this alone rules out drive-by commands.
        """
        server = self.server  # type: ignore[assignment]
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
        if host and host not in server.allowed_hosts:  # type: ignore[attr-defined]
            self._refuse("unexpected Host header", 403)
            return False

        origin = self.headers.get("Origin")
        if origin and origin.lower() not in server.allowed_origins:  # type: ignore[attr-defined]
            self._refuse("cross-origin request refused", 403)
            return False

        if self.command == "POST" and self.headers.get("X-CA-UI") != "1":
            self._refuse("missing X-CA-UI header", 403)
            return False

        token = server.token  # type: ignore[attr-defined]
        if token:
            offered = self.headers.get("X-CA-Token") or (query.get("token") or [""])[0]
            if not hmac.compare_digest(offered, token):
                self._refuse("bad or missing token", 401)
                return False
        return True

    # -- routes -------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - the base class names it
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if not self._guard(query):
            return

        route = parsed.path.rstrip("/") or "/"
        if route == "/":
            self._serve_page()
        elif route == "/api/state":
            self._json(self.dashboard.state())
        elif route == "/api/commands":
            self._json(self.dashboard.commands())
        elif route == "/api/history":
            self._json(self.dashboard.history())
        elif route == "/api/config":
            self._json(self.dashboard.config_file())
        elif route == "/api/config/form":
            self._json(self.dashboard.config_form())
        elif route == "/api/log":
            try:
                lines = int((query.get("lines") or ["0"])[0])
            except ValueError:
                lines = 0
            self._json(self.dashboard.log_tail(lines or self.dashboard.config.ui.log_lines))
        elif route == "/api/prints/file":
            found = self.dashboard.part_file((query.get("name") or [""])[0],
                                             (query.get("kind") or [""])[0])
            if found is None:
                self._json({"ok": False, "error": "no such part file"}, 404)
            else:
                self._send(200, *found, cache="private, max-age=86400")
        elif route == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
        else:
            self._json({"ok": False, "error": "no such route"}, 404)

    def _read_upload(self) -> bytes | None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._json({"ok": False, "error": "bad Content-Length"}, 400)
            return None
        if length <= 0:
            self._json({"ok": False, "error": "no file was sent"}, 400)
            return None
        if length > MAX_UPLOAD_BYTES:
            self._drain(length)
            self._json({"ok": False, "error": "that file is too large (16 MB at most)"}, 413)
            self.close_connection = True
            return None
        return self.rfile.read(length)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if not self._guard(query):
            return
        route = parsed.path.rstrip("/")

        if route == "/api/import":
            # The body is the file itself, not JSON; the name rides in a
            # header (or the query, for a client that cannot set one).
            data = self._read_upload()
            if data is None:
                return
            name = self.headers.get("X-File-Name") or (query.get("name") or [""])[0]
            source = (query.get("source") or [""])[0]
            self._json(self.dashboard.import_upload(name, data, source))
            return
        if route == "/api/prints/upload":
            data = self._read_upload()
            if data is None:
                return
            name = self.headers.get("X-File-Name") or (query.get("name") or [""])[0]
            self._json(self.dashboard.upload_picture(name, data))
            return

        payload = self._read_body()
        if payload is None:
            return

        if route == "/api/exec":
            name = str(payload.get("cmd") or "").strip()
            args = payload.get("args")
            self._json(
                self.dashboard.run_command(
                    name,
                    args if isinstance(args, dict) else {},
                    bool(payload.get("speak")),
                )
            )
        elif route == "/api/say":
            self._json(self.dashboard.say(str(payload.get("text") or "")))
        elif route == "/api/config":
            text = payload.get("text")
            if not isinstance(text, str) or not text.strip():
                self._json({"ok": False, "error": "no config text was sent"}, 400)
                return
            self._json(self.dashboard.save_config(text, payload.get("mtime"), bool(payload.get("restart"))))
        elif route == "/api/config/form":
            changes = payload.get("changes")
            if not isinstance(changes, dict) or not changes:
                self._json({"ok": False, "error": "no changes were sent"}, 400)
                return
            self._json(self.dashboard.save_config_fields(
                changes, payload.get("mtime"), bool(payload.get("restart"))))
        elif route == "/api/ask":
            self._json(self.dashboard.ask(str(payload.get("text") or "")))
        else:
            self._json({"ok": False, "error": "no such route"}, 404)

    def _serve_page(self) -> None:
        try:
            page = PAGE_FILE.read_text(encoding="utf-8")
        except OSError as exc:
            log.error("could not read the dashboard page: %s", exc)
            self._json({"ok": False, "error": "the dashboard page is missing"}, 500)
            return
        # The page holds the token in a variable and sends it as a header. A
        # cookie would be sent by the browser on any request to this origin,
        # including one another page made - which is the thing to avoid.
        cfg = self.dashboard.config
        boot = json.dumps(
            {
                "token": self.server.token,  # type: ignore[attr-defined]
                "refresh": cfg.ui.refresh_seconds,
                "logLines": cfg.ui.log_lines,
                # So the tab has the right name and colours before the first
                # poll answers, rather than flashing a generic console.
                "assistant": cfg.assistant_name(),
                "title": cfg.assistant_title(),
                "motto": cfg.assistant_motto(),
                "palette": cfg.face_palette(),
                "design": cfg.face_design(),
            }
        )
        page = page.replace("__BOOT__", boot, 1)
        self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")


class UiServer(ThreadingHTTPServer):
    daemon_threads = True
    # Not on Windows. There SO_REUSEADDR does not mean "rebind a port still in
    # TIME_WAIT", it means "bind a port another process is already listening
    # on" - so starting a second dashboard would quietly split the requests
    # between two servers instead of failing with the port already in use.
    allow_reuse_address = os.name != "nt"

    def __init__(self, config: Config, dashboard: Dashboard | None = None) -> None:
        ui = config.ui
        host = ui.host or "127.0.0.1"
        if ":" in host:  # an IPv6 literal
            self.address_family = socket.AF_INET6
        super().__init__((host, ui.port), _Handler)

        self.dashboard = dashboard or Dashboard(config)
        self.token = ui.token
        # What a browser may legitimately put in Host and Origin. Loopback has
        # several spellings and people type all of them.
        names = {host.strip("[]").lower()}
        if is_loopback(host):
            names |= {"localhost", "127.0.0.1", "::1"}
        self.allowed_hosts = names
        self.allowed_origins = {
            f"http://{name}:{ui.port}" for name in names
        } | {f"http://[{name}]:{ui.port}" for name in names if ":" in name}


def dashboard_url(config: Config) -> str:
    host = "localhost" if is_loopback(config.ui.host) else config.ui.host
    url = f"http://{host}:{config.ui.port}/"
    return url + f"?token={config.ui.token}" if config.ui.token else url


def already_serving(config: Config, timeout: float = 0.4) -> bool:
    """Is something listening on the dashboard's port?

    A connect is the whole test. The port is on loopback, so whatever answers
    is this machine's own dashboard - either the agent's or a standalone one -
    and starting a second would only take the port away from it.
    """
    host = config.ui.host or "127.0.0.1"
    if host in ("0.0.0.0", "::"):  # bound to everything; reach it on loopback
        host = "127.0.0.1"
    try:
        with socket.create_connection((host.strip("[]"), config.ui.port), timeout):
            return True
    except OSError:
        return False


def ensure_serving(config: Config) -> tuple[str, bool]:
    """The dashboard URL, having made sure something is serving it.

    Returns the URL and whether this call is what started it. Raises OSError if
    nothing was serving and one could not be started, because the caller is
    about to point a browser at the URL and an error page is a worse answer
    than a sentence saying why.
    """
    if already_serving(config):
        return dashboard_url(config), False

    server = serve_in_background(config)
    if server is None:
        raise OSError(
            "nothing is serving the dashboard and I could not start it - "
            "run `arnold ui`, or check ui.host and ui.port"
        )
    return dashboard_url(config), True


def serve_in_background(config: Config, **shared: Any) -> UiServer | None:
    """Serve the dashboard from inside the running agent, on its own thread.

    Returns None rather than raising if the port is taken or the bind is
    refused: the agent's job is telemetry and alerts, and it must not fail to
    do that because a browser page could not be served.
    """
    ui = config.ui
    if not is_loopback(ui.host) and not ui.token:
        log.error("not serving the dashboard: ui.host is %r and ui.token is empty", ui.host)
        return None
    try:
        server = UiServer(config, Dashboard(config, **shared))
    except OSError as exc:
        log.warning("not serving the dashboard on %s:%s - %s", ui.host, ui.port, exc)
        return None

    from .. import runtime

    runtime.mark_resident()
    server.dashboard.start()
    threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.5},
        name="ui-server", daemon=True,
    ).start()
    log.info("dashboard on %s", dashboard_url(config))
    return server


def serve(config: Config) -> int:
    """Run the dashboard until interrupted."""
    ui = config.ui
    if not is_loopback(ui.host) and not ui.token:
        log.error(
            "ui.host is %r, which the network can reach, but ui.token is empty. "
            "Set a token or bind to 127.0.0.1 - the dashboard can run every command.",
            ui.host,
        )
        return 2

    from .. import runtime

    try:
        server = UiServer(config)
    except OSError as exc:
        log.error("could not listen on %s:%s - %s", ui.host, ui.port, exc)
        return 1

    runtime.mark_resident()
    url = dashboard_url(config)
    server.dashboard.start()
    log.info("dashboard on %s", url)
    print(f"dashboard: {url}")

    if ui.open_browser:
        import webbrowser

        threading.Timer(0.4, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.dashboard.stop()
        server.server_close()
    return 0
