"""Configuration loading.

Config is YAML on disk. Any string value may reference an environment variable
as ``${VAR}`` or ``${VAR:default}`` so secrets (MQTT password, HA token, HMAC
shared secret) can stay out of the file.
"""

from __future__ import annotations

import os
import re
import socket
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::([^}]*))?\}")

CONFIG_ENV_VAR = "ARNOLD_CONFIG"


def _install_root() -> Path | None:
    """Project directory for a virtualenv install: <root>/.venv/Scripts/exe."""
    try:
        exe = Path(sys.executable).resolve()
    except OSError:
        return None
    # .../<root>/.venv/Scripts/python.exe -> parents[2] is <root>
    return exe.parents[2] if len(exe.parents) >= 3 else None


def default_config_paths() -> list[Path]:
    """Where to look for config.yaml, in order.

    The working directory is not enough on its own: Jarvis invokes the agent
    over SSH, and those sessions start in the user's home directory, not the
    project. So the install root is searched too.
    """
    candidates: list[Path] = []

    from_env = os.environ.get(CONFIG_ENV_VAR)
    if from_env:
        candidates.append(Path(from_env))

    candidates.append(Path("config.yaml"))

    root = _install_root()
    if root is not None:
        candidates.append(root / "config.yaml")

    candidates.append(Path.home() / ".config" / "arnold" / "config.yaml")
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates.append(Path(appdata) / "arnold" / "config.yaml")

    # Preserve order while dropping duplicates.
    seen: set[str] = set()
    unique: list[Path] = []
    for path in candidates:
        key = str(path).lower()
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


class ConfigError(RuntimeError):
    pass


def _expand(value: Any) -> Any:
    """Recursively expand ``${VAR}`` / ``${VAR:default}`` in strings."""
    if isinstance(value, str):

        def sub(m: re.Match[str]) -> str:
            name, default = m.group(1), m.group(2)
            env = os.environ.get(name)
            if env is not None:
                return env
            if default is not None:
                return default
            raise ConfigError(
                f"config references environment variable ${{{name}}} which is not set "
                f"(use ${{{name}:some-default}} to make it optional)"
            )

        return _ENV_RE.sub(sub, value)
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", text.lower()).strip("_")


@dataclass(slots=True)
class ProfileConfig:
    """One named identity: everything that makes the assistant *someone*.

    Every field is optional. `None` means "leave the flat setting alone", so
    a profile can change just the voice, or just the wake word. An empty
    string is a value in its own right for `persona` and `delivery` (it means
    "use the built-in one"), which is why the sentinel is `None`.
    """

    name: str | None = None
    # How the name is written on screen, and what it stands for.
    title: str | None = None
    motto: str | None = None
    voice: str | None = None
    persona: str | None = None
    delivery: str | None = None
    mirror_jarvis: bool | None = None
    # voice.wake_word: a bundled model, a name under voice.wake_word_dir,
    # or a path to an .onnx.
    wake_word: str | None = None
    # face.palette / face.design.
    palette: str | None = None
    design: str | None = None


# Ship three. `arnold` is the project's own identity and the default;
# `mycroft` is the earlier one, kept because its wake word ships with
# openWakeWord; `jarvis` is the escape hatch that mirror_jarvis always was. A
# profile of the same name in config.yaml is merged over it key by key.
BUILTIN_PROFILES: dict[str, dict[str, Any]] = {
    "arnold": {
        "name": "Arnold",
        "title": "A.R.N.O.L.D.",
        "motto": "A Rather Nice, Ordinary, Loyal Daemon",
        # A warm, well-spoken male voice; the cheek is in the persona and
        # delivery (voice/session_config.py). Jarvis keeps cedar.
        "voice": "ballad",
        "mirror_jarvis": False,
        "wake_word": "hey_arnold",
        "palette": "steel",
        "design": "lattice",
    },
    "mycroft": {
        "name": "Mycroft",
        "voice": "marin",
        "mirror_jarvis": False,
        "wake_word": "hey_mycroft",
        "palette": "steel",
        "design": "lattice",
    },
    "jarvis": {
        "name": "Jarvis",
        "voice": "cedar",
        "mirror_jarvis": True,
        "wake_word": "hey_jarvis",
        "palette": "gold",
        "design": "core",
    },
}


@dataclass(slots=True)
class AssistantConfig:
    """Who the assistant on this PC is.

    By default it is its own character - a different name, wake word, voice
    and colour from Jarvis on the Pi - so you always know which of the two
    you are talking to, and each can hand things to the other by name.

    The flat fields are the identity. `profile` names one of `profiles` (or a
    built-in) whose set fields are laid over them when the config is loaded,
    so switching identities is one line - and `arnold profile use`
    edits exactly that line.
    """

    name: str = "Arnold"
    # How the name is written on screen (blank = `name`) and, under it, what
    # it stands for. Display only: speech, the persona and the topics use `name`.
    title: str = ""
    motto: str = ""
    # Realtime voice: alloy, ash, ballad, coral, echo, sage, shimmer, verse,
    # marin or cedar. Jarvis speaks as cedar, so anything else tells them apart.
    voice: str = "marin"
    # Personality prompt. Blank uses the built-in one, written for `name`.
    persona: str = ""
    # How the spoken acknowledgement is delivered (an instruction to the TTS
    # model). Blank uses a built-in line that matches the persona.
    delivery: str = ""
    # true = be Jarvis: fetch his voice and personality from the Pi and share
    # his colours and topics, which is how this worked before it had a name
    # of its own. The wake word is still voice.wake_word.
    mirror_jarvis: bool = False
    # Which named identity is active. Blank = the flat fields above, as is.
    profile: str = ""
    # Named identities, merged over BUILTIN_PROFILES. Raw mappings here; they
    # are checked against ProfileConfig when read (Config.profiles()).
    profiles: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def slug(self) -> str:
        return _slug(self.name) or "assistant"


@dataclass(slots=True)
class DeviceConfig:
    id: str = ""
    friendly_name: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            self.id = _slug(socket.gethostname())
        else:
            self.id = _slug(self.id)
        if not self.friendly_name:
            self.friendly_name = socket.gethostname()


@dataclass(slots=True)
class MqttConfig:
    enabled: bool = True
    host: str = "192.168.1.171"
    port: int = 1883
    username: str = ""
    password: str = ""
    client_id: str = ""
    keepalive: int = 45
    tls: bool = False
    base_topic: str = "computer_assistant"
    discovery_prefix: str = "homeassistant"
    enable_discovery: bool = True
    qos: int = 1


@dataclass(slots=True)
class HomeAssistantConfig:
    url: str = "http://192.168.1.171:8123"
    token: str = ""
    # Name of the HA `rest_command:` that relays text to Jarvis's loopback /say.
    # See pi/homeassistant/configuration.snippet.yaml for the definition.
    say_service: str = "rest_command/jarvis_say"
    message_field: str = "message"
    timeout_seconds: float = 10.0


@dataclass(slots=True)
class SshConfig:
    host: str = "192.168.1.171"
    user: str = "parzival"
    port: int = 22
    key_path: str = ""
    hud_port: int = 8765
    timeout_seconds: float = 15.0


