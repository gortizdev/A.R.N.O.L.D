"""The little "Yes?" that proves the wake word landed.

Without it there is no way to tell the difference between "it heard me" and
"it did not", and the natural reaction is to say the wake word again - which
lands in the middle of the session that did open and confuses it.

Latency is the whole design constraint: an acknowledgement that arrives a
second after the wake word is worse than none, because by then the user has
already started repeating themselves. So nothing is synthesised at wake time.
Phrases are rendered once, cached as WAV next to the other models, and loaded
into memory at startup; playback is a `sd.play` of an array that is already
sitting in RAM. A locally generated chime covers the gap while the render is
still running, and stands in permanently when there is no API key or network.

Playback is blocking on purpose. The caller drains the microphone afterwards,
which is what keeps the acknowledgement out of the audio the assistant is
about to listen to - the same half-duplex gating `realtime.py` applies to the
assistant's own replies.
"""

from __future__ import annotations

import hashlib
import logging
import random
import re
import threading
import wave
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

DEFAULT_PHRASES = [
    "Yes?",
    "Listening.",
    "Sir?",
    "At your service.",
    "Go ahead.",
]

# What counts as "off"/"chime" in config, tolerating YAML booleans.
_OFF = {"off", "none", "no", "false", "0", ""}
_CHIME = {"chime", "beep", "tone"}

CHIME_RATE = 24000


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:24] or "phrase"


def make_chime(rate: int = CHIME_RATE) -> np.ndarray:
    """A soft two-note rise, generated locally so it is always available."""
    notes = [(880.0, 0.09), (1318.5, 0.13)]  # A5 -> E6
    parts = []
    for frequency, seconds in notes:
        t = np.linspace(0.0, seconds, int(rate * seconds), endpoint=False)
        tone = np.sin(2 * np.pi * frequency * t)
        # Raised-cosine envelope: a bare sine starts and ends with a click.
        envelope = np.sin(np.pi * np.linspace(0.0, 1.0, len(t))) ** 0.6
        parts.append(tone * envelope * 0.25)
    audio = np.concatenate(parts)
    return (audio * 32767).astype(np.int16)


def trim_silence(
    audio: np.ndarray, threshold: float = 0.01, pad: int = 480
) -> np.ndarray:
    """Cut the lead-in and tail-off a TTS model pads its output with.

    `gpt-4o-mini-tts` returns as much as two seconds for a one-word phrase,
    almost all of it silence. Left in, that silence is exactly the dead air
    this acknowledgement exists to remove.
    """
    if len(audio) == 0:
        return audio
    loud = np.flatnonzero(np.abs(audio.astype(np.float32) / 32768.0) > threshold)
    if len(loud) == 0:
        return audio
    start = max(0, int(loud[0]) - pad)
    end = min(len(audio), int(loud[-1]) + pad)
    return audio[start:end]


