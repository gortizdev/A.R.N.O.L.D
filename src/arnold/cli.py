"""Command-line interface.

`exec` is the important one for the Pi: Jarvis already reaches this machine over
SSH with PowerShell (assistant.py:3014, `_pc_ssh()`), so

    ssh geogo@192.168.1.103 arnold exec query.disk --arg drive=C

gets a JSON answer with a ready-to-speak `speech` field, reusing the trust that
already exists instead of requiring new credentials.
"""

from __future__ import annotations

import argparse
import json
import logging
import secrets
import sys
import time
from typing import Any

from . import __version__
from .alerts import AlertEngine, RuleError
from .commands import CommandContext, build_registry
from .config import Config, ConfigError, load_config
from .humanize import bytes_human, duration_speech
from .jarvis import JarvisClient
from .logging_setup import setup_logging
from .monitors.collector import Collector

log = logging.getLogger(__name__)


def _build_context(config: Config, speech: Any = None) -> CommandContext:
    return CommandContext(
        config=config,
        collector=Collector(config.monitors, config.outlook, config.notifications, config.claude),
        alerts=AlertEngine(config.alerts, device_name=config.device.friendly_name),
        jarvis=JarvisClient(config.jarvis),
        speech=speech,
    )


def _parse_args_pairs(
    pairs: list[str] | None, json_blob: str | None, json_b64: str | None = None
) -> dict[str, Any]:
    """Merge repeated `--arg k=v` with an optional `--json` object.

    `--json-b64` takes the same object base64-encoded. Remote callers should
    prefer it: this machine's SSH sessions run PowerShell, and raw JSON's
    quotes and spaces do not survive being joined into a remote command line.
    """
    args: dict[str, Any] = {}
    if json_b64:
        import base64

        try:
            json_blob = base64.b64decode(json_b64).decode("utf-8")
        except Exception as exc:
            raise SystemExit(f"--json-b64 is not valid base64: {exc}")
    if json_blob:
        try:
            parsed = json.loads(json_blob)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"--json is not valid JSON: {exc}")
        if not isinstance(parsed, dict):
            raise SystemExit("--json must be a JSON object")
        args.update(parsed)

    for pair in pairs or []:
        key, sep, value = pair.partition("=")
        if not sep:
            raise SystemExit(f"--arg expects key=value, got {pair!r}")
        # Let obvious scalars through as their real types; leave the rest as strings.
        lowered = value.lower()
        if lowered in ("true", "false"):
            args[key] = lowered == "true"
        elif value.lstrip("-").isdigit():
            args[key] = int(value)
        else:
            args[key] = value
    return args


# -- subcommands -------------------------------------------------------------


def cmd_run(args: argparse.Namespace) -> int:
    from .service import AssistantService

    config = load_config(args.config)
    setup_logging(args.log_level or config.log_level, config.log_file)

    problems = config.validate()
    if problems:
        for problem in problems:
            log.error("config: %s", problem)
        if not args.force:
            log.error("refusing to start; fix the above or pass --force")
            return 2

    return AssistantService(config).run()


def _delegate_if_headless(config, registry, name: str, command_args: dict, args) -> dict | None:
    """Forward a desktop command to the agent when we cannot reach the screen.

    Returns the agent's reply, or None to mean "run it here after all".
    """
    from .platform_win.session import has_interactive_desktop
    from .runtime import IS_AGENT

    command = registry.get(name)
    if command is None:
        return None
    # Two reasons to hand a command to the resident agent: it has to draw on
    # the logged-on desktop and we are in session 0, or it starts background
    # work that would die with this short-lived process.
    if command.needs_desktop and not has_interactive_desktop():
        why = "needs the desktop"
    elif command.needs_agent and not IS_AGENT:
        why = "runs in the background"
    else:
        return None

    if not config.mqtt.enabled:
        return {
            "ok": False,
            "cmd": name,
            "error": (
                f"{name} {why}, so it has to run in the agent, and mqtt is "
                "disabled - there is no way to hand it over."
            ),
        }

    log.info("%s %s; forwarding to the agent", name, why)
    try:
        reply = _round_trip(
            config, name, command_args, speak=args.speak, timeout=25.0
        )
    except RuntimeError as exc:
        return {
            "ok": False,
            "cmd": name,
            "error": (
                f"{name} {why}, and the agent did not answer ({exc}). "
                "Is it running?"
            ),
        }
    # `speak` was handled by the agent; do not let the caller repeat it.
    args.speak = False
    return {**reply, "cmd": name, "via": "agent"}