@dataclass(slots=True)
class JarvisConfig:
    # home_assistant -> POST HA REST API (recommended: LAN-native, authenticated)
    # ssh            -> ssh to the Pi and curl its loopback :8765/say
    # none           -> speech disabled, telemetry/alerts only
    speech_route: str = "home_assistant"
    home_assistant: HomeAssistantConfig = field(default_factory=HomeAssistantConfig)
    ssh: SshConfig = field(default_factory=SshConfig)
    # Consume Jarvis's SSE state stream (requires an SSH tunnel to :8765).
    observe_events: bool = False


@dataclass(slots=True)
class SecurityConfig:
    shared_secret: str = ""
    require_signature: bool = True
    max_clock_skew_seconds: int = 120
    # Gate for shutdown / restart / sleep / logoff. Off by default on purpose.
    allow_destructive: bool = False
    # Absolute paths only; anything not listed cannot be launched.
    launch_allowlist: dict[str, str] = field(default_factory=dict)
    script_allowlist: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class TelemetryConfig:
    interval_seconds: float = 10.0
    retain: bool = True
    publish_active_window: bool = True


@dataclass(slots=True)
class MonitorsConfig:
    disks: list[str] = field(default_factory=list)
    watch_processes: list[str] = field(default_factory=list)
    gpu: bool = True
    top_processes: int = 5
    net_interfaces: list[str] = field(default_factory=list)


@dataclass(slots=True)
class VoiceConfig:
    enabled: bool = False
    # realtime - stream audio to OpenAI's Realtime API using the same model,
    #            voice and prompt as the Pi, so both sound like one assistant.
    # pipeline - transcribe locally with Whisper, then answer. Cheaper and
    #            works offline for PC questions, but plainer.
    mode: str = "realtime"

    # Audio devices. None/empty means the system default. Names are matched as
    # case-insensitive substrings, which survives device index reshuffling.
    input_device: str = ""
    output_device: str = ""

    # Wake word. openWakeWord ships a pretrained "hey_jarvis" model. This is
    # a bundled name (hey_jarvis, hey_mycroft, alexa, hey_rhasspy), the name
    # of an .onnx under wake_word_dir, or a path to one.
    wake_word: str = "hey_jarvis"
    # Where `arnold wake install` and `wake train` put custom
    # models. Relative to the config file.
    wake_word_dir: str = "models/wake"
    # Score the model has to reach. 0.5 is openWakeWord's own default, and a
    # single frame of ordinary speech clears it more often than you would
    # think - which is a wake word that fires on nobody saying it.
    wake_threshold: float = 0.5
    # Consecutive 80 ms frames that have to sit at or above the threshold
    # before it counts. 1 is the raw model. A real "hey jarvis" holds the
    # score up for several frames; a fluke holds it for one, so 2 drops most
    # false triggers for 80 ms of extra latency.
    wake_patience: int = 2
    # Voice-activity gate: somebody has to have been speaking in the half
    # second before the frame, or its score is thrown away. Music, keyboards
    # and door slams never get a vote. 0 turns the gate off.
    wake_vad_threshold: float = 0.5
    # Ignore repeat triggers within this window, so one utterance fires once.
    wake_cooldown_seconds: float = 2.0

    # Answer the wake word out loud, so you know it is listening before you
    # start talking. speech - a short spoken phrase in the assistant's own
    # voice; chime - a locally generated two-note tone; off - nothing.
    # Anything said over the acknowledgement is discarded: wait for it before
    # giving the command.
    wake_ack: str = "speech"
    wake_ack_phrases: list[str] = field(
        default_factory=lambda: ["Yes?", "Listening.", "Sir?", "At your service.", "Go ahead."]
    )
    # Rendered once and cached here, so the reply is instant rather than a
    # round trip to the TTS API every time the wake word fires.
    wake_ack_cache_dir: str = "models/ack"
    wake_ack_volume: float = 0.7

    # -- Turn-taking (realtime mode) ---------------------------------------
    # How it decides you have finished a sentence.
    #   semantic - a model reads the words and waits when you are mid-thought,
    #              which is what stops it cutting in on a pause for breath.
    #   server   - plain silence detection: fast, but a pause is the end of
    #              your turn whether you meant it or not.
    #   pi       - whatever Jarvis reports from the Pi.
    turn_detection: str = "semantic"
    # semantic only. low waits longest before deciding you are done (up to 8s),
    # high answers soonest (2s). low is the setting for people who think out
    # loud; high suits short commands.
    turn_eagerness: str = "low"
    # server only. 500ms is the API default and interrupts constantly.
    turn_silence_ms: int = 900
    turn_threshold: float = 0.55
    turn_prefix_padding_ms: int = 300

    # Barge-in: start talking over a reply and it stops, the way a person
    # would. With this off the microphone is dead for the whole reply, so
    # talking over it is not an interruption - it is simply unheard.
    barge_in: bool = True
    # Mic RMS (0-1) that counts as you cutting in. It has to sit above the
    # assistant's own voice coming back through the microphone, or it will
    # interrupt itself; the level actually measured is logged at the end of
    # every conversation, so there is a real number to set this against.
    barge_in_threshold: float = 0.055
    # Sustained for this long before it yields, so a cough, a door or a
    # keystroke does not stop it mid-sentence.
    barge_in_hold_ms: int = 250
    # Ignore the opening of a reply. Without this the tail of your own last
    # word, still decaying in the room, cancels the answer to it.
    barge_in_grace_ms: int = 500
    # How long after playback ends the mic stays gated, covering speaker decay.
    # Too high and it clips the first word of your next sentence.
    echo_guard_ms: int = 250

    # Utterance capture.
    max_utterance_seconds: float = 12.0
    silence_timeout_seconds: float = 1.1
    min_utterance_seconds: float = 0.4
    # RMS below this counts as silence. Raise it in a noisy room.
    silence_threshold: float = 0.010

    # Speech to text.
    stt_model: str = "small.en"
    stt_device: str = "auto"  # auto | cuda | cpu
    stt_compute_type: str = ""  # blank picks float16 on cuda, int8 on cpu

    # Speech synthesis. `openai` matches the voice Jarvis uses on the Pi
    # (gpt-4o-mini-tts / fable); `piper` is fully local and free but plainer.
    tts_backend: str = "openai"
    openai_tts_model: str = "gpt-4o-mini-tts"
    openai_tts_voice: str = "fable"
    # Delivery direction. Blank uses the British-butler default that mirrors
    # Jarvis's own accent prompt.
    tts_instructions: str = ""

    piper_voice: str = "en_GB-alan-medium"
    piper_dir: str = "models/piper"
    speak_locally: bool = True
    # Pace and pitch variation. 1.0 is the model's default; above 1 is slower.
    speech_rate: float = 1.0

    # Manner is NOT configured here: in realtime mode the personality comes
    # from Jarvis's own prompt, fetched from the Pi so the two machines cannot
    # drift into being different assistants.

    # Where the thinking happens. `jarvis` posts the transcript to the Pi and
    # speaks its reply; `local` answers PC questions from the command registry
    # without involving Jarvis at all.
    brain: str = "jarvis"
    # Endpoint on the Pi that accepts {"text": ...} and returns {"reply": ...}.
    # Reached through Home Assistant, since Jarvis's own port is loopback-only.
    jarvis_ask_service: str = "rest_command/jarvis_ask"
    jarvis_timeout_seconds: float = 30.0

    # Announce on MQTT that this PC is handling voice, so Jarvis can stand down
    # and not answer the same wake word from the other side of the room.
    claim_wake_word: bool = True

    # Let the assistant look at the screen (the `view_screen` tool). The capture
    # is sent to OpenAI as an image, so turning this off is the way to guarantee
    # nothing on screen ever leaves the machine.
    vision: bool = True
    # Downscale before sending. Wide enough to read most on-screen text across
    # two monitors, small enough not to stall the realtime socket.
    vision_max_width: int = 1600
    vision_jpeg_quality: int = 70

    # Reading the text of the window in front, instead of photographing it.
    # Independent of `vision`: with vision off and this on, the assistant can
    # answer "what am I looking at" and no picture is ever taken. The text
    # still reaches the model when a voice session asks, so this is narrower
    # than a screenshot rather than private.
    screen_text: bool = True
    # Never read a window whose process name or title matches one of these.
    # Checked before anything is read at all.
    screen_text_deny: list[str] = field(
        default_factory=lambda: [
            "keepass", "bitwarden", "1password", "lastpass", "dashlane",
            "private browsing", "incognito", "inprivate",
        ]
    )
    screen_text_max_chars: int = 4000
    # UI Automation is a cross-process call, and an application that has hung
    # hangs its caller, so the read happens on a thread with a deadline.
    screen_text_timeout_seconds: float = 4.0


