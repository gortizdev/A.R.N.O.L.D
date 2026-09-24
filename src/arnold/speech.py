"""The PC's own mouth.

Everything this agent volunteers - an alert firing, a notice it decided was
worth raising, a reminder coming due, a timer going off - used to be handed to
Jarvis on the Pi and nowhere else. When the Pi is off, and it often is, all of
that was written to the log and lost. The startup banner said as much: "nothing
will be spoken". A monitoring agent that notices a disk filling up and then
cannot tell anybody is not finished.

So the agent gets a voice of its own, and a policy for which room to use:

    jarvis  the Pi, exactly as before
    local   this PC's speakers
    both    say it in both rooms
    auto    try the Pi; speak here only if he could not be reached
    none    stay quiet

`auto` is the default because it changes nothing while the Pi is up and loses
nothing when it is down.

Two constraints shape the implementation:

* **Speaking must never block the tick.** A sentence is a TTS round trip plus
  however long it takes to say out loud - seconds, against a tick that wants to
  come round every ten. So `say()` enqueues and returns, and one worker thread
  does the talking.
* **Speaking must never take the agent down.** No sounddevice, no API key, no
  audio device, no Piper voice, no Pi: each of those is a log line and silence,
  never an exception reaching the caller.

Everything under `voice/` is imported lazily, inside the worker, in a
try/except. That package pulls in numpy, faster-whisper and the wake-word stack
through its `__init__`, and none of those are core dependencies - the agent has
to run on a machine where `pip install -e .` was the whole installation.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .jarvis import JarvisError

try:  # only the making-a-noise part needs it, and it is not a core dependency
    import numpy as np
except ImportError:  # pragma: no cover - depends on the install
    np = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

RING_RATE = 24000
# How long to leave a failed speech backend alone before trying again. Long
# enough not to retry an import error every alert, short enough that a sound
# card which was not ready at logon is picked up the same afternoon.
SPEAKER_RETRY_SECONDS = 300.0
# Locally we are not talking to Jarvis's 500-character limit, but an unbounded
# string read aloud is its own kind of failure.
MAX_LOCAL_CHARS = 2000


class SpeechError(RuntimeError):
    pass


def make_ring(rate: int = RING_RATE, repeats: int = 2) -> "np.ndarray | None":
    """The timer sound: a three-note figure, generated here so it always works.

    Deliberately not the wake acknowledgement's chime. That one says "I am
    listening"; this one says "the thing you asked about has happened", and the
    two must not be mistakable across a room. Three notes rather than two, a
    fourth higher, repeated, and louder.
    """
    if np is None:
        return None
    notes = [(1046.5, 0.12), (1318.5, 0.12), (1568.0, 0.22)]  # C6 - E6 - G6
    parts = []
    for _ in range(max(1, repeats)):
        for frequency, seconds in notes:
            t = np.linspace(0.0, seconds, int(rate * seconds), endpoint=False)
            tone = np.sin(2 * np.pi * frequency * t)
            # Raised-cosine envelope: a bare sine starts and ends with a click.
            envelope = np.sin(np.pi * np.linspace(0.0, 1.0, len(t))) ** 0.6
            parts.append(tone * envelope * 0.45)
        parts.append(np.zeros(int(rate * 0.18)))
    return (np.concatenate(parts) * 32767).astype(np.int16)


# -- is somebody already talking? --------------------------------------------


def conversation_marker(config) -> Path:
    """Where the voice process says whether a conversation is open.

    A file rather than MQTT, because the broker lives on the Pi: the one time
    the agent most needs to know whether to keep quiet is exactly the time the
    broker is unreachable.
    """
    state = config.state_file or "logs/state.json"
    return Path(state).with_name("voice.json")


def conversation_is_live(config, max_age: float = 30.0) -> bool:
    """True while a voice conversation is open on this machine.

    Read through the state file's staleness rule, so a voice process that
    crashed mid-conversation cannot mute the agent forever.
    """
    from .state import read_state

    payload = read_state(conversation_marker(config), max_age=max_age)
    return bool(payload and payload.get("busy"))


def set_conversation_live(config, busy: bool) -> None:
    """Called by the voice process as a conversation opens and closes."""
    from .state import write_state

    try:
        write_state(conversation_marker(config), {"busy": bool(busy)})
    except Exception as exc:  # never cost a conversation over a marker file
        log.debug("could not update the conversation marker: %s", exc)


@dataclass(slots=True)
class _Utterance:
    text: str
    ring: bool
    queued_at: float


class Voice:
    """Says things out loud, in whichever room the route calls for.

    `say()` returns immediately and never raises. Everything after that happens
    on the worker thread, which is started on the first utterance so a one-shot
    `exec` that never speaks pays nothing for having one of these.
    """

    def __init__(
        self,
        config,
        jarvis=None,
        publish: Callable[[str, Any], None] | None = None,
        route: str = "",
    ) -> None:
        self.config = config
        self._route = (route or "").strip().lower() or config.speech_route()
        self._publish = publish

        if jarvis is None:
            from .jarvis import JarvisClient

            jarvis = JarvisClient(config.jarvis)
        self._jarvis = jarvis

        cfg = config.speech
        self._queue: queue.Queue = queue.Queue(maxsize=max(1, int(cfg.max_queued)))
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._closed = False

        # The Pi's health, so `auto` does not pay a Home Assistant timeout on
        # every single line while the Pi is off.
        self._retry_at = 0.0
        self._backoff = 0.0
        self._pi_down_since = 0.0

        self._speaker: Any = None
        self._speaker_failed_at = 0.0
        self._speaker_lock = threading.Lock()
        self._degraded = False
        self._ring: Any = None

    # -- what the caller sees ------------------------------------------------

    @property
    def route(self) -> str:
        return self._route

    @property
    def enabled(self) -> bool:
        return self._route != "none"

    @property
    def pi_down_since(self) -> float:
        """When the Pi first stopped answering, or 0.0 while it answers.

        A wall-clock stamp, so an observer can say how long it has been.
        """
        return self._pi_down_since

    def say(self, text: str, *, ring: bool = False) -> bool:
        """Queue a line to be said. Returns whether it was accepted.

        Never blocks, never raises: a failure to speak must not become a
        failure to monitor.
        """
        text = (text or "").strip()
        if not text or not self.enabled or self._closed:
            return False

        item = _Utterance(text[:MAX_LOCAL_CHARS], ring, time.monotonic())
        self._ensure_worker()
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            # The newest line is the true one for a monitoring agent, so the
            # oldest waiting line makes way rather than the new one being lost.
            try:
                dropped = self._queue.get_nowait()
                if dropped is None:
                    # close()'s sentinel. Put it back rather than swallowing
                    # it, or close() waits out its whole timeout.
                    self._queue.put_nowait(None)
                    return False
                log.warning("speech is backed up; dropped: %s", dropped.text[:60])
                self._queue.put_nowait(item)
            except (queue.Empty, queue.Full):
                log.warning("speech queue is full; not saying: %s", text[:60])
                return False
        return True

    def check(self) -> dict[str, Any]:
        """Probe both routes without saying anything. Never raises."""
        result: dict[str, Any] = {"route": self._route, "enabled": self.enabled}
        if self._route in ("jarvis", "both", "auto"):
            probe = self._jarvis.check()
            if not getattr(self._jarvis, "enabled", True):
                # 'ok' from a client that will never speak would read as a
                # working route in the log and in `diag`.
                probe = {
                    **probe,
                    "ok": False,
                    "detail": "jarvis.speech_route is 'none', so the Pi says nothing",
                }
            result["jarvis"] = probe
        if self._route in ("local", "both", "auto"):
            speaker = self._build_speaker()
            result["local"] = {
                "ok": speaker is not None,
                "backend": type(speaker).__name__ if speaker else "",
                "detail": "" if speaker else "no local speech backend available",
            }
        return result

    def close(self, timeout: float | None = None) -> None:
        """Finish the line being spoken, then stop the worker.

        Safe to call twice, and safe when nothing was ever said - in that case
        there is no thread to wait for.
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
            thread = self._thread
        if thread is None:
            return
        if timeout is None:
            timeout = max(0.0, float(self.config.speech.shutdown_wait_seconds))
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        thread.join(timeout=timeout)
        if thread.is_alive():
            log.debug("the speech worker is still talking after %.1fs", timeout)

    # -- the worker ----------------------------------------------------------

    def _ensure_worker(self) -> None:
        with self._lock:
            if self._thread is not None or self._closed:
                return
            self._thread = threading.Thread(
                target=self._run, name="speech", daemon=True
            )
            self._thread.start()

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            try:
                self._deliver(item)
            except Exception:  # a bug here must not end the agent's voice
                log.exception("could not say %r", item.text[:60])

    def _deliver(self, item: _Utterance) -> None:
        cfg = self.config.speech
        waited = time.monotonic() - item.queued_at
        if cfg.stale_after_seconds and waited > cfg.stale_after_seconds:
            log.debug("dropping a line that waited %.0fs: %s", waited, item.text[:60])
            return

        if cfg.defer_to_conversation:
            self._wait_for_quiet()

        if item.ring:
            # Always here, whatever the route: a ring is for the person at
            # this desk, not for the other room.
            self._play(self._ring_audio(), self.config.speech.ring_volume)

        spoken_by_jarvis = False
        if self._route in ("jarvis", "both", "auto") and self._pi_worth_trying():
            spoken_by_jarvis = self._say_via_jarvis(item.text)

        if self._route == "local" or self._route == "both":
            self._speak_locally(item.text)
        elif self._route == "auto" and not spoken_by_jarvis:
            self._speak_locally(item.text)

    def _wait_for_quiet(self) -> None:
        """Hold a line while a conversation is open, rather than talking over it."""
        cfg = self.config.speech
        deadline = time.monotonic() + max(0.0, float(cfg.defer_seconds))
        waited = False
        while time.monotonic() < deadline and conversation_is_live(self.config):
            waited = True
            time.sleep(min(2.0, max(0.1, deadline - time.monotonic())))
        if waited:
            log.debug("waited for the conversation to finish before speaking")

    # -- the Pi --------------------------------------------------------------

    def _pi_worth_trying(self) -> bool:
        return time.monotonic() >= self._retry_at

    def _say_via_jarvis(self, text: str) -> bool:
        cfg = self.config.speech
        # The Pi's own route can be 'none', in which case JarvisClient.say
        # quietly returns {"spoken": False} rather than raising. Treating that
        # as success would leave `auto` believing the Pi said it and skipping
        # the local speakers - silence, which is the exact failure this whole
        # module exists to remove.
        if not getattr(self._jarvis, "enabled", True):
            return False
        try:
            spoken = self._jarvis.say(text)
        except JarvisError as exc:
            if not self._pi_down_since:
                self._pi_down_since = time.time()
                log.warning("the Pi did not take the message (%s)", str(exc)[:120])
            # Back off, doubling, so a dead Pi costs one timeout a minute
            # rather than one on every line.
            self._backoff = min(
                max(cfg.jarvis_retry_seconds, self._backoff * 2 or cfg.jarvis_retry_seconds),
                cfg.jarvis_retry_max_seconds,
            )
            self._retry_at = time.monotonic() + self._backoff
            return False
        except Exception as exc:  # an unexpected client bug is still not fatal
            log.warning("the Pi route failed unexpectedly: %s", str(exc)[:120])
            return False

        if isinstance(spoken, dict) and not spoken.get("spoken", True):
            return False

        if self._pi_down_since:
            log.info("the Pi is taking messages again")
            self._pi_down_since = 0.0
        self._backoff = 0.0
        self._retry_at = 0.0
        return True

    # -- this PC -------------------------------------------------------------

    def _speak_locally(self, text: str) -> bool:
        speaker = self._build_speaker()
        if speaker is None:
            if not self._degraded:
                log.warning(
                    "no local speech backend, so this was only written to the log: %s",
                    text[:80],
                )
                self._degraded = True
            return False
        if self._degraded:
            log.info("local speech is working again")
            self._degraded = False

        try:
            audio, rate = speaker.synthesize(text)
        except Exception as exc:
            log.warning("could not synthesise speech (%s)", str(exc)[:120])
            return False
        # The length, not the words. Some of what passes through here is read
        # off the user's screen, and the log outlives the sentence.
        log.info("said here (%d characters)", len(text))
        return self._play((audio, rate), self.config.speech.volume)

    def _ring_audio(self):
        if self._ring is None:
            self._ring = (make_ring(), RING_RATE)
        return self._ring if self._ring[0] is not None else None

    def _play(self, clip, volume: float) -> bool:
        """Play a clip on this PC's speakers, telling the face while it lasts."""
        if clip is None or np is None:
            return False
        audio, rate = clip
        if audio is None or len(audio) == 0:
            return False

        try:
            import sounddevice as sd
        except Exception as exc:
            log.debug("no sounddevice, so nothing can be played here: %s", exc)
            return False

        samples = audio
        volume = max(0.0, min(1.0, float(volume)))
        if volume < 0.999:
            samples = (audio.astype(np.float32) * volume).astype(np.int16)

        self._face("speaking")
        try:
            sd.play(samples, rate, device=self._output_device())
            sd.wait()
            # Release the shared stream, or the voice session cannot open the
            # same device for its own conversation.
            sd.stop()
            return True
        except Exception as exc:
            log.warning("could not play audio here: %s", str(exc)[:120])
            return False
        finally:
            self._face("idle")

    def _face(self, state: str) -> None:
        """Let the on-screen face know the agent is talking.

        Only the state, not an audio envelope: the face synthesises a plausible
        mouth when it has no levels, so a bare "speaking" is already a face that
        talks. A retained message, and a no-op when the broker is unreachable -
        which, the broker being on the Pi, is exactly when this matters least.
        """
        if self._publish is None or not self.config.speech.publish_face_state:
            return
        try:
            self._publish("state", state)
        except Exception as exc:
            log.debug("could not publish the face state: %s", exc)

    def _output_device(self):
        cfg = self.config.speech
        name = cfg.output_device or self.config.voice.output_device
        if not name:
            return None
        try:
            from .voice.audio import resolve_device

            return resolve_device(name, input=False)
        except Exception as exc:
            log.debug("could not resolve the output device %r: %s", name, exc)
            return None

    def _build_speaker(self):
        """The local TTS backend, built lazily and never fatally.

        Every import here is deferred: `voice/__init__` pulls in the whole
        listening stack, and the agent must run without it.

        A failure is remembered but not final. This runs first at logon, when
        the audio endpoint may not be up and the network may not be there, and
        an agent that decides at boot that it can never speak stays mute until
        somebody restarts it.
        """
        with self._speaker_lock:
            return self._build_speaker_locked()

    def _build_speaker_locked(self):
        if self._speaker is not None:
            return self._speaker
        if self._speaker_failed_at and (
            time.monotonic() - self._speaker_failed_at < SPEAKER_RETRY_SECONDS
        ):
            return None
        self._speaker_failed_at = time.monotonic()

        backend = (self.config.speech.backend or "auto").strip().lower()
        device = self._output_device()
        try:
            from .voice.openai_tts import BUTLER_INSTRUCTIONS, OpenAISpeaker, build_speaker
            from .voice.session_config import delivery_for
            from .voice.tts import Speaker as PiperSpeaker

            voice_cfg = self.config.voice
            if backend == "piper":
                self._speaker = PiperSpeaker(
                    voice_cfg.piper_voice, voice_cfg.piper_dir, device
                )
            elif backend == "openai":
                self._speaker = OpenAISpeaker(
                    model=voice_cfg.openai_tts_model,
                    voice=self.config.tts_voice(),
                    instructions=delivery_for(self.config) or BUTLER_INSTRUCTIONS,
                    output_device=device,
                )
            else:
                # build_speaker already prefers OpenAI with Piper as the net.
                self._speaker = build_speaker(self.config, device)
        except ImportError as exc:
            log.info(
                'no local speech: the voice extra is not installed (%s). '
                'uv pip install -e ".[voice]"',
                str(exc)[:80],
            )
            self._speaker = None
        except Exception as exc:
            log.info("no local speech backend (%s)", str(exc)[:120])
            self._speaker = None

        if self._speaker is not None:
            self._speaker_failed_at = 0.0
            log.info("local speech ready (%s)", type(self._speaker).__name__)
        return self._speaker
