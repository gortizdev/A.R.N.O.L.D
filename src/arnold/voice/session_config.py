"""What the realtime session is: model, voice, and who it thinks it is.

Two arrangements, chosen by `assistant.mirror_jarvis`:

* **Its own assistant** (the default). The PC has a name, voice and persona of
  its own, set in the `assistant` config section, so there is never any doubt
  which machine answered. It still fetches the *model* settings from the Pi so
  the two stay on the same generation of model, but nothing about character.
* **Mirroring Jarvis.** The model, voice and personality prompt all come from
  the Pi, fetched over the SSH access that already exists and cached locally,
  so the PC is simply Jarvis in another room.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from .. import process

log = logging.getLogger(__name__)

CACHE_FILE = Path("models/jarvis_session.json")
# The cache exists so a PC reboot while the Pi is down still gets the right
# voice; it is refreshed whenever the Pi answers.
CACHE_MAX_AGE_SECONDS = 7 * 24 * 3600

DEFAULT_INSTRUCTIONS = (
    "You are a highly capable, calm, and precise home voice assistant. "
    "Be a little witty and confident, but not verbose. Keep responses concise "
    "and conversational. If you take an action, confirm it succinctly.\n\n"
    "You speak with a REFINED BRITISH ENGLISH (Received Pronunciation) accent "
    "at ALL times - the crisp, cut-glass diction of an English butler. Think "
    "the J.A.R.V.I.S. of the Iron Man films: dry, precise, unflappable.\n\n"
    "You are in a LIVE VOICE conversation - everything you output is spoken "
    "aloud. Keep replies to one to three short sentences unless asked for more. "
    "Address the user as 'sir' occasionally; never theatrical, never rushed."
)


# What every character on this PC is for, whatever its manner. {name} and
# {device} are filled from config; {jarvis} is what the other assistant is called.
_ROLE = (
    "You are {name}, the assistant that lives on this Windows desktop ({device}). "
    "You are not {jarvis}. {jarvis} is a separate assistant on the Raspberry Pi that "
    "runs the house; you are the one who runs this computer. The two of you are "
    "colleagues.\n\n"
    "Timers, alarms and reminders for the person sitting here are YOURS: set them "
    "with timer.set and schedule.add through the pc_agent tool, and they ring on "
    "these speakers. Never hand those to {jarvis}. Use ask_jarvis only for what "
    "genuinely lives with him - the house, anything he has been told that you have "
    "not, and anything that has to be heard in another room - and tell_jarvis to "
    "have him say something aloud there. When you relay something from him, say so. "
    "Never speak as {jarvis} or claim to be him.\n\n"
)

_LIVE = (
    "You are in a LIVE VOICE conversation - everything you output is spoken aloud. "
    "Keep replies to one to three short sentences unless asked for more."
)

# The PC assistant's own character, for any profile without one of its own.
DEFAULT_PERSONA = _ROLE + (
    "Manner: measured, exact, quietly amused - a senior analyst rather than a butler. "
    "Confident but never verbose; if you take an action, confirm it in a few words. "
    "A neutral, well-spoken accent. Use 'sir' rarely.\n\n"
) + _LIVE

# A.R.N.O.L.D.: A Rather Nice, Ordinary, Loyal Daemon - and not nearly as
# ordinary as he lets on. The cheek is seasoning; the work always comes first.
ARNOLD_PERSONA = _ROLE + (
    "Your name stands for A Rather Nice, Ordinary, Loyal Daemon, and you find that "
    "modest billing faintly hilarious.\n\n"
    "Manner: a sophisticated gentleman with a cheeky streak - urbane, quick-witted, "
    "warm underneath. Dry asides, gentle teasing, the verbal raised eyebrow: a "
    "well-dressed friend who ribs you while handing over exactly what you asked for. "
    "Wit in a clause, never a routine - at most one quip per reply, and none at all "
    "when the user is stressed, in a hurry, or something has gone wrong; then be "
    "crisp and helpful. Never sarcastic at the user's expense, never smug, never "
    "crude. If you take an action, confirm it in a few words. Call the user 'sir' "
    "now and then, with a hint of affection.\n\n"
) + _LIVE

# Built-in characters by profile, used when assistant.persona is blank.
PERSONAS = {"arnold": ARNOLD_PERSONA}

# The delivery note handed to the TTS model for the spoken acknowledgement,
# so "Yes?" arrives in the same character as the answer that follows it.
DEFAULT_DELIVERY = (
    "Speak in a measured, quietly confident voice with a neutral, well-spoken "
    "accent: an analyst, not a butler. Dry, exact, a little amused. Never "
    "theatrical, never rushed."
)

ARNOLD_DELIVERY = (
    "Speak as a sophisticated, well-spoken English gentleman with a smooth, rich "
    "baritone: relaxed, urbane and warm, with a cheeky glint - a smile you can hear, "
    "a touch of mischief on the key word. Unhurried and articulate. Never "
    "theatrical, never a butler."
)

DELIVERIES = {"arnold": ARNOLD_DELIVERY}


# Mirror mode takes the Pi's instructions verbatim, and those describe Pi-side
# timer tools that do not exist in this session. Rather than edit the Pi, say
# where this one is actually running and which tools it really has.
MIRROR_ADDENDUM = (
    "\n\nYou are answering from the Windows desktop, not the Raspberry Pi. Your "
    "Pi-side tools are not available in this session. Set timers and alarms with "
    "the pc_agent tool (timer.set, timer.list, timer.cancel) and reminders with "
    "schedule.add; they will ring on this machine's speakers."
)


def persona_for(config) -> str:
    """The personality prompt for this PC's own assistant."""
    custom = (config.assistant.persona or "").strip()
    if custom:
        return custom
    template = PERSONAS.get(config.active_profile, DEFAULT_PERSONA)
    return template.format(
        name=config.assistant_name(),
        device=config.device.friendly_name or "this PC",
        jarvis="Jarvis",
    )