@dataclass(slots=True)
class WebConfig:
    enabled: bool = True
    # Browser to use when the caller does not name one. Blank = the system
    # default, which is usually what someone means by "open youtube".
    default_browser: str = ""
    # name -> executable, for browsers installed somewhere unusual. The common
    # install paths for firefox, chrome, edge, brave, opera and vivaldi are
    # already searched, so this is only needed for the exceptions.
    browsers: dict[str, str] = field(default_factory=dict)
    # Extra "open <name>" destinations, merged over the built-in table.
    sites: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class WeatherConfig:
    """Where "what's the weather" is about, and in which units.

    Forecasts come from Open-Meteo, which needs no key. With no place or
    coordinates set, the location is worked out from this PC's public IP.
    """

    enabled: bool = True
    # A town, "Leeds, UK", a postcode... anything Open-Meteo's geocoder knows.
    place: str = ""
    # Exact coordinates win over `place`; both blank = locate by IP.
    latitude: float | None = None
    longitude: float | None = None
    # auto (Fahrenheit and mph in the US, Celsius and km/h elsewhere),
    # metric or imperial.
    units: str = "auto"


@dataclass(slots=True)
class CodeConfig:
    """Letting the assistant hand work to Claude Code.

    Off by default. Everything else in this project reads state or opens a
    window; this edits source, so it is opt-in and the projects it may touch
    are an explicit allowlist rather than anywhere on disk.
    """

    enabled: bool = False
    # Blank searches PATH and the usual install locations.
    cli_path: str = ""
    # Spoken name -> directory. The trust boundary.
    projects: dict[str, str] = field(default_factory=dict)
    # acceptEdits lets it change files without stopping to ask, which it has
    # to do when nobody is at the keyboard. `plan` makes it read-only.
    permission_mode: str = "acceptEdits"
    model: str = ""
    # Run against the account Claude Code is signed in to (a Max plan
    # here) rather than an API key. Claude Code prefers ANTHROPIC_API_KEY
    # when it is set, so this hides it from the child process; otherwise
    # setting that variable for some other project would silently move
    # this work onto metered billing.
    use_subscription: bool = True
    timeout_seconds: float = 900.0
    max_concurrent: int = 1
    # Say the outcome aloud when the job lands.
    speak_when_done: bool = True


@dataclass(slots=True)
class ClaudeConfig:
    """Watching the Claude Code sessions open on this machine.

    Read-only by nature - it tails the transcripts Claude Code writes for
    itself - so it is on by default. Sending a session a prompt is the one
    thing here that makes something happen, and `allow_prompt` gates it.
    """

    enabled: bool = True
    # Blank = ~/.claude, or $CLAUDE_CONFIG_DIR when that is set.
    home: str = ""
    # How often the transcripts are tailed; the file system makes this cheap.
    poll_seconds: float = 3.0
    # How often the projects folder is rescanned for new sessions.
    scan_seconds: float = 10.0
    # How often listening ports are re-read and probed.
    apps_seconds: float = 10.0
    # A transcript untouched for longer than this is no longer a session
    # worth showing. Live processes are always shown.
    recent_minutes: float = 240.0
    # Where the installed hooks write what they see. Relative to the config.
    events_file: str = "logs/claude-events.jsonl"
    # Say aloud when a session finishes a turn, if the turn took at least
    # `speak_after_seconds`: a quick answer is not news, a five-minute job is.
    speak_when_done: bool = True
    speak_after_seconds: float = 45.0
    # Say aloud when a session stops to ask something or wants permission.
    speak_needs_input: bool = True
    # Say aloud when a dev server appears or goes away.
    speak_apps: bool = False
    respect_quiet_hours: bool = True
    # `claude.prompt`: let the voice put words into a session.
    allow_prompt: bool = True
    # Put in front of every prompt sent this way, so the session knows where
    # it came from. Blank sends the words as spoken.
    prompt_prefix: str = ""
    # A message into a session must attest the session's own permission
    # class - "bypass" or "prompting" - or the session parks it for a review.
    # The transcript normally says which; this is the guess when it does not.
    assume_permission_class: str = "bypass"
    # A terminal or VS Code session with no live process cannot be messaged;
    # with this on, the prompt runs through the CLI against the same
    # transcript instead. Never applied to a desktop-app conversation: the
    # app forks a fresh transcript for every turn it starts, so a turn
    # appended behind its back would never be shown or carried forward.
    resume_fallback: bool = True
    # Blank searches PATH, the usual install locations, then the copies the
    # VS Code extension and the desktop app carry.
    cli_path: str = ""
    permission_mode: str = "acceptEdits"


@dataclass(slots=True)
class MemoryConfig:
    """What the assistant keeps between conversations.

    A realtime session ends after a few seconds of silence, so without this
    every wake word introduces the assistant to the user again.
    """

    enabled: bool = True
    file: str = "logs/memory.json"
    conversation_file: str = "logs/conversations.jsonl"
    # Facts kept on disk, and how many are handed to a voice session. Every
    # injected fact is prompt on every wake word, so the second number is the
    # one that costs money.
    max_facts: int = 500
    inject_facts: int = 40
    # Carry the tail of the last conversation into the next one, so a
    # follow-up after a wake-word gap still lands.
    carry_conversation: bool = True
    carry_turns: int = 12
    carry_max_age_minutes: int = 180
    max_conversations: int = 200