class WakeAck:
    """Speaks a short acknowledgement the instant the wake word fires.

    `synth` is anything with `synthesize(text) -> (int16 array, rate)`; the
    pipeline hands over the Speaker it already built so both voices match. In
    realtime mode there is no local speaker, so one is built here from the
    OpenAI TTS settings, and if that fails the chime carries on alone.
    """

    def __init__(
        self,
        config,
        output_device: int | None = None,
        synth=None,
        tts_voice: str = "",
    ) -> None:
        voice = config.voice
        self.config = config
        self.output_device = output_device
        self._synth = synth
        # Realtime mode passes the voice the Pi's session actually speaks in,
        # so the acknowledgement and the answer are the same person. It is part
        # of the cache key, so changing it on the Pi re-renders here by itself.
        self._tts_voice = tts_voice or config.tts_voice()
        # Fixed at construction: the lazily built OpenAI speaker must not
        # change the cache key halfway through a render, or the phrase that
        # triggered the build lands under a different name than its siblings.
        self._voice_identity = (
            f"synth:{type(synth).__name__}:{voice.piper_voice}"
            if synth is not None
            else f"openai:{voice.openai_tts_model}:{self._tts_voice}"
        )
        self._mode = self._resolve_mode(voice.wake_ack)
        self.volume = max(0.0, min(1.0, float(voice.wake_ack_volume)))

        # "{name} here." follows a profile switch by itself: the substituted
        # text is what the cache is keyed on, so a new name is a new render.
        # str.replace rather than format, so a stray brace cannot raise.
        name = config.assistant_name()
        phrases = [
            str(p).strip().replace("{name}", name)
            for p in (voice.wake_ack_phrases or [])
            if str(p).strip()
        ]
        self._phrases = phrases or list(DEFAULT_PHRASES)
        self._clips: list[tuple[np.ndarray, int]] = []
        self._last_index = -1
        self._ready = threading.Event()

        self._chime = (make_chime(), CHIME_RATE) if self._mode != "off" else None
        self._cache_dir = self._resolve_cache_dir(voice.wake_ack_cache_dir)

    @property
    def enabled(self) -> bool:
        return self._mode != "off"

    @staticmethod
    def _resolve_mode(value) -> str:
        text = str(value).strip().lower()
        if text in _OFF:
            return "off"
        if text in _CHIME:
            return "chime"
        return "speech"

    def _resolve_cache_dir(self, configured: str) -> Path:
        directory = Path(configured or "models/ack")
        if not directory.is_absolute() and self.config.source_path is not None:
            # Relative to the config file, not the working directory: the
            # scheduled task that runs `listen` starts in C:\Windows\system32.
            directory = self.config.source_path.resolve().parent / directory
        return directory

    # -- preparation --------------------------------------------------------

    def prepare(self, background: bool = True) -> None:
        """Render (or load) the phrases. Cheap and cached after the first run."""
        if self._mode != "speech":
            self._ready.set()
            return
        if background:
            threading.Thread(target=self._prepare, daemon=True, name="wake-ack").start()
        else:
            self._prepare()

    def _prepare(self) -> None:
        try:
            clips = [clip for clip in (self._clip_for(p) for p in self._phrases) if clip]
        except Exception as exc:  # never let this cost us the voice assistant
            log.warning("could not prepare the wake acknowledgement: %s", exc)
            clips = []

        self._clips = clips
        self._ready.set()
        if clips:
            log.info("wake acknowledgement ready (%d phrases)", len(clips))
        else:
            log.info("wake acknowledgement falling back to the chime")

    def _clip_for(self, phrase: str) -> tuple[np.ndarray, int] | None:
        path = self._cache_path(phrase)
        clip = _read_wav(path)
        if clip is not None:
            # Trimmed on the way out rather than only before caching, so clips
            # rendered by an older build are tightened up too.
            return trim_silence(clip[0]), clip[1]

        synth = self._synthesizer()
        if synth is None:
            return None
        try:
            audio, rate = synth.synthesize(phrase)
        except Exception as exc:
            log.warning("could not synthesise %r: %s", phrase, str(exc)[:120])
            return None
        if audio is None or len(audio) == 0:
            return None

        audio = trim_silence(np.asarray(audio, dtype=np.int16))
        try:
            _write_wav(path, audio, rate)
        except Exception as exc:
            log.debug("could not cache %s: %s", path, exc)
        return audio, rate

    def _cache_path(self, phrase: str) -> Path:
        # Keyed by how it was rendered, so changing voice or model re-renders
        # rather than replaying yesterday's accent.
        from .session_config import delivery_for

        key = f"{self._voice_identity}|{delivery_for(self.config)}|{phrase}"
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]
        return self._cache_dir / f"{_slug(phrase)}-{digest}.wav"

    def _synthesizer(self):
        if self._synth is not None:
            return self._synth
        voice = self.config.voice
        try:
            from .openai_tts import OpenAISpeaker
            from .session_config import delivery_for

            self._synth = OpenAISpeaker(
                model=voice.openai_tts_model,
                voice=self._tts_voice,
                instructions=delivery_for(self.config),
                output_device=self.output_device,
            )
        except Exception as exc:
            log.info("no speech backend for the wake acknowledgement (%s)", str(exc)[:120])
            self._synth = None
        return self._synth

    # -- playback -----------------------------------------------------------

    def _pick(self) -> tuple[np.ndarray, int] | None:
        if self._mode == "off":
            return None
        if self._mode == "chime" or not self._clips:
            # Also the path taken while the render is still in flight, so the
            # first wake word after startup is still answered.
            return self._chime
        if len(self._clips) == 1:
            return self._clips[0]
        choices = [i for i in range(len(self._clips)) if i != self._last_index]
        index = random.choice(choices)
        self._last_index = index
        return self._clips[index]

    def play(self, blocking: bool = True) -> float:
        """Acknowledge the wake word. Returns the audio length in seconds."""
        clip = self._pick()
        if clip is None:
            return 0.0
        audio, rate = clip
        if len(audio) == 0:
            return 0.0

        try:
            import sounddevice as sd

            samples = audio
            if self.volume < 0.999:
                samples = (audio.astype(np.float32) * self.volume).astype(np.int16)
            sd.play(samples, rate, device=self.output_device)
            if blocking:
                sd.wait()
                # Release the shared playback stream before the conversation
                # opens its own on the same device.
                sd.stop()
        except Exception as exc:
            log.debug("could not play the wake acknowledgement: %s", exc)
            return 0.0
        return len(audio) / rate


def _read_wav(path: Path) -> tuple[np.ndarray, int] | None:
    if not path.is_file():
        return None
    try:
        with wave.open(str(path), "rb") as fh:
            if fh.getsampwidth() != 2 or fh.getnchannels() != 1:
                return None
            rate = fh.getframerate()
            data = fh.readframes(fh.getnframes())
    except Exception as exc:
        log.debug("could not read %s: %s", path, exc)
        return None
    return np.frombuffer(data, dtype=np.int16), rate


def _write_wav(path: Path, audio: np.ndarray, rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".part")
    with wave.open(str(temporary), "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(int(rate))
        fh.writeframes(audio.tobytes())
    temporary.replace(path)