def cmd_exec(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    # Quiet by default: Jarvis calls this over SSH and only wants the JSON on
    # stdout. Probe warnings would otherwise land on stderr on every call.
    setup_logging(args.log_level or "ERROR", "")

    # Destructive commands are gated here too, not just on the MQTT path.
    from .security import DESTRUCTIVE_COMMANDS

    if args.command in DESTRUCTIVE_COMMANDS and not config.security.allow_destructive:
        payload = {
            "ok": False,
            "cmd": args.command,
            "error": f"{args.command} is destructive and security.allow_destructive is false",
        }
        print(json.dumps(payload, indent=2 if args.pretty else None))
        return 1

    registry = build_registry()
    command_args = _parse_args_pairs(args.arg, args.json, args.json_b64)

    # Jarvis reaches this over SSH, which lands in session 0 - no visible
    # desktop. Anything that has to appear on screen is forwarded to the agent
    # already running in the logon session rather than done here, where it
    # would succeed silently and show the user nothing.
    delegated = _delegate_if_headless(config, registry, args.command, command_args, args)
    if delegated is not None:
        print(json.dumps(delegated, indent=2 if args.pretty else None, default=str))
        return 0 if delegated.get("ok") else 1

    result = registry.dispatch(args.command, command_args, _build_context(config))

    payload = {**result.to_dict(), "cmd": args.command, "ts": time.time()}
    print(json.dumps(payload, indent=2 if args.pretty else None, default=str))

    if args.speak and result.speech:
        from .speech import Voice

        voice = Voice(config)
        voice.say(result.speech)
        # A one-shot process has to wait for playback, or it exits mid-word.
        voice.close(timeout=60.0)

    return 0 if result.ok else 1


def cmd_say(args: argparse.Namespace) -> int:
    from .speech import Voice

    config = load_config(args.config)
    setup_logging(args.log_level or "WARNING", "")

    route = "local" if getattr(args, "local", False) else (args.route or "")
    voice = Voice(config, route=route)
    text = " ".join(args.text)
    if not voice.say(text, ring=bool(getattr(args, "ring", False))):
        print(f"nothing was said (route is {voice.route!r})", file=sys.stderr)
        return 1
    # Blocks until it has actually been said, which is what a person running
    # this from a terminal is waiting for.
    voice.close(timeout=120.0)
    print(json.dumps({"spoken": True, "route": voice.route, "text": text}))
    return 0


def cmd_diag(args: argparse.Namespace) -> int:
    setup_logging(args.log_level or "WARNING", "")
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"config: {exc}", file=sys.stderr)
        return 2

    print(f"arnold {__version__}")
    print(f"config file       : {config.source_path}")
    print(f"device id         : {config.device.id}")
    print(f"friendly name     : {config.device.friendly_name}")
    print()

    problems = config.validate()
    if problems:
        print("config problems:")
        for problem in problems:
            print(f"  ! {problem}")
    else:
        print("config: OK")
    print()

    try:
        alerts = AlertEngine(config.alerts, device_name=config.device.friendly_name)
        print(f"alert rules       : {len(alerts.rules)} loaded")
    except RuleError as exc:
        print(f"alert rules       : INVALID - {exc}")
        return 2

    registry = build_registry()
    print(f"commands          : {len(registry.names())} registered")
    print()

    print("-- Speech --")
    from .speech import Voice

    voice = Voice(config)
    speech = voice.check()
    print(f"  route           : {speech['route']}")
    pi = speech.get("jarvis")
    if pi is not None:
        status = "OK" if pi["ok"] else "FAIL"
        print(f"  the Pi ({pi['route']:<14}) {status}  {pi.get('detail', '')}")
    local = speech.get("local")
    if local is not None:
        status = "OK" if local["ok"] else "FAIL"
        print(f"  this PC         : {status}  {local.get('backend') or local.get('detail', '')}")
    if speech["route"] == "none":
        print("  nothing will be spoken (speech.route: none)")
    print()

    if config.mqtt.enabled:
        print("-- MQTT --")
        print(f"  broker          : {config.mqtt.host}:{config.mqtt.port}")
        ok, detail = _probe_mqtt(config)
        print(f"  connect         : {'OK' if ok else 'FAIL'}  {detail}")
        print()

    print("-- Snapshot --")
    collector = Collector(config.monitors, config.outlook, config.notifications, config.claude)
    snapshot = collector.snapshot()
    if args.json:
        print(json.dumps(snapshot, indent=2, default=str))
        return 0

    cpu, mem = snapshot["cpu"], snapshot["memory"]
    print(f"  uptime          : {duration_speech(snapshot['uptime_seconds'])}")
    print(f"  cpu             : {cpu['percent']}%  ({cpu['cores_physical']}c/{cpu['cores_logical']}t)")
    print(
        f"  memory          : {mem['percent']}%  "
        f"{bytes_human(mem['used_bytes'])} / {bytes_human(mem['total_bytes'])}"
    )
    for drive, disk in sorted(snapshot["disks"].items()):
        print(
            f"  disk {drive:<11}: {disk['percent']}%  "
            f"{bytes_human(disk['free_bytes'])} free of {bytes_human(disk['total_bytes'])}"
        )
    for gpu in snapshot.get("gpus") or []:
        print(
            f"  gpu {gpu.get('index')}           : {gpu.get('name')}  "
            f"{gpu.get('utilization_percent')}%  {gpu.get('temperature_c')}C"
        )
    if snapshot.get("battery"):
        print(f"  battery         : {snapshot['battery']['percent']}%")
    window = snapshot.get("active_window") or {}
    if window.get("title"):
        print(f"  active window   : {window['title'][:60]} ({window.get('process')})")
    print(f"  processes       : {snapshot['processes']['count']}")

    if config.outlook.enabled:
        print()
        print("-- Outlook --")
        mail, calendar = snapshot.get("mail") or {}, snapshot.get("calendar") or {}
        if mail:
            detail = (
                f"{mail.get('unread')} unread"
                if mail.get("available")
                else f"FAIL  {mail.get('error')}"
            )
            print(f"  mail            : {detail}")
        if calendar:
            if calendar.get("available"):
                events = calendar.get("events") or []
                nxt = calendar.get("next")
                detail = f"{len(events)} event(s) in the next {config.outlook.lookahead_hours:.0f}h"
                if nxt:
                    detail += f", next in {calendar.get('minutes_until_next'):.0f} min"
                print(f"  calendar        : {detail}")
            else:
                print(f"  calendar        : FAIL  {calendar.get('error')}")
        # The single most common reason for both to fail, and not obvious from the
        # error: the classic client is what has COM, and the new one is what most
        # people now have open.
        if not mail.get("available") and not calendar.get("available"):
            print("  note            : needs the classic Outlook desktop client, signed in")
            print("                    to an account. The new Outlook has no COM automation.")
    return 0


def _probe_mqtt(config: Config) -> tuple[bool, str]:
    """Connect, wait for CONNACK, disconnect. Used by diag only."""
    from .transport.mqtt import MqttTransport
    from .transport.topics import Topics

    transport = MqttTransport(
        config.mqtt,
        Topics(config.mqtt.base_topic, config.device.id),
        announce_availability=False,
    )
    try:
        if transport.connect(timeout=8):
            return True, "connected and authenticated"
        return False, "no CONNACK - check host, credentials, and that Mosquitto is running"
    except Exception as exc:
        return False, str(exc)
    finally:
        try:
            transport.disconnect()
        except Exception:
            pass