@dataclass(slots=True)
class OutlookConfig:
    """Mail and calendar read from the Outlook desktop client over COM.

    Off by default. Everything else in this project reads the machine; this
    reads the user's mailbox, which is not something to switch on by accident.

    Needs classic Outlook (the Office 16 one) signed in and running on the
    logged-on desktop. The new Outlook exposes no COM automation at all, so it
    cannot be read this way. See monitors/outlook.py for why COM rather than
    Microsoft Graph.
    """

    enabled: bool = False
    mail: bool = True
    calendar: bool = True

    # Seconds between COM polls. This is the worst-case lateness for noticing
    # new mail. Meeting timing does not depend on it: minutes-until is worked
    # out from the cached start time on every snapshot.
    poll_seconds: float = 60.0

    # Attach to the Outlook the user already has open rather than starting one.
    # An agent launching Outlook by itself is a surprise nobody asked for.
    require_running: bool = True

    # How far ahead to read the calendar, and how many occurrences to walk. The
    # cap matters because a recurring series with no end date expands forever.
    lookahead_hours: float = 12.0
    max_events: int = 60
    # Only the first few events pay for a body read to find a join link; the
    # rest are counted but not described.
    detail_events: int = 6
    # All-day events are calendar entries, not meetings to be reminded about
    # five minutes ahead, so they are left out of "what's next" by default.
    include_all_day: bool = False

    # Newest-first inbox items to walk when looking for one worth mentioning.
    scan_messages: int = 15
    # Substring matches, case-insensitive, against the sender's display name and
    # the subject. They suppress the "something arrived" announcement; the unread
    # count is Outlook's own and still counts everything.
    ignore_senders: list[str] = field(default_factory=list)
    ignore_subjects: list[str] = field(default_factory=list)
    # Subjects are read aloud and published to MQTT. Set false to keep the fact
    # that mail arrived without saying what it was about.
    include_subjects: bool = True


@dataclass(slots=True)
class NotificationsConfig:
    """Teams and Outlook, read from the Windows notification centre.

    The new Outlook and Teams expose nothing a local process can read, but
    both raise toasts, and Windows lets an allowed app read those. So this
    sees exactly what the user sees - sender, subject, first line - with no
    Graph registration and no admin consent. See monitors/notifications.py.

    Off by default: it reads work messages, which is not something to switch
    on by accident.
    """

    enabled: bool = False
    # Say a new one aloud as it arrives, through the same route alerts use.
    # `enabled` alone records them for the console and for "what did I miss".
    speak: bool = False
    # Say the message or subject, not only who it is from.
    include_text: bool = True
    # Spoken announcements respect proactive.quiet_hours.
    respect_quiet_hours: bool = True
    # Above this many arriving in one tick, they are summarised as a count.
    speak_up_to: int = 3
    poll_seconds: float = 5.0
    # How many are kept for "what did I miss".
    keep: int = 40
    # Label -> substring of the app's user model id (or display name). Blank
    # means the built-in Teams and Outlook pair.
    apps: dict[str, str] = field(default_factory=dict)
    # Substring matches, case-insensitive, on the sender and on the text.
    ignore_senders: list[str] = field(default_factory=list)
    ignore_subjects: list[str] = field(default_factory=list)


@dataclass(slots=True)
class FaceConfig:
    enabled: bool = True
    # "holo" is the J.A.R.V.I.S. core: a spinning cage of gold filament that
    # flares with the voice. "orb" is the older cartoon face with eyes.
    style: str = "holo"
    size: int = 200
    # An anchor name (top-left, top-right, bottom-left, bottom-right, center)
    # or explicit "x,y" pixels.
    position: str = "bottom-right"
    margin: int = 24
    opacity: float = 0.94
    always_on_top: bool = True
    # Capped at 144. The animation is frame-rate independent, so this only
    # trades CPU for smoothness.
    fps: int = 60
    # Render scale before downsampling. 2 is smooth; 1 is cheaper.
    supersample: int = 2
    hide_when_idle: bool = False
    # A notification-area icon that shows whether Jarvis is reachable and
    # can hide/show the avatar without killing the process.
    tray: bool = True
    # Let the eyes track the mouse pointer anywhere on screen.
    follow_cursor: bool = True
    # Colour scheme for the holo style. gold is the J.A.R.V.I.S. core; steel
    # is a cool cyan that reads as a different machine. Blank picks gold when
    # assistant.mirror_jarvis is on and steel otherwise.
    palette: str = ""
    # The shape, independent of the colour. core is the J.A.R.V.I.S. sphere;
    # lattice is Arnold's - an evenly ruled globe with long traces, a
    # gyroscope at the centre, turning the other way. Blank follows palette's
    # rule: core when mirroring Jarvis, lattice otherwise.
    design: str = ""
    # MQTT topics this PC's own voice session publishes its state on, and the
    # face follows. Blank = "<assistant name>/hud" (or jarvis_topic_prefix
    # when mirroring Jarvis).
    topic_prefix: str = ""
    # MQTT topics the Pi-side bridge publishes Jarvis's state on. The wake
    # claim lives here whatever the assistant is called.
    jarvis_topic_prefix: str = "jarvis/hud"
    # Also react to Jarvis's own listening/speaking, relayed from the Pi. Off
    # by default: this is the PC assistant's face, and lighting up when
    # something in another room is talking is exactly the confusion a
    # separate identity is meant to end.
    follow_jarvis: bool = False


@dataclass(slots=True)
class HistoryConfig:
    """The series the agent keeps, so it can talk about trends and not just now."""

    enabled: bool = True
    # Fine samples, one per telemetry tick. 180 is half an hour at the default
    # interval - what the dashboard's sparklines are drawn from.
    fine_samples: int = 180
    # Hourly buckets. Three months is enough to say "since you installed it".
    hourly_buckets: int = 24 * 90


@dataclass(slots=True)
class ProactiveConfig:
    """Speaking without being asked.

    `enabled` runs the observers and records what it would have said;
    `speak` is what lets it actually say any of it. They are separate on
    purpose - read a week of the notice book before letting it talk.
    """

    enabled: bool = True
    speak: bool = False
    interval_seconds: float = 300.0
    # "HH:MM-HH:MM", crossing midnight if the end is earlier than the start.
    quiet_hours: str = "22:30-08:00"
    min_gap_minutes: float = 45.0
    max_per_day: int = 6
    repeat_after_hours: float = 24.0
    # Below this, a notice is recorded but never spoken.
    min_priority: int = 4
    # rules  - the highest-priority notice that clears the bar.
    # model  - ask a small model whether any of it is worth interrupting for,
    #          given the time and what is on screen. Falls back to rules
    #          whenever it cannot answer.
    judge: str = "rules"
    judge_model: str = "gpt-4o-mini"

    # What the observers count as worth raising.
    disk_days_ahead: float = 21.0      # warn when a disk is this close to full
    memory_climb_per_day: float = 12.0  # percentage points a day
    gpu_drift_per_day: float = 1.5      # degrees a day
    log_error_threshold: int = 5        # errors in an hour

    # A pending Windows update is only worth mentioning on a machine that has
    # been up long enough to have ignored it. 0 switches it off.
    reboot_after_hours: float = 72.0
    # "Steam has closed" - the moment a watched process stops, which is the
    # answer to "did it finish while I was out".
    announce_watched_exit: bool = True
    battery_percent: float = 20.0       # on battery and at or below this
    battery_minutes: float = 25.0       # ...or this little time left
    # How long the Pi has to be unreachable before it is worth saying that
    # everything is now coming out of these speakers instead.
    pi_quiet_hours: float = 2.0
    announce_missing_drives: bool = True
    # "You've been at this for four hours." Off by default, and priority 3 -
    # below min_priority - so switching it on only starts recording it. Lower
    # min_priority too if you actually want to be told.
    break_after_hours: float = 0.0


