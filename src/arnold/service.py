"""The long-running agent.

One thread ticks: collect a snapshot, publish it, evaluate alert rules, act on
whatever fired. paho's own thread delivers inbound commands, which are verified
and dispatched through the same registry the CLI uses.
"""

from __future__ import annotations

import logging
import signal
import threading
import time
from pathlib import Path
from typing import Any

from . import history as history_module
from . import runtime
from . import scheduler as scheduler_module
from .alerts import AlertEngine, AlertEvent
from .commands import CommandContext, build_registry
from .config import Config, IdentityWatcher
from .face.local import LocalFaceSender
from .humanize import duration_speech
from .jarvis import JarvisClient
from .monitors.collector import Collector
from .notices import Notice, NoticeBook, ProactiveEngine, World, _in_quiet_hours
from .platform_win import IS_WINDOWS
from .security import AuthError, CommandVerifier
from .speech import Voice
from .state import write_state
from .todos import TodoSync
from .transport.discovery import publish_discovery
from .transport.mqtt import MqttTransport
from .transport.topics import Topics

log = logging.getLogger(__name__)


class AssistantService:
    def __init__(self, config: Config) -> None:
        # Commands that start background work check this: only here does a
        # thread outlive the call that spawned it.
        runtime.mark_agent()

        self.config = config
        self.topics = Topics(config.mqtt.base_topic, config.device.id)

        self.collector = Collector(config.monitors, config.outlook, config.notifications, config.claude)
        self.alerts = AlertEngine(config.alerts, device_name=config.device.friendly_name)
        self.registry = build_registry()
        self.jarvis = JarvisClient(config.jarvis)
        # Whatever the agent volunteers goes through this rather than straight
        # to the Pi, so a Pi that is off means "said here" and not "lost".
        self._local_face = LocalFaceSender(config)
        self.speech = Voice(config, jarvis=self.jarvis, publish=self._publish_face)

        self.verifier = CommandVerifier(
            config.security.shared_secret,
            require_signature=config.security.require_signature,
            max_skew_seconds=config.security.max_clock_skew_seconds,
            allow_destructive=config.security.allow_destructive,
        )

        self.transport: MqttTransport | None = None
        if config.mqtt.enabled:
            self.transport = MqttTransport(
                config.mqtt,
                self.topics,
                on_command=self._on_command,
                on_connected=self._on_connected,
            )

        # What the machine has been doing, so the assistant can talk about
        # where a number is heading rather than only where it is.
        self.history = (
            history_module.History(
                history_module.path_for(config),
                fine=config.history.fine_samples,
                hourly=config.history.hourly_buckets,
            )
            if config.history.enabled
            else None
        )
        self.proactive = (
            ProactiveEngine(
                config,
                self.history,
                NoticeBook(Path(config.state_file).with_name("notices.json")),
            )
            if config.proactive.enabled and self.history is not None
            else None
        )
        self.schedule = (
            scheduler_module.Schedule(
                scheduler_module.path_for(config), config.schedule.max_jobs
            )
            if config.schedule.enabled
            else None
        )

        # The weekly document, watched for on the tick. Cheap: it walks two
        # small folders every couple of minutes and reads nothing otherwise.
        self.todos = TodoSync(config) if config.todo.enabled else None

        self._stop = threading.Event()
        self._discovery_done = False
        self._last_window: tuple[Any, Any] | None = None
        self._ui: Any = None
        # The dashboard reads the identity off this config on every request;
        # following the file keeps its header honest after a profile switch.
        self._watcher = IdentityWatcher(config)

    def _announce_speech_route(self) -> None:
        """Say in the log what will actually be audible, and where."""
        route = self.config.speech_route()
        if route == "none":
            log.warning("speech route 'none': nothing will be spoken")
            return

        probe = self.speech.check()
        pi = probe.get("jarvis") or {}
        local = probe.get("local") or {}
        if route == "jarvis":
            if pi.get("ok"):
                log.info("speech route 'jarvis': %s", pi.get("detail") or "the Pi is ready")
            else:
                log.warning(
                    "speech route 'jarvis' but the Pi is not usable (%s), and nothing "
                    "will be spoken here - set speech.route: auto to fall back",
                    pi.get("detail"),
                )
            return

        if route in ("auto", "both") and pi.get("ok"):
            log.info("speech route '%s': the Pi is answering, so he says it", route)
        elif route in ("auto", "both"):
            log.info(
                "speech route '%s': the Pi is not answering (%s), so it will be "
                "spoken here instead",
                route,
                pi.get("detail"),
            )
        if route in ("local", "both", "auto"):
            if local.get("ok"):
                log.info("this PC can speak (%s)", local.get("backend"))
            else:
                log.warning(
                    "this PC has no local speech backend (%s) - "
                    'install it with: uv pip install -e ".[voice]"',
                    local.get("detail"),
                )

    def _publish_face(self, leaf: str, payload: Any) -> None:
        """Drive the on-screen face while the agent itself is talking."""
        topic = f"{self.config.assistant_topic_prefix()}/{leaf}"
        self._local_face.send(topic, payload)
        if self.transport is None:
            return
        self.transport.publish(topic, payload, retain=True, qos=1)

    @property
    def context(self) -> CommandContext:
        return CommandContext(
            config=self.config,
            collector=self.collector,
            alerts=self.alerts,
            jarvis=self.jarvis,
            speech=self.speech,
        )

    # -- lifecycle ----------------------------------------------------------

    def run(self) -> int:
        self._install_signal_handlers()

        if not IS_WINDOWS:
            log.warning("not running on Windows - control and desktop commands will fail")

        self._announce_speech_route()

        if self.transport:
            self.transport.connect()

        # Only the agent runs the poller. A one-shot `exec` polls inline instead,
        # because a worker thread would be abandoned the moment it printed.
        self.collector.outlook.start()
        self.collector.notifications.start()
        self.collector.claude.start()

        # The dashboard rides along in the agent, sharing its collector and its
        # alert history. Started here rather than in __init__ so nothing binds a
        # port until the agent has decided it is really going to run.
        if self.config.ui.enabled:
            from .ui.server import serve_in_background

            self._ui = serve_in_background(
                self.config,
                collector=self.collector,
                registry=self.registry,
                alerts=self.alerts,
                jarvis=self.jarvis,
                speech=self.speech,
            )

        interval = max(1.0, self.config.telemetry.interval_seconds)
        log.info(
            "agent running as device '%s', publishing every %.0fs",
            self.config.device.id,
            interval,
        )

        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self._tick()
            except Exception:
                log.exception("tick failed; continuing")
            # Subtract the work already done so the cadence stays honest.
            self._stop.wait(max(0.5, interval - (time.monotonic() - started)))

        self._shutdown()
        return 0

    def stop(self) -> None:
        self._stop.set()

    def _install_signal_handlers(self) -> None:
        def handler(signum, frame):
            log.info("received signal %s; shutting down", signum)
            self.stop()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass  # not the main thread, or unsupported on this platform

    def _shutdown(self) -> None:
        log.info("stopping")
        # Before the transport goes, so the face's final "idle" still gets out.
        self.speech.close()
        if self.history is not None:
            # Persist the part-filled hour, or a restart loses it and leaves a
            # gap in the middle of every trend.
            self.history.flush()
        self.collector.outlook.stop()
        self.collector.notifications.stop()
        self.collector.claude.stop()
        if self._ui is not None:
            try:
                self._ui.shutdown()
                self._ui.dashboard.stop()
                self._ui.server_close()
            except Exception as exc:
                log.debug("closing the dashboard: %s", exc)
        if self.transport:
            self.transport.disconnect()

    # -- the tick -----------------------------------------------------------

    def _tick(self) -> None:
        fresh = self._watcher.changed()
        if fresh is not None and self.config.adopt_identity(fresh):
            log.info(
                "identity is now %s (profile %s)",
                self.config.assistant_name(),
                self.config.active_profile or "-",
            )

        snapshot = self.collector.snapshot(
            include_window=self.config.telemetry.publish_active_window
        )

        events = self.alerts.evaluate(snapshot)
        active = self.alerts.active()
        snapshot["alerts"] = {"active": active, "count": len(active)}

        if self.history is not None:
            self.history.record(snapshot)

        # Publish the values a one-shot `exec` cannot work out for itself:
        # alert history, network rates (need two samples), and the foreground
        # window (an SSH session is not in the interactive desktop session).
        write_state(
            Path(self.config.state_file),
            {
                "alerts": {"active": active, "count": len(active)},
                "active_window": snapshot.get("active_window"),
                "network": snapshot.get("network"),
                "device": self.config.device.id,
                "notices": (
                    self.proactive.book.recent(8) if self.proactive is not None else []
                ),
                "jobs": (
                    [job.to_dict() for job in self.schedule.jobs()[:8]]
                    if self.schedule is not None
                    else []
                ),
            },
        )

        if self.transport:
            if not self._discovery_done and self.transport.connected:
                publish_discovery(self.transport, self.config, self.topics, snapshot)
                self.transport.publish(
                    self.topics.capabilities,
                    {"commands": self.registry.describe()},
                    retain=True,
                )
                self._discovery_done = True

            self.transport.publish(
                self.topics.telemetry, snapshot, retain=self.config.telemetry.retain
            )
            self._publish_window_change(snapshot)

        for event in events:
            self._handle_alert(event)

        self._run_due_jobs()
        self._announce_work()
        self._announce_claude()
        self._sync_todos(snapshot)
        self._notice(snapshot, active)

    def _sync_todos(self, snapshot: dict[str, Any]) -> None:
        """Take in the week's document when it turns up, and say so once."""
        if self.todos is None:
            return
        try:
            self.todos.note_toasts(snapshot.get("work_notifications"))
            report = self.todos.sync()
        except Exception:
            log.exception("todo sync failed; continuing")
            return
        if report is None or report.skipped:
            return
        text = (
            f"This week's {self.config.todo.source_name} is in: "
            f"{report.total} item{'s' if report.total != 1 else ''} on the list"
            + (f", {report.added} new." if report.kept else ".")
        )
        if self.proactive is not None:
            self.proactive.book.record(
                Notice(
                    key=f"todo:{self.config.todo.source_name}:{int(time.time())}",
                    text=text,
                    priority=6,
                    detail={"file": report.file, "added": report.added, "kept": report.kept},
                    ts=time.time(),
                ),
                spoken=False,
            )

    # -- acting without being asked -----------------------------------------

    def _announce_claude(self) -> None:
        """Pass on what the Claude Code sessions just did.

        Same arrangement as the work notifications: recorded in the book so
        the console shows it, spoken only for the things worth interrupting
        for - a long job landing, a session stopped and waiting on the person.
        """
        fresh = self.collector.claude.drain_new()
        if not fresh:
            return
        cfg = self.config.claude
        now = time.time()
        quiet = cfg.respect_quiet_hours and _in_quiet_hours(self.config, now)
        can_speak = self.speech.enabled and not quiet

        for event in fresh:
            kind = event.get("kind")
            name = event.get("name") or "a Claude Code session"
            text, priority, speak = "", 4, False
            if kind == "turn_done":
                seconds = float(event.get("seconds") or 0.0)
                took = duration_speech(seconds)
                reply = event.get("reply") or ""
                if reply.startswith("API Error"):
                    # A failed turn is worth hearing about at once, however
                    # short it was: the person is waiting on an answer that
                    # is not coming.
                    text = f"{name} hit an error: {reply}"
                    speak, priority = cfg.speak_when_done, 7
                else:
                    text = f"{name} finished after {took}." + (f" {reply}" if reply else "")
                    speak = cfg.speak_when_done and seconds >= cfg.speak_after_seconds
                    priority = 6 if speak else 3
            elif kind == "needs_input":
                what = event.get("what")
                detail = event.get("detail") or ""
                if what == "permission":
                    text = f"{name} is waiting for permission" + (f": {detail}" if detail else ".")
                else:
                    text = f"{name} has a question for you" + (f": {detail.removeprefix('asking: ')}" if detail else ".")
                speak, priority = cfg.speak_needs_input, 7
            elif kind == "app_up":
                where = event.get("title") or event.get("process") or "a dev server"
                text = f"{name} has {where} up on port {event.get('port')}."
                speak, priority = cfg.speak_apps, 3
            elif kind == "app_down":
                text = f"{name}'s server on port {event.get('port')} has gone away."
                speak, priority = cfg.speak_apps, 3
            elif kind == "turn_started":
                text = f"{name} was asked: {event.get('prompt', '')}"
                priority = 2
            elif kind == "session_started":
                text = f"A Claude Code session started in {name}."
                priority = 2
            elif kind == "session_ended":
                text = f"The {name} session ended."
                priority = 2
            if not text:
                continue

            log.info("claude session: %s", text)
            if self.transport:
                self.transport.publish(self.topics.notice, {"claude": event})
            spoken = bool(speak and can_speak)
            if self.proactive is not None:
                self.proactive.book.record(
                    Notice(
                        key=f"claude:{event.get('session', '')[:8]}:{kind}:{int(event.get('ts') or now)}",
                        text=text,
                        priority=priority,
                        detail=event,
                        ts=float(event.get("ts") or now),
                    ),
                    spoken=spoken,
                )
            if spoken:
                self.speech.say(text[:400])

    def _announce_work(self) -> None:
        """Pass on what Teams and Outlook just put up, if anything.

        These bypass the proactive engine's pacing on purpose: a message from a
        colleague is not a remark about the disk, and "at most six a day" would
        be absurd for it. What they share with notices is the record - the
        book, and so the console - and the quiet hours.
        """
        fresh = self.collector.notifications.drain_new()
        if not fresh:
            return
        cfg = self.config.notifications
        now = time.time()
        quiet = cfg.respect_quiet_hours and _in_quiet_hours(self.config, now)
        speak = cfg.speak and self.speech.enabled and not quiet

        lines: list[str] = []
        for note in fresh:
            text = note.speech(cfg.include_text)
            log.info("work notification: %s", text)
            lines.append(text)
            if self.transport:
                self.transport.publish(self.topics.notice, {"work": note.to_dict()})
            if self.proactive is not None:
                self.proactive.book.record(
                    Notice(
                        key=f"work:{note.app.lower()}:{note.id}",
                        text=text,
                        priority=6,
                        detail=note.to_dict(),
                        ts=note.ts,
                    ),
                    spoken=speak,
                )
        if not speak:
            return

        if len(fresh) <= max(1, cfg.speak_up_to):
            speech = " ".join(line.rstrip(".") + "." for line in lines)
        else:
            counts: dict[str, int] = {}
            for note in fresh:
                counts[note.app] = counts.get(note.app, 0) + 1
            parts = []
            for app, n in counts.items():
                noun = "message" if app == "Teams" else "mail" if app == "Outlook" else "notification"
                plural = "" if n == 1 else ("s" if noun != "mail" else "s")
                parts.append(f"{n} new {app} {noun}{plural}")
            speech = ", ".join(parts) + "."
        self.speech.say(speech)

    def _notice(self, snapshot: dict[str, Any], active: list[str]) -> None:
        """Look for something worth saying, and say it if it is a moment to."""
        if self.proactive is None or not self.proactive.due():
            return
        try:
            notice = self.proactive.run(
                World(
                    self.config,
                    snapshot,
                    self.history,
                    active,
                    links={
                        "mqtt_connected": bool(self.transport and self.transport.connected),
                        "pi_down_since": self.speech.pi_down_since,
                    },
                )
            )
        except Exception:
            log.exception("the proactive pass failed; continuing")
            return
        if notice is None:
            return

        log.info("saying unprompted: %s", notice.text)
        if self.transport:
            self.transport.publish(self.topics.notice, notice.to_dict())
        self.speech.say(notice.text)

    def _run_due_jobs(self) -> None:
        """Fire anything the schedule says is due."""
        if self.schedule is None:
            return
        try:
            due = self.schedule.due()
        except Exception:
            log.exception("reading the schedule failed; continuing")
            return

        for job in due:
            log.info("job %s is due: %s", job.id, job.said or job.what)
            try:
                if job.kind == "command":
                    self._run_job_command(job)
                elif getattr(job, "is_timer", False):
                    # A timer rings; a reminder just speaks. The ring is what
                    # carries across a room with headphones half on.
                    self.speech.say(job.what, ring=self.config.schedule.ring)
                    self._toast_timer(job)
                else:
                    self.speech.say(job.what)
            except Exception:
                log.exception("job %s failed", job.id)
            if self.transport:
                self.transport.publish(self.topics.notice, {"job": job.to_dict()})

    def _toast_timer(self, job: Any) -> None:
        """A ring you slept through is gone; a toast waits in the centre.

        This is the only acknowledgement surface there is - there is no button
        to press and the microphone belongs to the voice process - which is
        also why timers do not escalate.
        """
        if not IS_WINDOWS:
            return
        try:
            from .platform_win import toast

            toast.notify(self.config.assistant_name(), job.what)
        except Exception as exc:
            log.debug("could not show the timer toast: %s", exc)

    def _run_job_command(self, job: Any) -> None:
        """Run a scheduled command, which must still be on the allowlist.

        Checked here as well as when the job was set: the allowlist may have
        been narrowed since, and a standing order is exactly the thing that
        should stop working when permission is withdrawn.
        """
        if job.what not in self.config.schedule.allow_commands:
            log.warning("job %s wants %s, which is not allowed any more", job.id, job.what)
            self.speech.say(
                f"I was going to {job.said or job.what}, but that command "
                "is no longer allowed to run on a schedule."
            )
            return

        result = self.registry.dispatch(job.what, job.args, self.context)
        if result.speech:
            self.speech.say(result.speech)

    def _publish_window_change(self, snapshot: dict[str, Any]) -> None:
        """Emit a discrete event when the foreground window changes."""
        if not self.config.telemetry.publish_active_window or self.transport is None:
            return
        window = snapshot.get("active_window") or {}
        key = (window.get("process"), window.get("title"))
        if key == self._last_window or key == (None, None):
            return
        self._last_window = key
        self.transport.publish(self.topics.window, window)

    def _handle_alert(self, event: AlertEvent) -> None:
        level = logging.WARNING if event.severity in ("warning", "critical") else logging.INFO
        log.log(level, "alert %s %s: %s", event.rule, event.state, event.message)

        if self.transport:
            self.transport.publish(self.topics.alert, event.to_dict())

        if event.speak:
            self.speech.say(event.speech)

        if event.notify and IS_WINDOWS:
            try:
                from .platform_win import toast

                toast.notify(f"{event.severity.title()}: {event.rule}", event.message)
            except Exception as exc:
                log.error("could not show toast for %s: %s", event.rule, exc)

    # -- inbound commands ---------------------------------------------------

    def _on_connected(self) -> None:
        # Force a re-announce so a broker restart that dropped retained
        # messages gets the discovery configs back.
        self._discovery_done = False

    def _on_command(self, topic: str, envelope: dict[str, Any]) -> None:
        command_id = envelope.get("id")
        name = envelope.get("cmd", "<missing>")

        try:
            self.verifier.verify(envelope)
        except AuthError as exc:
            log.warning("rejected command %s from %s: %s", name, topic, exc)
            self._publish_result(
                envelope,
                {"ok": False, "error": str(exc), "cmd": name, "id": command_id},
            )
            return

        log.info("command %s (%s)", name, command_id)
        result = self.registry.dispatch(name, envelope.get("args") or {}, self.context)

        payload = {**result.to_dict(), "cmd": name, "id": command_id, "ts": time.time()}
        self._publish_result(envelope, payload)

        # A command can ask for its own answer to be spoken, which is what makes
        # "Jarvis, how much disk is left?" work without the Pi parsing anything.
        if envelope.get("speak") and result.speech:
            self.speech.say(result.speech)

    def _publish_result(self, envelope: dict[str, Any], payload: dict[str, Any]) -> None:
        if self.transport is None:
            return
        reply_to = envelope.get("reply_to")
        topic = reply_to if isinstance(reply_to, str) and reply_to else self.topics.result
        self.transport.publish(topic, payload)