def cmd_commands(args: argparse.Namespace) -> int:
    registry = build_registry()
    described = registry.describe()
    if args.json:
        print(json.dumps(described, indent=2))
        return 0
    for entry in described:
        flag = " [destructive]" if entry["destructive"] else ""
        print(f"{entry['name']:<26} {entry['description']}{flag}")
        for name, hint in entry["args"].items():
            print(f"{'':<26}   --arg {name}=<{hint}>")
    return 0


def _round_trip(config, command: str, args: dict, *, speak: bool, timeout: float) -> dict:
    """Publish a signed command to the running agent and wait for its reply.

    Raises RuntimeError if the broker or the agent does not answer.
    """
    import queue

    from .security import build_command
    from .transport.mqtt import MqttTransport
    from .transport.topics import Topics

    topics = Topics(config.mqtt.base_topic, config.device.id)
    # A dedicated reply topic keeps this run's answer separate from the shared
    # result topic, so a concurrent sender's reply is not mistaken for ours.
    reply_topic = f"{topics.result}/cli-{secrets.token_hex(4)}"
    replies: queue.Queue[dict] = queue.Queue()

    transport = MqttTransport(
        config.mqtt,
        topics,
        subscribe_to=reply_topic,
        on_command=lambda topic, payload: replies.put(payload),
        announce_availability=False,
    )

    envelope = build_command(
        config.security.shared_secret,
        command,
        args,
        reply_to=reply_topic,
        speak=speak,
    )

    if not transport.connect(timeout=8):
        raise RuntimeError("could not connect to the broker")
    try:
        transport.publish(topics.command, envelope, qos=1)
        try:
            return replies.get(timeout=timeout)
        except queue.Empty:
            raise RuntimeError("no reply within timeout - is the agent running?") from None
    finally:
        transport.disconnect()


def cmd_send(args: argparse.Namespace) -> int:
    """Publish a signed command over MQTT and wait for the agent's reply.

    This is the round-trip the Pi-side helper performs, exposed locally so the
    whole path can be tested from the PC without involving Jarvis.
    """
    config = load_config(args.config)
    setup_logging(args.log_level or "WARNING", "")

    try:
        reply = _round_trip(
            config,
            args.command,
            _parse_args_pairs(args.arg, args.json, getattr(args, "json_b64", None)),
            speak=args.speak,
            timeout=args.timeout,
        )
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(json.dumps(reply, indent=2, default=str))
    return 0 if reply.get("ok") else 1


def cmd_face(args: argparse.Namespace) -> int:
    """Show the animated face overlay."""
    config = load_config(args.config)
    setup_logging(args.log_level or config.log_level, config.log_file)

    if args.style:
        config.face.style = args.style
    if args.size:
        config.face.size = args.size
    if args.position:
        config.face.position = args.position
    if args.opacity:
        config.face.opacity = args.opacity

    try:
        import tkinter  # noqa: F401
    except ImportError:
        print(
            "tkinter is not available in this Python build. On Windows, reinstall "
            "Python with the 'tcl/tk and IDLE' option ticked.",
            file=sys.stderr,
        )
        return 2

    from .face.app import FaceWindow

    try:
        return FaceWindow(config).run()
    except Exception as exc:  # a GUI failure should explain itself, not traceback
        log.exception("face failed")
        print(f"the face could not start: {exc}", file=sys.stderr)
        return 1


def cmd_ui(args: argparse.Namespace) -> int:
    """Serve the dashboard."""
    config = load_config(args.config)
    setup_logging(args.log_level or config.log_level, config.log_file)

    if args.host:
        config.ui.host = args.host
    if args.port:
        config.ui.port = args.port
    if args.no_open:
        config.ui.open_browser = False

    from .ui.server import serve

    return serve(config)


def cmd_listen(args: argparse.Namespace) -> int:
    """Run the local voice assistant: wake word, transcribe, answer, speak."""
    config = load_config(args.config)
    setup_logging(args.log_level or config.log_level, config.log_file)

    if args.device:
        config.voice.input_device = args.device
    if args.wake_word:
        config.voice.wake_word = args.wake_word
    if args.model:
        config.voice.stt_model = args.model
    if args.brain:
        config.voice.brain = args.brain

    mode = args.mode or config.voice.mode
    try:
        if mode == "realtime":
            from .voice.realtime_runner import RealtimeSetupError, RealtimeVoiceAssistant

            try:
                assistant = RealtimeVoiceAssistant(config)
            except RealtimeSetupError as exc:
                print(str(exc), file=sys.stderr)
                return 2
        else:
            from .voice.pipeline import VoiceAssistant

            assistant = VoiceAssistant(config)
    except ImportError as exc:
        print(
            f"voice support is not installed ({exc}).\n"
            'Run:  uv pip install -e ".[voice,cuda]"',
            file=sys.stderr,
        )
        return 2

    def handle(signum, frame):
        assistant.stop()

    import signal as _signal

    for sig in (_signal.SIGINT, _signal.SIGTERM):
        try:
            _signal.signal(sig, handle)
        except (ValueError, OSError):
            pass

    return assistant.run()


def cmd_mouth_test(args: argparse.Namespace) -> int:
    """Play a WAV at the face, so lip-sync can be tuned without the wake word."""
    from dataclasses import replace
    from pathlib import Path

    from .mouth import TUNING
    from .voice import mouth_test

    config = load_config(args.config)
    setup_logging(args.log_level or "WARNING", "")

    overrides = {
        name: getattr(args, attr)
        for name, attr in (
            ("attack_ms", "attack_ms"),
            ("release_ms", "release_ms"),
            ("open_gain", "open_gain"),
            ("width_gain", "width_gain"),
            ("low_crossover_hz", "low_hz"),
            ("high_crossover_hz", "high_hz"),
        )
        if getattr(args, attr, None) is not None
    }
    tuning = replace(TUNING, **overrides) if overrides else TUNING

    path = Path(args.wav)
    if not path.exists():
        print(f"no such file: {path}", file=sys.stderr)
        return 1
    try:
        return mouth_test.run(
            config, path, play=not args.silent, loop=args.loop, tuning=tuning
        )
    except Exception as exc:
        print(f"mouth test failed: {exc}", file=sys.stderr)
        return 1