@dataclass(slots=True)
class ScheduleConfig:
    """Reminders and jobs that fire at a time rather than on request."""

    enabled: bool = True
    max_jobs: int = 200
    # Commands a scheduled job may run. Empty means reminders only - a spoken
    # "at midnight, shut down" should not become a standing shutdown order
    # unless that was set up deliberately.
    allow_commands: list[str] = field(default_factory=list)

    # Kitchen-style timers that ring on this PC's own speakers.
    timers: bool = True
    # Play a sound when one fires, not just the sentence. This is the whole
    # difference between a timer and a reminder.
    ring: bool = True
    # Beyond this, it is not a timer, it is a reminder - and a timer you
    # cannot see counting down is a bad way to remember tomorrow.
    max_timer_hours: float = 24.0


@dataclass(slots=True)
class UiConfig:
    """The dashboard served by `arnold ui`.

    Loopback by default. The dashboard can run every command in the registry,
    so binding it to the LAN is binding a remote-control surface to the LAN -
    which is why a token is required the moment `host` is not loopback.
    """

    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8770
    # Required for any non-loopback bind. Sent as a header by the page, never
    # as a cookie, so a hostile page in another tab has nothing ambient to ride.
    token: str = ""
    open_browser: bool = True
    # How often the page asks for a fresh snapshot.
    refresh_seconds: float = 2.0
    # Lines of the agent log the dashboard shows.
    log_lines: int = 200


SPEECH_ROUTES = ("jarvis", "local", "both", "auto", "none")
SPEECH_BACKENDS = ("auto", "openai", "piper")


@dataclass(slots=True)
class SpeechConfig:
    """Which room this PC says things in.

    Everything the agent volunteers used to go to Jarvis on the Pi and nowhere
    else, so with the Pi off it was written to the log and lost. This is the
    PC's own mouth, and the route decides who uses it.
    """

    # jarvis - the Pi only, exactly as before.
    # local  - this PC's speakers only.
    # both   - say it in both rooms.
    # auto   - try the Pi; speak here only if he could not be reached.
    # none   - never speak.
    # Blank follows the older jarvis.speech_route, so an upgrade changes
    # nothing by itself.
    route: str = ""
    # auto uses voice.tts_backend (OpenAI, with Piper as the net).
    backend: str = "auto"
    # Blank uses voice.output_device, then the system default.
    output_device: str = ""
    volume: float = 0.8

    # Speaking is a TTS round trip plus playback, so it happens on one worker
    # thread and the agent's tick never waits for it.
    max_queued: int = 8
    # Nobody wants a nine-minute-old alert read out.
    stale_after_seconds: float = 90.0
    shutdown_wait_seconds: float = 5.0

    # After the Pi refuses, stop asking him for a while: otherwise every line
    # pays a Home Assistant timeout before anything is said here.
    jarvis_retry_seconds: float = 60.0
    jarvis_retry_max_seconds: float = 600.0

    # Do not talk over a live conversation. The voice session leaves a marker
    # beside the state file; a queued line waits for it, up to this long.
    defer_to_conversation: bool = True
    defer_seconds: float = 60.0

    # Tell the face when the agent is talking, so it moves its mouth for an
    # alert as well as for a conversation.
    publish_face_state: bool = True

    # The timer ring, generated locally so it needs nothing at all.
    ring_volume: float = 0.6


def is_loopback(host: str) -> bool:
    host = (host or "").strip().strip("[]").lower()
    return host in ("", "localhost", "::1") or host.startswith("127.")


# The inbox settings live with the Graph client; imported here so `todo.mail`
# is a section like any other. graph_mail imports nothing from this module.
from .graph_mail import MailConfig  # noqa: E402


@dataclass(slots=True)
class TodoConfig:
    """The to-do list on the dashboard, and the weekly document it is filled
    from.

    The document arrives by email in the *new* Outlook, which nothing local
    can read - but the attachment lands on disk when it is opened in Outlook
    (its cache) or when a "save attachments to OneDrive" flow files it. Those
    folders are watched; the newest document whose path mentions one of
    `match` becomes this week's list. See todos.py.
    """

    enabled: bool = True
    # Where the list lives. Blank = todos.json beside the state file.
    file: str = ""
    max_items: int = 300

    # The weekly document. `source_name` labels its items on the page and in
    # speech; `weekday` is when it is expected, so the page can say "not yet"
    # rather than "nothing".
    source_name: str = "geo update"
    weekday: str = "wednesday"
    # Folders to look in. Blank = OneDrive's "Email attachments" and the new
    # Outlook's attachment cache. Environment variables and ~ are expanded.
    watch_folders: list[str] = field(default_factory=list)
    # Case-insensitive substrings; a document counts when its path under a
    # watched folder contains any of them. Empty = every document there.
    match: list[str] = field(default_factory=lambda: ["geo update", "geo projects"])
    extensions: list[str] = field(default_factory=lambda: [".docx", ".pdf", ".txt", ".md"])
    # How often the folders are walked. They are small, but not free.
    scan_seconds: float = 120.0
    # The inbox itself, through Microsoft Graph: the first place looked, once
    # someone has signed in. The folders above remain the fallback.
    mail: MailConfig = field(default_factory=MailConfig)