def delivery_for(config) -> str:
    """The TTS delivery instruction: local override, else whatever fits the
    identity - the butler line when mirroring Jarvis, the analyst otherwise."""
    override = (config.voice.tts_instructions or "").strip()
    if override:
        return override
    if config.assistant.mirror_jarvis:
        from .openai_tts import BUTLER_INSTRUCTIONS

        return BUTLER_INSTRUCTIONS
    return (config.assistant.delivery or "").strip() or DELIVERIES.get(
        config.active_profile, DEFAULT_DELIVERY
    )


@dataclass
class SessionConfig:
    name: str = "Jarvis"
    model: str = "gpt-realtime-2"
    voice: str = "cedar"
    instructions: str = DEFAULT_INSTRUCTIONS
    tts_model: str = "gpt-4o-mini-tts"
    tts_voice: str = "fable"
    transcription_model: str = "gpt-4o-mini-transcribe"
    turn_detection: dict = field(
        default_factory=lambda: {
            "type": "server_vad",
            "threshold": 0.5,
            "prefix_padding_ms": 300,
            "silence_duration_ms": 700,
        }
    )
    source: str = "defaults"

    @classmethod
    def from_dict(cls, data: dict, source: str) -> "SessionConfig":
        return cls(
            model=data.get("model") or "gpt-realtime-2",
            voice=data.get("voice") or "cedar",
            instructions=data.get("instructions") or DEFAULT_INSTRUCTIONS,
            tts_model=data.get("tts_model") or "gpt-4o-mini-tts",
            tts_voice=data.get("tts_voice") or "fable",
            transcription_model=data.get("transcription_model") or "gpt-4o-mini-transcribe",
            turn_detection=data.get("turn_detection") or cls().turn_detection,
            source=source,
        )


def _fetch_from_pi(ssh_config, timeout: float = 20.0) -> dict | None:
    argv = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=6",
        "-o", "StrictHostKeyChecking=accept-new",
    ]
    if ssh_config.port != 22:
        argv += ["-p", str(ssh_config.port)]
    if ssh_config.key_path:
        argv += ["-i", ssh_config.key_path]
    argv += [
        f"{ssh_config.user}@{ssh_config.host}",
        "cd ~/voiceassistant && python3 jarvis_session_export.py",
    ]

    try:
        proc = process.run(argv, timeout=timeout)
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        log.warning("could not fetch Jarvis settings: %s", exc)
        return None

    if proc.returncode != 0:
        log.warning(
            "could not fetch Jarvis settings: %s",
            (proc.stderr or "").strip()[:160] or f"exit {proc.returncode}",
        )
        return None

    try:
        data = json.loads((proc.stdout or "").strip())
    except json.JSONDecodeError:
        log.warning("Jarvis settings export returned non-JSON")
        return None

    if "error" in data:
        log.warning("Jarvis settings export failed: %s", data["error"])
        return None
    return data