def cmd_voice_test(args: argparse.Namespace) -> int:
    """Exercise each voice stage on its own, so failures are easy to place."""
    config = load_config(args.config)
    setup_logging(args.log_level or "WARNING", "")
    voice = config.voice

    print("-- devices --")
    try:
        import sounddevice as sd

        from .voice.audio import resolve_device

        in_dev = resolve_device(voice.input_device, input=True)
        out_dev = resolve_device(voice.output_device, input=False)
        devices = sd.query_devices()
        print(f"  input : {devices[in_dev]['name'] if in_dev is not None else 'system default'}")
        print(f"  output: {devices[out_dev]['name'] if out_dev is not None else 'system default'}")
    except Exception as exc:
        print(f"  FAILED: {exc}")
        return 1

    print("\n-- microphone (speak now, 3 seconds) --")
    import numpy as np

    recording = sd.rec(int(3 * 16000), samplerate=16000, channels=1,
                       dtype="float32", device=in_dev)
    sd.wait()
    peak = float(np.max(np.abs(recording)))
    print(f"  peak level: {peak:.4f}  {'OK' if peak > 0.02 else 'VERY QUIET - check the mic'}")

    print("\n-- transcription --")
    from .voice.stt import Transcriber

    transcriber = Transcriber(voice.stt_model, voice.stt_device, voice.stt_compute_type)
    print(f"  backend: {transcriber.device}/{transcriber.compute_type}")
    started = time.time()
    text = transcriber.transcribe(recording[:, 0])
    print(f"  heard: {text!r}  ({time.time() - started:.2f}s)")

    print("\n-- brain --")
    from .voice.brain import LocalBrain
    from .alerts import AlertEngine
    from .commands import CommandContext
    from .monitors.collector import Collector

    ctx = CommandContext(
        config=config,
        collector=Collector(config.monitors, config.outlook, config.notifications, config.claude),
        alerts=AlertEngine(config.alerts, config.device.friendly_name),
        jarvis=JarvisClient(config.jarvis),
    )
    local = LocalBrain(ctx)
    probe = text or "how much disk space is left"
    matched = local.match(probe)
    print(f"  {probe!r} -> {matched[0] if matched else 'no local match (would go to Jarvis)'}")
    if matched:
        print(f"  answer: {local.ask(probe)}")

    print("\n-- speech --")
    from .voice.tts import Speaker

    speaker = Speaker(voice.piper_voice, voice.piper_dir, out_dev)
    line = text and f"I heard you say: {text}" or "Voice test complete."
    started = time.time()
    speaker.say(line)
    print(f"  spoke in {time.time() - started:.2f}s")
    return 0


def cmd_memory(args: argparse.Namespace) -> int:
    """Read and edit what the assistant remembers, without talking to it."""
    from . import memory as memory_module

    config = load_config(args.config)
    setup_logging(args.log_level or "WARNING", "")
    registry = build_registry()
    ctx = _build_context(config)

    if args.action == "add":
        result = registry.dispatch(
            "memory.remember",
            {"text": " ".join(args.text), "tags": args.tags or "", "source": "cli"},
            ctx,
        )
    elif args.action == "forget":
        result = registry.dispatch("memory.forget", {"query": " ".join(args.text)}, ctx)
    elif args.action == "search":
        result = registry.dispatch("memory.recall", {"query": " ".join(args.text)}, ctx)
    else:
        result = registry.dispatch("memory.list", {"limit": args.limit}, ctx)

    if not result.ok:
        print(result.error, file=sys.stderr)
        return 1

    facts = result.result.get("facts") or result.result.get("forgotten") or []
    if args.action == "add":
        facts = [result.result["remembered"]]
    for fact in facts:
        tags = f"  [{', '.join(fact['tags'])}]" if fact.get("tags") else ""
        print(f"{fact['id']}  {fact['text']}{tags}  ({fact['age']})")
    if not facts:
        print(result.speech)
    else:
        print(
            f"\n{memory_module.store_for(config).count()} remembered, in "
            f"{memory_module.resolve(config, config.memory.file)}"
        )
    return 0


def cmd_mail(args: argparse.Namespace) -> int:
    """The inbox sign-in behind the to-do list's weekly document."""
    from .graph_mail import MailUnavailable, shared
    from .todos import TodoSync

    config = load_config(args.config)
    setup_logging(args.log_level or "WARNING", "")
    if not config.todo.enabled or not config.todo.mail.enabled:
        print("reading the inbox is switched off: see todo.enabled and todo.mail.enabled", file=sys.stderr)
        return 2
    mail = shared(config.todo.mail)

    try:
        if args.action == "login":
            if mail.signed_in():
                print(f"already signed in as {(mail.account() or {}).get('username', '?')}")
                return 0
            info = mail.begin_login()
            print(info.get("message") or f"Go to {info['verification_uri']} and enter {info['user_code']}")
            print("waiting for the browser...")
            if mail.login_blocking():
                print(f"signed in as {(mail.account() or {}).get('username', '?')}")
                print(f"token cache: {mail.path}")
                return 0
            print(f"sign-in failed: {mail.status().get('error') or 'no answer'}", file=sys.stderr)
            return 1
        if args.action == "logout":
            mail.logout()
            print("forgotten")
            return 0
        if args.action == "check":
            sync = TodoSync(config)
            report = sync.sync(force=True)
            status = sync.status()
            inbox = status.get("mail") or {}
            if report is None:
                print("nothing new" if not inbox.get("error") else f"inbox: {inbox['error']}")
            elif report.skipped:
                print(f"already have {report.file}")
            else:
                print(f"took {report.total} item(s) from {report.file}: {report.added} new, "
                      f"{report.kept} kept, {report.dropped} dropped")
            return 0
    except MailUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return 1

    status = mail.status()
    print(f"client id : {status['client_id']}")
    print(f"token file: {mail.path}")
    if not status["available"]:
        print(f"unavailable: {status['error']}")
        return 1
    if status["pending"]:
        print(f"sign-in waiting: go to {status['pending']['verification_uri']} and enter {status['pending']['user_code']}")
    print(f"signed in : {'yes, as ' + status['account'] if status['signed_in'] else 'no'}")
    if status["last_message"]:
        last = status["last_message"]
        print(f"newest    : {last.get('subject')!r} from {last.get('from')}")
    if status["error"]:
        print(f"last error: {status['error']}")
    return 0