@dataclass(slots=True)
class Config:
    device: DeviceConfig = field(default_factory=DeviceConfig)
    assistant: AssistantConfig = field(default_factory=AssistantConfig)
    mqtt: MqttConfig = field(default_factory=MqttConfig)
    jarvis: JarvisConfig = field(default_factory=JarvisConfig)
    speech: SpeechConfig = field(default_factory=SpeechConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    telemetry: TelemetryConfig = field(default_factory=TelemetryConfig)
    monitors: MonitorsConfig = field(default_factory=MonitorsConfig)
    face: FaceConfig = field(default_factory=FaceConfig)
    voice: VoiceConfig = field(default_factory=VoiceConfig)
    web: WebConfig = field(default_factory=WebConfig)
    weather: WeatherConfig = field(default_factory=WeatherConfig)
    code: CodeConfig = field(default_factory=CodeConfig)
    claude: ClaudeConfig = field(default_factory=ClaudeConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    outlook: OutlookConfig = field(default_factory=OutlookConfig)
    notifications: NotificationsConfig = field(default_factory=NotificationsConfig)
    ui: UiConfig = field(default_factory=UiConfig)
    history: HistoryConfig = field(default_factory=HistoryConfig)
    proactive: ProactiveConfig = field(default_factory=ProactiveConfig)
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    todo: TodoConfig = field(default_factory=TodoConfig)
    alerts: list[dict[str, Any]] = field(default_factory=list)
    log_level: str = "INFO"
    log_file: str = ""
    # Where the agent records live state so one-shot `exec` calls can read it.
    state_file: str = "logs/state.json"
    source_path: Path | None = None
    # The profile apply_profile() last laid over the flat fields, if any.
    active_profile: str = ""

    # -- identity -----------------------------------------------------------

    def profiles(self) -> dict[str, ProfileConfig]:
        """Every identity this PC can be: the built-ins, with the config's
        own entries merged over them key by key."""
        merged: dict[str, dict[str, Any]] = {
            name: dict(fields) for name, fields in BUILTIN_PROFILES.items()
        }
        for raw_name, fields in (self.assistant.profiles or {}).items():
            name = _slug(str(raw_name))
            if not name:
                raise ConfigError(f"assistant.profiles has an entry with no usable name: {raw_name!r}")
            if fields is None:
                fields = {}
            if not isinstance(fields, dict):
                raise ConfigError(
                    f"assistant.profiles.{name} must be a mapping, got {type(fields).__name__}"
                )
            merged.setdefault(name, {}).update(fields)
        return {
            name: _build(ProfileConfig, fields, f"assistant.profiles.{name}")
            for name, fields in merged.items()
        }

    def apply_profile(self, name: str | None = None) -> str:
        """Lay a profile's set fields over the flat identity settings.

        Blank means no profile: the flat fields stand exactly as written.
        Returns the name applied, or "" for none.
        """
        chosen = _slug(str(name if name is not None else self.assistant.profile) or "")
        if not chosen:
            self.active_profile = ""
            return ""
        profiles = self.profiles()
        if chosen not in profiles:
            raise ConfigError(
                f"assistant.profile {chosen!r} is not a profile. Have: {', '.join(sorted(profiles))}"
            )
        profile = profiles[chosen]
        # str() on the way in: `name: 123` in YAML must not become an int
        # that .strip() chokes on at the next wake word.
        if profile.name is not None:
            self.assistant.name = str(profile.name)
        if profile.title is not None:
            self.assistant.title = str(profile.title)
        if profile.motto is not None:
            self.assistant.motto = str(profile.motto)
        if profile.voice is not None:
            self.assistant.voice = str(profile.voice)
        if profile.persona is not None:
            self.assistant.persona = str(profile.persona)
        if profile.delivery is not None:
            self.assistant.delivery = str(profile.delivery)
        if profile.mirror_jarvis is not None:
            self.assistant.mirror_jarvis = bool(profile.mirror_jarvis)
        if profile.wake_word is not None:
            self.voice.wake_word = str(profile.wake_word)
        if profile.palette is not None:
            self.face.palette = str(profile.palette)
        if profile.design is not None:
            self.face.design = str(profile.design)
        self.assistant.profile = chosen
        self.active_profile = chosen
        return chosen

    def identity_key(self) -> tuple:
        """Everything a running process would have to rebuild on a switch."""
        return (
            self.assistant_name(),
            self.assistant_title(),
            self.assistant_motto(),
            self.assistant.voice,
            self.assistant.persona,
            self.assistant.delivery,
            bool(self.assistant.mirror_jarvis),
            self.voice.wake_word,
            self.voice.wake_word_dir,
            self.face_palette(),
            self.face_design(),
            self.assistant_topic_prefix(),
        )

    def adopt_identity(self, other: "Config") -> bool:
        """Take another Config's identity, in place.

        In place on purpose: the command context, the tool dispatcher, the
        face feed and the dashboard all hold this same object, and swapping
        it would leave half of them talking about yesterday's assistant.
        Returns whether anything that matters changed.
        """
        before = self.identity_key()
        self.assistant.name = other.assistant.name
        self.assistant.title = other.assistant.title
        self.assistant.motto = other.assistant.motto
        self.assistant.voice = other.assistant.voice
        self.assistant.persona = other.assistant.persona
        self.assistant.delivery = other.assistant.delivery
        self.assistant.mirror_jarvis = other.assistant.mirror_jarvis
        self.assistant.profile = other.assistant.profile
        self.assistant.profiles = dict(other.assistant.profiles)
        self.active_profile = other.active_profile
        self.voice.wake_word = other.voice.wake_word
        self.voice.wake_word_dir = other.voice.wake_word_dir
        self.face.palette = other.face.palette
        self.face.design = other.face.design
        self.face.topic_prefix = other.face.topic_prefix
        return self.identity_key() != before

    def assistant_name(self) -> str:
        """What the assistant on this PC is called."""
        if self.assistant.mirror_jarvis:
            return "Jarvis"
        return self.assistant.name.strip() or "Arnold"

    def tts_voice(self) -> str:
        """The voice this PC's own speech is synthesised in.

        Its own assistant speaks in its own voice, so an alert sounds like the
        same person as the conversation. Mirroring Jarvis, it is
        voice.openai_tts_voice, which matches the Pi.
        """
        if self.assistant.mirror_jarvis:
            return self.voice.openai_tts_voice
        return self.assistant.voice.strip() or self.voice.openai_tts_voice

    def assistant_title(self) -> str:
        """The name as it is written on screen, e.g. A.R.N.O.L.D."""
        if self.assistant.mirror_jarvis:
            return "Jarvis"
        return self.assistant.title.strip() or self.assistant_name()

    def assistant_motto(self) -> str:
        """What the title stands for, shown under it. Blank for none."""
        if self.assistant.mirror_jarvis:
            return ""
        return self.assistant.motto.strip()

    def assistant_topic_prefix(self) -> str:
        """Where this PC's own voice state is published for the face.

        Jarvis's state from the Pi stays on face.jarvis_topic_prefix. Keeping
        the two apart is what lets the face show *this* assistant rather than
        whichever of the two last said something.
        """
        if self.face.topic_prefix.strip():
            return self.face.topic_prefix.strip().rstrip("/")
        if self.assistant.mirror_jarvis:
            return self.face.jarvis_topic_prefix.rstrip("/")
        return f"{self.assistant.slug}/hud"

    def speech_route(self) -> str:
        """Where spoken output goes.

        Blank in config means: keep doing what the older jarvis.speech_route
        said, so upgrading does not by itself make a silent machine start
        talking. 'none' there still means silence; anything else becomes
        'auto', which only speaks here when the Pi could not be reached.
        """
        chosen = (self.speech.route or "").strip().lower()
        if chosen:
            return chosen
        return "none" if self.jarvis.speech_route == "none" else "auto"

    def face_palette(self) -> str:
        if self.face.palette.strip():
            return self.face.palette.strip().lower()
        return "gold" if self.assistant.mirror_jarvis else "steel"

    def face_design(self) -> str:
        if self.face.design.strip():
            return self.face.design.strip().lower()
        return "core" if self.assistant.mirror_jarvis else "lattice"

    def validate(self) -> list[str]:
        """Return a list of human-readable problems (empty means OK)."""
        problems: list[str] = []
        if not self.assistant.name.strip():
            problems.append("assistant.name is empty - it needs something to call itself")
        try:
            profiles = self.profiles()
        except ConfigError as exc:
            problems.append(str(exc))
        else:
            wanted = _slug(self.assistant.profile or "")
            if wanted and wanted not in profiles:
                problems.append(
                    f"assistant.profile {wanted!r} is not a profile. "
                    f"Have: {', '.join(sorted(profiles))}"
                )
        if (
            self.voice.claim_wake_word
            and not self.assistant.mirror_jarvis
            and self.voice.wake_word != "hey_jarvis"
        ):
            problems.append(
                f"voice.claim_wake_word is true but the wake word is {self.voice.wake_word!r}, "
                "not hey_jarvis - the Pi would go quiet for 'hey jarvis' whenever this PC is "
                "up, for nothing. Set it false; the two answer different words"
            )
        if self.security.require_signature and not self.security.shared_secret:
            problems.append(
                "security.require_signature is true but security.shared_secret is empty "
                "- run `arnold gen-secret` and set it"
            )
        if self.security.shared_secret and len(self.security.shared_secret) < 16:
            problems.append("security.shared_secret is shorter than 16 characters")
        if self.mqtt.enabled and not self.mqtt.host:
            problems.append("mqtt.enabled is true but mqtt.host is empty")
        if self.mqtt.enabled and not self.mqtt.username:
            problems.append(
                "mqtt.username is empty - the Pi's Mosquitto sets allow_anonymous false, "
                "so an anonymous connection will be refused"
            )
        speech_route = (self.speech.route or "").strip().lower()
        if speech_route and speech_route not in SPEECH_ROUTES:
            problems.append(
                f"speech.route {speech_route!r} is not one of: {', '.join(SPEECH_ROUTES)}"
            )
        speech_backend = (self.speech.backend or "").strip().lower()
        if speech_backend and speech_backend not in SPEECH_BACKENDS:
            problems.append(
                f"speech.backend {speech_backend!r} is not one of: "
                f"{', '.join(SPEECH_BACKENDS)}"
            )
        if not 0.0 <= self.speech.volume <= 1.0:
            problems.append(f"speech.volume must be between 0 and 1; got {self.speech.volume}")

        route = self.jarvis.speech_route
        if route not in ("home_assistant", "ssh", "none"):
            problems.append(
                f"jarvis.speech_route {route!r} is not one of: home_assistant, ssh, none"
            )
        if route == "home_assistant" and not self.jarvis.home_assistant.token:
            problems.append(
                "jarvis.speech_route is home_assistant but jarvis.home_assistant.token is empty "
                "- create a long-lived access token in your HA profile page"
            )
        for name, path in self.security.launch_allowlist.items():
            if not Path(path).is_absolute():
                problems.append(f"security.launch_allowlist[{name}] must be an absolute path")
        if self.outlook.enabled and not self.outlook.mail and not self.outlook.calendar:
            problems.append(
                "outlook.enabled is true but both outlook.mail and outlook.calendar are false, "
                "so there is nothing for it to read"
            )
        if self.outlook.enabled and self.outlook.poll_seconds < 10:
            problems.append(
                f"outlook.poll_seconds is {self.outlook.poll_seconds} - each poll is a round trip "
                "into Outlook, and anything under 10 seconds is clamped to 10"
            )
        if self.notifications.enabled and self.notifications.poll_seconds < 2:
            problems.append(
                f"notifications.poll_seconds is {self.notifications.poll_seconds} - toasts do "
                "not arrive faster than that, and anything under 2 seconds is clamped to 2"
            )
        if self.claude.enabled and self.claude.poll_seconds < 1:
            problems.append(
                f"claude.poll_seconds is {self.claude.poll_seconds} - anything under 1 second "
                "is clamped to 1"
            )
        if self.claude.permission_mode not in ("default", "acceptEdits", "plan", "bypassPermissions", "dontAsk"):
            problems.append(
                f"claude.permission_mode {self.claude.permission_mode!r} is not one Claude Code "
                "accepts: default, acceptEdits, plan, bypassPermissions, dontAsk"
            )
        if self.voice.turn_detection not in ("semantic", "server", "pi"):
            problems.append(
                f"voice.turn_detection {self.voice.turn_detection!r} is not one of: "
                "semantic, server, pi"
            )
        if self.voice.turn_eagerness not in ("low", "medium", "high", "auto"):
            problems.append(
                f"voice.turn_eagerness {self.voice.turn_eagerness!r} is not one of: "
                "low, medium, high, auto"
            )
        if self.voice.turn_detection == "server" and self.voice.turn_silence_ms < 400:
            problems.append(
                f"voice.turn_silence_ms is {self.voice.turn_silence_ms} - below about 400ms "
                "it treats a pause for breath as the end of your sentence"
            )
        if self.voice.barge_in and not 0.0 < self.voice.barge_in_threshold < 1.0:
            problems.append(
                "voice.barge_in_threshold must be between 0 and 1 (it is a microphone RMS); "
                f"got {self.voice.barge_in_threshold}"
            )
        if self.proactive.judge not in ("rules", "model"):
            problems.append(
                f"proactive.judge {self.proactive.judge!r} is not one of: rules, model"
            )
        if self.proactive.speak and not self.proactive.enabled:
            problems.append(
                "proactive.speak is true but proactive.enabled is false, so nothing "
                "will ever be noticed to say"
            )
        for name in self.schedule.allow_commands:
            if not isinstance(name, str) or "." not in name:
                problems.append(
                    f"schedule.allow_commands[{name!r}] should be a command name "
                    "like control.lock"
                )
        if self.ui.enabled and not is_loopback(self.ui.host) and not self.ui.token:
            problems.append(
                f"ui.host is {self.ui.host!r}, which is reachable from the network, but "
                "ui.token is empty - anyone on the LAN could run commands. Set a token "
                "(`arnold gen-secret` makes a good one) or bind to 127.0.0.1"
            )
        return problems


def _build(section: type, data: dict[str, Any] | None, where: str) -> Any:
    data = data or {}
    if not isinstance(data, dict):
        raise ConfigError(f"config section {where!r} must be a mapping, got {type(data).__name__}")
    known = {f for f in section.__dataclass_fields__}  # type: ignore[attr-defined]
    unknown = set(data) - known
    if unknown:
        raise ConfigError(
            f"unknown key(s) in config section {where!r}: {', '.join(sorted(unknown))}"
        )
    return section(**data)


def load_config(path: str | Path | None = None) -> Config:
    """Load config from `path`, or the first of DEFAULT_CONFIG_PATHS that exists."""
    candidates = [Path(path)] if path else default_config_paths()
    chosen: Path | None = next((p for p in candidates if p.is_file()), None)
    if chosen is None:
        if path:
            raise ConfigError(f"config file not found: {path}")
        raise ConfigError(
            "no config file found. Copy config.example.yaml to config.yaml, or set "
            f"{CONFIG_ENV_VAR} to its full path. Looked in: "
            + ", ".join(str(p.resolve()) for p in candidates)
        )

    with chosen.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{chosen}: top level of config must be a mapping")
    raw = _expand(raw)

    jarvis_raw = dict(raw.get("jarvis") or {})
    ha_raw = jarvis_raw.pop("home_assistant", None)
    ssh_raw = jarvis_raw.pop("ssh", None)
    jarvis = _build(JarvisConfig, jarvis_raw, "jarvis")
    jarvis.home_assistant = _build(HomeAssistantConfig, ha_raw, "jarvis.home_assistant")
    jarvis.ssh = _build(SshConfig, ssh_raw, "jarvis.ssh")

    todo_raw = dict(raw.get("todo") or {})
    mail_raw = todo_raw.pop("mail", None)
    todo = _build(TodoConfig, todo_raw, "todo")
    todo.mail = _build(MailConfig, mail_raw, "todo.mail")

    cfg = Config(
        device=_build(DeviceConfig, raw.get("device"), "device"),
        assistant=_build(AssistantConfig, raw.get("assistant"), "assistant"),
        mqtt=_build(MqttConfig, raw.get("mqtt"), "mqtt"),
        jarvis=jarvis,
        speech=_build(SpeechConfig, raw.get("speech"), "speech"),
        security=_build(SecurityConfig, raw.get("security"), "security"),
        telemetry=_build(TelemetryConfig, raw.get("telemetry"), "telemetry"),
        monitors=_build(MonitorsConfig, raw.get("monitors"), "monitors"),
        face=_build(FaceConfig, raw.get("face"), "face"),
        voice=_build(VoiceConfig, raw.get("voice"), "voice"),
        web=_build(WebConfig, raw.get("web"), "web"),
        weather=_build(WeatherConfig, raw.get("weather"), "weather"),
        code=_build(CodeConfig, raw.get("code"), "code"),
        claude=_build(ClaudeConfig, raw.get("claude"), "claude"),
        ui=_build(UiConfig, raw.get("ui"), "ui"),
        history=_build(HistoryConfig, raw.get("history"), "history"),
        proactive=_build(ProactiveConfig, raw.get("proactive"), "proactive"),
        schedule=_build(ScheduleConfig, raw.get("schedule"), "schedule"),
        todo=todo,
        memory=_build(MemoryConfig, raw.get("memory"), "memory"),
        outlook=_build(OutlookConfig, raw.get("outlook"), "outlook"),
        notifications=_build(NotificationsConfig, raw.get("notifications"), "notifications"),
        alerts=list(raw.get("alerts") or []),
        log_level=str(raw.get("log_level") or "INFO").upper(),
        log_file=str(raw.get("log_file") or ""),
        state_file=str(raw.get("state_file") or "logs/state.json"),
        source_path=chosen,
    )
    if not cfg.mqtt.client_id:
        cfg.mqtt.client_id = f"arnold-{cfg.device.id}"

    # The active profile goes over the flat fields before anything reads them.
    cfg.apply_profile()

    # Relative paths are relative to the config file, not the working directory.
    # An SSH-invoked `exec` starts in the user's home, and would otherwise write
    # logs there and fail to find the state file the agent maintains.
    base = chosen.resolve().parent
    if cfg.state_file and not Path(cfg.state_file).is_absolute():
        cfg.state_file = str(base / cfg.state_file)
    if cfg.log_file and not Path(cfg.log_file).is_absolute():
        cfg.log_file = str(base / cfg.log_file)
    if cfg.claude.events_file and not Path(cfg.claude.events_file).is_absolute():
        cfg.claude.events_file = str(base / cfg.claude.events_file)
    if cfg.voice.wake_word_dir and not Path(cfg.voice.wake_word_dir).is_absolute():
        cfg.voice.wake_word_dir = str(base / cfg.voice.wake_word_dir)
    # A wake word given as a relative path is relative to the config too; a
    # bare model name is left alone.
    wake = cfg.voice.wake_word
    if wake and _looks_like_model_path(wake) and not Path(wake).expanduser().is_absolute():
        cfg.voice.wake_word = str(base / wake)
    for attribute in ("file", "conversation_file"):
        value = getattr(cfg.memory, attribute)
        if value and not Path(value).is_absolute():
            setattr(cfg.memory, attribute, str(base / value))

    return cfg


def _looks_like_model_path(value: str) -> bool:
    value = value.strip()
    return value.lower().endswith((".onnx", ".tflite")) or "/" in value or "\\" in value


# -- switching identities on disk --------------------------------------------

_PROFILE_NAME_RE = re.compile(r"^[a-z0-9_]+$")
_SECTION_RE = re.compile(r"^assistant:\s*(#.*)?$")
_FLOW_SECTION_RE = re.compile(r"^assistant:\s*[{\[]")
_PROFILE_LINE_RE = re.compile(r"^(\s*profile:\s*)([^#\r\n]*?)(\s*#.*)?(\r?\n)?$")
_INDENTED_KEY_RE = re.compile(r"^(\s+)[A-Za-z_][A-Za-z0-9_]*\s*:")


def set_active_profile_in_file(path: str | Path, name: str) -> None:
    """Change `assistant.profile` in a config file and nothing else.

    The file is hand-written and commented, and a YAML round trip would strip
    every comment in it. So this is a text edit of one line: the `profile:`
    key inside the `assistant:` block is rewritten, keeping its trailing
    comment; if there is none it is inserted right after `assistant:`; if
    there is no `assistant:` block one is appended. Every other byte,
    including the line endings, is left as it was.
    """
    # Strict on purpose: callers normalise (Config.apply_profile slugs), and
    # a name that needs quoting must never reach the file.
    name = (name or "").strip()
    if not _PROFILE_NAME_RE.match(name):
        raise ConfigError(f"{name!r} is not a usable profile name (letters, digits, underscore)")
    path = Path(path)
    with path.open("r", encoding="utf-8", newline="") as fh:
        lines = fh.readlines()

    newline = "\r\n" if any(line.endswith("\r\n") for line in lines) else "\n"
    if any(_FLOW_SECTION_RE.match(line) for line in lines):
        # `assistant: {name: X}` on one line. Appending a block would leave
        # two `assistant:` keys and YAML keeps only the last, silently
        # dropping the name and voice. Refuse rather than guess.
        raise ConfigError(
            f"{path}: the assistant section is written on one line (assistant: {{...}}); "
            "rewrite it as an indented block before switching profiles"
        )
    section = next((i for i, line in enumerate(lines) if _SECTION_RE.match(line)), None)

    if section is None:
        if lines and not lines[-1].endswith(("\n", "\r")):
            lines[-1] += newline
        lines += [newline, f"assistant:{newline}", f"  profile: {name}{newline}"]
    else:
        # The block runs until the next unindented, non-blank, non-comment line.
        end = section + 1
        indent = ""
        replaced = False
        while end < len(lines):
            line = lines[end]
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and not line[0].isspace():
                break
            if not indent:
                key = _INDENTED_KEY_RE.match(line)
                if key:
                    indent = key.group(1)
            match = _PROFILE_LINE_RE.match(line)
            if match and not replaced and line[0].isspace():
                comment = match.group(3) or ""
                ending = match.group(4) or ""
                lines[end] = f"{match.group(1)}{name}{comment}{ending}"
                replaced = True
            end += 1
        if not replaced:
            lines.insert(section + 1, f"{indent or '  '}profile: {name}{newline}")

    tmp = path.with_name(path.name + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="") as fh:
            fh.writelines(lines)
        os.replace(tmp, path)
    except OSError:
        # Windows refuses the replace while another process has the file
        # open (a watcher mid-read, say); do not leave the temp file behind.
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


class IdentityWatcher:
    """Notices when the config file's identity changes underneath a process.

    The voice session, the face and the agent each run for days; a profile
    switch edits the file they loaded at startup. Each of them polls this
    between frames: one `stat` every couple of seconds, and a full reload only
    when the mtime moves. `changed()` hands back a freshly loaded Config when
    the identity differs, else None. A half-saved or broken file is logged
    and skipped until it changes again, so an editor mid-save cannot take the
    assistant down.
    """

    def __init__(self, config: Config, interval: float = 2.0) -> None:
        self.config = config
        self.interval = interval
        self._path = config.source_path
        self._mtime = self._stat()
        self._checked = 0.0

    def _stat(self) -> int | None:
        if self._path is None:
            return None
        try:
            return self._path.stat().st_mtime_ns
        except OSError:
            return None

    def changed(self, now: float | None = None) -> Config | None:
        if self._path is None:
            return None
        import time

        now = time.monotonic() if now is None else now
        if now - self._checked < self.interval:
            return None
        self._checked = now

        mtime = self._stat()
        if mtime == self._mtime or mtime is None:
            return None
        self._mtime = mtime
        try:
            fresh = load_config(self._path)
        except Exception as exc:  # ConfigError, a YAML parse error, a vanished file
            import logging

            logging.getLogger(__name__).warning(
                "config changed but could not be read (%s); keeping the current identity", exc
            )
            return None
        if fresh.identity_key() == self.config.identity_key():
            return None
        return fresh