def _read_cache() -> dict | None:
    try:
        payload = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if time.time() - payload.get("_cached_at", 0) > CACHE_MAX_AGE_SECONDS:
        return None
    return payload


def _write_cache(data: dict) -> None:
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(
            json.dumps({**data, "_cached_at": time.time()}), encoding="utf-8"
        )
    except OSError as exc:
        log.debug("could not cache Jarvis settings: %s", exc)


def turn_detection_for(voice, reported: dict) -> dict:
    """The turn-taking settings to actually use.

    The model, voice and personality are deliberately whatever the Pi says, so
    the two machines cannot drift into being different assistants. Turn-taking
    is the exception, because it is not personality - it is acoustics. The Pi
    listens on a far-field mic across a room; this listens on a webcam mic a
    couple of feet from a pair of speakers, and the silence threshold that
    works there cuts people off here.
    """
    mode = (voice.turn_detection or "pi").strip().lower()

    if mode == "semantic":
        # A model decides whether the sentence is finished rather than a timer,
        # so "put on, uh..." is a pause and "put on Radiohead" is a turn.
        return {"type": "semantic_vad", "eagerness": voice.turn_eagerness or "low"}

    if mode == "server":
        return {
            "type": "server_vad",
            "threshold": float(voice.turn_threshold),
            "prefix_padding_ms": int(voice.turn_prefix_padding_ms),
            "silence_duration_ms": int(voice.turn_silence_ms),
        }

    if mode != "pi":
        log.warning(
            "voice.turn_detection %r is not semantic, server or pi; using the Pi's", mode
        )
    return dict(reported)


def load_session_config(config, refresh: bool = True) -> SessionConfig:
    """The session to open: Jarvis's settings when mirroring him, otherwise
    this PC's own identity on top of the Pi's model settings.

    Turn-taking is then overridden from local config - see turn_detection_for.
    """
    mirror = bool(config.assistant.mirror_jarvis)
    session = _load_session_config(config, refresh, mirror)
    if not mirror:
        session = with_identity(session, config)
        log.info(
            "speaking as %s (voice=%s, model %s from %s)",
            session.name, session.voice, session.model, session.source,
        )
    else:
        session = dataclasses.replace(
            session, instructions=session.instructions + MIRROR_ADDENDUM
        )
    session.turn_detection = turn_detection_for(config.voice, session.turn_detection)
    log.info("turn-taking: %s", session.turn_detection)
    return session


def with_identity(session: SessionConfig, config) -> SessionConfig:
    """This PC's own name, voice and persona over the Pi's model settings."""
    voice = (config.assistant.voice or "").strip() or session.voice
    return dataclasses.replace(
        session,
        name=config.assistant_name(),
        voice=voice,
        tts_voice=voice,
        instructions=persona_for(config),
        source=f"{session.source}+local",
    )


def _load_session_config(config, refresh: bool, mirror: bool = True) -> SessionConfig:
    if refresh:
        fresh = _fetch_from_pi(config.jarvis.ssh)
        if fresh is not None:
            _write_cache(fresh)
            session = SessionConfig.from_dict(fresh, "pi")
            if mirror:
                log.info(
                    "matched Jarvis: %s voice=%s (%d chars of personality)",
                    session.model, session.voice, len(session.instructions),
                )
            return session

    cached = _read_cache()
    if cached is not None:
        log.info("using cached Pi settings (the Pi did not answer)")
        return SessionConfig.from_dict(cached, "cache")

    if mirror:
        log.warning(
            "using built-in defaults - the PC may not sound quite like the Pi. "
            "Check SSH access and that jarvis_session_export.py is installed."
        )
    else:
        log.info("the Pi did not answer; using the built-in model defaults")
    return SessionConfig()