def cmd_claude(args: argparse.Namespace) -> int:
    """The Claude Code sessions open here, and the hooks that report on them."""
    from pathlib import Path

    from . import claude_hooks

    config = load_config(args.config)
    setup_logging(args.log_level or "WARNING", "")

    if args.action == "hooks":
        what = (args.words[0] if args.words else "status").lower()
        events = Path(config.claude.events_file)
        if what == "install":
            outcome = claude_hooks.install(events)
            verb = "installed" if outcome["changed"] else "already installed"
            print(f"hooks {verb} in {outcome['path']}")
            print(f"  on: {', '.join(outcome['events'])}")
            print(f"  run: {outcome['command']}")
            print("Sessions started from now on report in; ones already open do not until restarted.")
        elif what in ("remove", "uninstall"):
            outcome = claude_hooks.remove()
            if outcome["removed"]:
                print(f"removed from {outcome['path']}: {', '.join(outcome['removed'])}")
            else:
                print(f"nothing of ours in {outcome['path']}")
        else:
            outcome = claude_hooks.status()
            if outcome.get("error"):
                print(outcome["error"], file=sys.stderr)
                return 1
            if outcome["installed"]:
                print(f"installed in {outcome['path']} on: {', '.join(outcome['events'])}")
                if outcome["missing"]:
                    print(f"  missing: {', '.join(outcome['missing'])} - run `claude hooks install` again")
                print(f"  events land in {events}")
            else:
                print(f"not installed in {outcome['path']}; run `arnold claude hooks install`")
        return 0

    registry = build_registry()
    ctx = _build_context(config)
    words = " ".join(args.words)
    if args.action == "status":
        result = registry.dispatch("claude.status", {"which": words}, ctx)
    elif args.action == "prompt":
        if not args.which:
            print("prompt needs --which <session> and the words to send", file=sys.stderr)
            return 2
        result = registry.dispatch("claude.prompt", {"which": args.which, "text": words}, ctx)
    elif args.action == "apps":
        result = registry.dispatch("claude.apps", {}, ctx)
    elif args.action == "open":
        result = registry.dispatch("claude.open", {"which": words}, ctx)
    else:
        result = registry.dispatch("claude.list", {}, ctx)

    if args.json:
        print(json.dumps(result.to_dict(), indent=2, default=str))
        return 0 if result.ok else 1
    if not result.ok:
        print(result.error, file=sys.stderr)
        return 1

    if args.action in ("list", None):
        for row in result.result.get("sessions") or []:
            state = row["turn"] + (f" ({row['waiting_for']})" if row.get("waiting_for") else "")
            live = "live" if row["live"] else "not running"
            print(f"{row['name']}  [{row['where']}, {live}]  {state}")
            if row.get("activity"):
                print(f"    doing: {row['activity']}")
            if row.get("last_prompt"):
                print(f"    asked: {row['last_prompt']}")
            for app in row.get("apps") or []:
                print(f"    app:   {app['url']}  {app['status']}  {app.get('title') or app.get('process')}")
        if not result.result.get("sessions"):
            print(result.speech)
    else:
        print(result.speech)
    return 0


def cmd_profile(args: argparse.Namespace) -> int:
    """Which assistant this PC is; `use` switches and running processes follow."""
    config = load_config(args.config)
    setup_logging(args.log_level or "WARNING", "")
    registry = build_registry()
    ctx = _build_context(config)

    if args.action == "use":
        if not args.name:
            print("profile use needs a name; `arnold profile` lists them", file=sys.stderr)
            return 2
        result = registry.dispatch("profile.use", {"name": args.name}, ctx)
        if not result.ok:
            print(result.error, file=sys.stderr)
            return 1
        print(result.speech)
        print(f"({config.source_path}: assistant.profile is now {config.active_profile})")
        return 0

    if args.action == "show":
        result = registry.dispatch("profile.show", {}, ctx)
        if not result.ok:
            print(result.error, file=sys.stderr)
            return 1
        for key, value in result.result.items():
            print(f"{key:<14} {value}")
        return 0

    result = registry.dispatch("profile.list", {}, ctx)
    if not result.ok:
        print(result.error, file=sys.stderr)
        return 1
    for entry in result.result["profiles"]:
        marker = "*" if entry["active"] else " "
        origin = "built-in" if entry["builtin"] else "config"
        fields = ", ".join(
            f"{key}={entry[key]}"
            for key in ("voice", "wake_word", "palette", "design")
            if entry.get(key) is not None
        )
        print(f"{marker} {entry['name']:<12} {entry['display_name'] or '':<10} {origin:<9} {fields}")
    current = result.result["current"]
    if not current["profile"]:
        print(f"\n(no profile active; the flat assistant fields apply: {current['name']})")
    return 0


def cmd_wake(args: argparse.Namespace) -> int:
    """Wake-word models: list, install, test, train."""
    config = load_config(args.config)
    setup_logging(args.log_level or "WARNING", "")
    try:
        from .voice import wake_tools
        from .voice.audio import AudioError
    except ImportError as exc:
        print(
            f"voice support is not installed ({exc}).\n"
            'Run:  uv pip install -e ".[voice,cuda]"',
            file=sys.stderr,
        )
        return 2

    words = list(args.phrase or [])
    try:
        if args.action == "install":
            if not words:
                print("wake install needs the path to an .onnx file", file=sys.stderr)
                return 2
            dest = wake_tools.install_model(
                config, " ".join(words), args.as_name, force=args.force
            )
            print(f"installed {dest}")
            print(f"Set voice.wake_word: {dest.stem} (or wake_word: {dest.stem} in a profile).")
            print(f"Try it:  arnold wake test {dest.stem}")
            return 0

        if args.action == "test":
            hits = wake_tools.test_live(config, words[0] if words else None, seconds=args.seconds)
            return 0 if hits else 1

        if args.action == "train":
            phrase = " ".join(words).strip()
            if not phrase:
                print('wake train needs the phrase, e.g.  wake train "hey athena"', file=sys.stderr)
                return 2
            missing = wake_tools.missing_training_deps()
            if missing:
                print(wake_tools.dependency_help(missing, phrase))
                return 2
            layout = wake_tools.TrainingLayout(
                args.data_dir or (wake_tools.wake_dir(config) / "train")
            )
            absent = layout.missing()
            if absent:
                print(layout.describe_missing(absent, phrase))
                return 2
            stages = tuple(s.strip() for s in (args.stage or "").split(",") if s.strip()) or wake_tools.STAGES
            name = args.as_name or args.name
            model = wake_tools.train(
                config,
                phrase,
                name,
                steps=args.steps,
                samples=args.samples,
                data_dir=layout.data_dir,
                stages=stages,
            )
            dest = wake_tools.install_model(config, model, name, force=True)
            print(f"\ntrained and installed {dest}")
            print(f"Set voice.wake_word: {dest.stem} (or wake_word: {dest.stem} in a profile).")
            print(f"Try it:  arnold wake test {dest.stem}")
            return 0

        # list
        entries = wake_tools.list_models(config)
        for entry in entries:
            marker = "*" if entry["active"] else " "
            where = entry["path"] or "(bundled with openWakeWord)"
            print(f"{marker} {entry['name']:<20} {entry['kind']:<8} {where}")
        folder = wake_tools.wake_dir(config)
        print(f"\ncustom models go in {folder}; active: voice.wake_word = {config.voice.wake_word!r}")
        return 0
    except (wake_tools.WakeToolError, AudioError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


def cmd_game(args: argparse.Namespace) -> int:
    """Get out of the way of a game: stop every task and boost the PC, then
    put it all back after. `watch` does both on its own when a game starts."""
    from . import booster

    config = load_config(args.config)
    game = config.game

    if args.action == "scaling":
        from . import lossless

        exe = lossless.find_exe(game.scaling_exe)
        if args.install or args.remove:
            if args.install and exe is None:
                print("Lossless Scaling is not installed where this can find it; "
                      "set game.scaling_exe", file=sys.stderr)
                return 1
            print("approve the UAC prompt to " + ("remove" if args.remove else "register")
                  + f" the elevated {lossless.TASK} task...")
            error = lossless.remove_task() if args.remove else lossless.install_task(exe)
            if error:
                print(error, file=sys.stderr)
                return 1
            print(f"{lossless.TASK} " + ("removed" if args.remove else
                  "registered: games start Lossless Scaling without a UAC prompt"))
            return 0
        settings = lossless.read_settings()
        print(f"auto-scaling   {'on' if game.scaling else 'off'} (game.scaling)")
        print(f"installed      {exe or 'not found'}")
        print(f"running        {'yes' if lossless.running() else 'no'}")
        print(f"elevated task  {'yes' if lossless.task_installed() else 'no - `arnold game scaling --install`'}")
        if settings:
            print(f"hotkey         {settings.hotkey_text}"
                  + ("" if settings.key else " (not one this can press)"))
            for path in sorted(settings.auto_paths):
                print(f"auto profile   {path}")
        return 0

    if args.action == "watch":
        if args.install or args.remove:
            error = booster.remove_watch() if args.remove else booster.install_watch(config)
            if error:
                print(error, file=sys.stderr)
                return 1
            print(f"{booster.WATCH_TASK} " + ("removed" if args.remove else
                  "installed and started: games are boosted as they launch"))
            return 0
        from pathlib import Path

        setup_logging(args.log_level or config.log_level,
                      str(Path(config.state_file).with_name("game-watch.log")))
        return booster.watch(config)

    if args.action == "status":
        states = booster.task_states(booster.TASKS + (booster.WATCH_TASK,))
        for name in booster.TASKS + (booster.WATCH_TASK,):
            print(f"{name:16} {states.get(name, 'not installed')}")
        saved = booster.read_boost(config)
        print()
        if saved:
            since = time.strftime("%H:%M", time.localtime(saved.get("since", 0)))
            print(f"boosted since {since} ({saved.get('by', 'manual')}) - `arnold game off` to undo")
        else:
            running = any(states.get(t) == "Running" for t in booster.TASKS)
            print("game mode is " + ("off" if running else "on") + " - `arnold game "
                  + ("on` to stop everything" if running else "off` to bring it back"))
        return 0

    if args.action == "on":
        if game.stop_tasks:
            error = booster.switch_tasks(stop=True)
            if error:
                print(error, file=sys.stderr)
                return 1
        done = []
        if game.boost:
            playing = booster.running_games(game)
            done = booster.apply(config, playing[0][0] if playing else None)
        print("game mode on: " + ("the agent, face and voice are stopped. " if game.stop_tasks else ""))
        for line in done:
            print(f"  {line}")
        print("`arnold game off` when done.")
        return 0

    done = booster.restore(config)
    if game.stop_tasks:
        error = booster.switch_tasks(stop=False)
        if error:
            print(error, file=sys.stderr)
            return 1
    print("game mode off: " + ("the agent, face and voice are starting again." if game.stop_tasks else ""))
    for line in done:
        print(f"  {line}")
    return 0


def cmd_printer(args: argparse.Namespace) -> int:
    """The Centauri Carbon 2: find it, check the access code, or ask the
    agent how the print is going."""
    import json as _json

    from .monitors import printer as printer_mod

    config = load_config(args.config)
    if args.action == "discover":
        found = printer_mod.discover(config.printer.host, timeout=4.0)
        if not found:
            print("no Centauri Carbon 2 answered (is it on, and on this network?)")
            return 1
        for p in found:
            print(f"{p['name'] or p['model']}  {p['host']}  sn {p['sn']}"
                  + ("  (needs printer.access_code)" if p["needs_code"] else ""))
        return 0

    if args.action == "local":
        # The local 3D generator: is every piece where sculpting expects it?
        from . import sculpt
        from .commands.printer import local_generator

        local = local_generator(_build_context(config))
        weights = local.weights / local.model / local.subfolder / "model.fp16.safetensors"
        for label, ok, where in (
            ("python", local.python.is_file(), local.python),
            ("Hunyuan3D-2 code", (local.repo / "hy3dgen").is_dir(), local.repo),
            ("weights", weights.is_file(), weights),
        ):
            print(f"{'ok ' if ok else 'MISSING'}  {label:18} {where}")
        using = sculpt.where(config.printer.sculpt_backend, local)
        print(f"sculpt_backend is {config.printer.sculpt_backend}: shapes are made "
              + {"local": "on this PC", "tencent": "on Tencent Cloud"}.get(using, "on Hugging Face"))
        return 0 if local.ready else 1

    if args.action == "test":
        # A connection of our own, separate from the agent's, to prove the code.
        cfg = config.printer
        if args.code:
            cfg.access_code = args.code
        cfg.enabled = True
        watch = printer_mod.PrinterWatch(cfg)
        watch.start()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            status = watch.status()
            if status.get("state") or status.get("error"):
                break
            time.sleep(0.5)
        watch.stop()
        status = watch.status()
        print(_json.dumps(status, indent=2))
        return 0 if status.get("state") else 1

    from .commands.printer import describe

    from .state import read_state

    state = read_state(Path(config.state_file))
    printer = (state or {}).get("printer")
    if not config.printer.enabled:
        print("printer.enabled is false - `arnold printer discover`, then set the printer section")
        return 1
    if not printer:
        print("the agent is not running, or has not reported on the printer yet")
        return 1
    print(describe(printer))
    return 0


def cmd_gen_secret(args: argparse.Namespace) -> int:
    from .security import generate_secret as gen

    secret = gen()
    print(secret)
    print(
        "\nPut this in config.yaml under security.shared_secret on BOTH this PC "
        "and the Pi-side helper.",
        file=sys.stderr,
    )
    return 0


def cmd_discovery(args: argparse.Namespace) -> int:
    from .transport.discovery import clear_discovery, publish_discovery
    from .transport.mqtt import MqttTransport
    from .transport.topics import Topics

    config = load_config(args.config)
    setup_logging(args.log_level or "INFO", "")
    topics = Topics(config.mqtt.base_topic, config.device.id)
    snapshot = Collector(config.monitors, config.outlook, config.notifications, config.claude).snapshot()

    transport = MqttTransport(config.mqtt, topics, announce_availability=False)
    if not transport.connect(timeout=8):
        print("could not connect to the broker", file=sys.stderr)
        return 1
    try:
        if args.clear:
            clear_discovery(transport, config, topics, snapshot)
            print("cleared Home Assistant discovery configs")
        else:
            count = publish_discovery(transport, config, topics, snapshot)
            print(f"published {count} discovery configs")
        time.sleep(1)  # let QoS 1 publishes drain before disconnecting
    finally:
        transport.disconnect()
    return 0


# -- parser ------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="arnold",
        description="Monitor this PC and bridge it to a Jarvis voice assistant.",
    )
    parser.add_argument("--version", action="version", version=f"arnold {__version__}")
    parser.add_argument("-c", "--config", help="path to config.yaml")
    parser.add_argument("--log-level", help="DEBUG, INFO, WARNING, ERROR")

    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    run = subparsers.add_parser("run", help="run the agent (telemetry, alerts, command listener)")
    run.add_argument("--force", action="store_true", help="start even if the config has problems")
    run.set_defaults(func=cmd_run)

    execute = subparsers.add_parser("exec", help="run one command locally and print JSON")
    execute.add_argument("command", help="e.g. query.system")
    execute.add_argument("--arg", action="append", help="key=value, repeatable")
    execute.add_argument("--json", help="arguments as a JSON object")
    execute.add_argument(
        "--json-b64",
        dest="json_b64",
        help="arguments as base64-encoded JSON; survives remote shells intact",
    )
    execute.add_argument("--speak", action="store_true", help="also send the reply to Jarvis")
    execute.add_argument("--pretty", action="store_true", help="indent the JSON output")
    execute.set_defaults(func=cmd_exec)

    say = subparsers.add_parser("say", help="say something out loud (the Pi, here, or both)")
    say.add_argument("text", nargs="+")
    say.add_argument(
        "--route",
        choices=["jarvis", "local", "both", "auto"],
        help="where to say it; default is speech.route from the config",
    )
    say.add_argument("--local", action="store_true", help="shorthand for --route local")
    say.add_argument("--ring", action="store_true", help="play the timer sound first")
    say.set_defaults(func=cmd_say)

    diag = subparsers.add_parser("diag", help="check config, connectivity, and print a snapshot")
    diag.add_argument("--json", action="store_true", help="dump the raw snapshot as JSON")
    diag.set_defaults(func=cmd_diag)

    commands = subparsers.add_parser("commands", help="list every supported command")
    commands.add_argument("--json", action="store_true")
    commands.set_defaults(func=cmd_commands)

    send = subparsers.add_parser("send", help="publish a signed command over MQTT and await reply")
    send.add_argument("command")
    send.add_argument("--arg", action="append")
    send.add_argument("--json")
    send.add_argument("--json-b64", dest="json_b64")
    send.add_argument("--speak", action="store_true")
    send.add_argument("--timeout", type=float, default=15.0)
    send.set_defaults(func=cmd_send)

    face = subparsers.add_parser("face", help="show the animated face overlay")
    face.add_argument(
        "--style",
        choices=("holo", "orb"),
        help="holo = the J.A.R.V.I.S. core (default), orb = the cartoon face",
    )
    face.add_argument("--size", type=int, help="width/height in pixels")
    face.add_argument(
        "--position", help="bottom-right, top-left, center, ... or explicit 'x,y'"
    )
    face.add_argument("--opacity", type=float, help="0.1 to 1.0")
    face.set_defaults(func=cmd_face)

    ui = subparsers.add_parser("ui", help="serve the dashboard in a browser")
    ui.add_argument("--host", help="bind address; anything but loopback needs ui.token")
    ui.add_argument("--port", type=int, help="default 8770")
    ui.add_argument("--no-open", action="store_true", help="do not open a browser")
    ui.set_defaults(func=cmd_ui)

    mouth = subparsers.add_parser(
        "mouth-test", help="drive the face's mouth from a WAV file, for tuning"
    )
    mouth.add_argument("wav", help="path to a 16-bit WAV file")
    mouth.add_argument("--silent", action="store_true", help="animate without playing it")
    mouth.add_argument("--loop", type=int, default=1, help="repeat N times")
    mouth.add_argument("--attack-ms", type=float, help="override attack_ms")
    mouth.add_argument("--release-ms", type=float, help="override release_ms")
    mouth.add_argument("--open-gain", type=float, help="override open_gain")
    mouth.add_argument("--width-gain", type=float, help="override width_gain")
    mouth.add_argument("--low-hz", type=float, help="override low_crossover_hz")
    mouth.add_argument("--high-hz", type=float, help="override high_crossover_hz")
    mouth.set_defaults(func=cmd_mouth_test)

    listen = subparsers.add_parser("listen", help="run the local voice assistant")
    listen.add_argument("--device", help="microphone name (substring match)")
    listen.add_argument(
        "--wake-word",
        help="bundled (hey_mycroft, hey_jarvis, alexa, hey_rhasspy), a name in "
        "voice.wake_word_dir, or a path to an .onnx",
    )
    listen.add_argument("--model", help="whisper model, e.g. small.en")
    listen.add_argument("--brain", choices=["jarvis", "local"], help="who answers")
    listen.add_argument(
        "--mode",
        choices=["realtime", "pipeline"],
        help="realtime talks to OpenAI directly with the Pi's voice and prompt; "
        "pipeline transcribes locally, then answers",
    )
    listen.set_defaults(func=cmd_listen)

    voice_test = subparsers.add_parser(
        "voice-test", help="check the mic, transcription, intents and speech in turn"
    )
    voice_test.set_defaults(func=cmd_voice_test)

    remember = subparsers.add_parser("memory", help="inspect or edit what the assistant remembers")
    remember.add_argument(
        "action", nargs="?", default="list", choices=["list", "add", "search", "forget"]
    )
    remember.add_argument("text", nargs="*", help="the fact, or what to look for")
    remember.add_argument("--tags", help="comma-separated labels, for add")
    remember.add_argument("--limit", type=int, default=50)
    remember.set_defaults(func=cmd_memory)

    mail = subparsers.add_parser(
        "mail", help="the inbox sign-in for the to-do list's weekly document"
    )
    mail.add_argument(
        "action", nargs="?", default="status", choices=["status", "login", "logout", "check"]
    )
    mail.set_defaults(func=cmd_mail)

    claude = subparsers.add_parser(
        "claude", help="the Claude Code sessions open here: list, status, prompt, apps, hooks"
    )
    claude.add_argument(
        "action", nargs="?", default="list",
        choices=["list", "status", "prompt", "apps", "open", "hooks"],
    )
    claude.add_argument(
        "words", nargs="*",
        help="status/open: which session; prompt: what to say; hooks: install, remove or status",
    )
    claude.add_argument("--which", help="prompt: the session to speak to")
    claude.add_argument("--json", action="store_true", help="print the raw result")
    claude.set_defaults(func=cmd_claude)

    profile = subparsers.add_parser("profile", help="which assistant this PC is; switch with `use`")
    profile.add_argument("action", nargs="?", default="list", choices=["list", "show", "use"])
    profile.add_argument("name", nargs="?", help="the profile, for use")
    profile.set_defaults(func=cmd_profile)

    wake = subparsers.add_parser("wake", help="wake-word models: list, install, test, train")
    wake.add_argument(
        "action", nargs="?", default="list", choices=["list", "install", "test", "train"]
    )
    wake.add_argument(
        "phrase", nargs="*",
        help="install: the .onnx file; test: a model name (optional); train: the phrase",
    )
    wake.add_argument("--as", dest="as_name", help="install/train: the model name to save as")
    wake.add_argument("--force", action="store_true", help="install: replace an existing model")
    wake.add_argument("--seconds", type=float, default=20.0, help="test: how long to listen")
    wake.add_argument("--name", help="train: the model name (default: from the phrase)")
    wake.add_argument("--steps", type=int, default=10000, help="train: training steps")
    wake.add_argument("--samples", type=int, default=1000, help="train: synthetic clips per class")
    wake.add_argument("--data-dir", dest="data_dir", help="train: where the assets live")
    wake.add_argument("--stage", help="train: comma-separated subset of generate,augment,train")
    wake.set_defaults(func=cmd_wake)

    game = subparsers.add_parser(
        "game", help="stop everything and boost the PC while you play (`on`), undo it (`off`), or `watch` for game launches"
    )
    game.add_argument("action", nargs="?", choices=("on", "off", "status", "watch", "scaling"),
                      default="status")
    game.add_argument("--install", action="store_true",
                      help="watch: register the logon task that boosts games as they launch; "
                           "scaling: register the elevated task that starts Lossless Scaling")
    game.add_argument("--remove", action="store_true", help="watch/scaling: remove that task")
    game.set_defaults(func=cmd_game)

    printer = subparsers.add_parser(
        "printer", help="the Elegoo printer: `discover` it, `test` the access code, or `status`"
    )
    printer.add_argument("action", nargs="?", choices=("status", "discover", "test", "local"), default="status")
    printer.add_argument("--code", default="", help="test: try this access code instead of the config's")
    printer.set_defaults(func=cmd_printer)

    secret = subparsers.add_parser("gen-secret", help="generate a shared secret for command signing")
    secret.set_defaults(func=cmd_gen_secret)

    discovery = subparsers.add_parser("discovery", help="publish or clear HA discovery configs")
    discovery.add_argument("--clear", action="store_true", help="remove the device from HA")
    discovery.set_defaults(func=cmd_discovery)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except RuleError as exc:
        print(f"alert rule error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
