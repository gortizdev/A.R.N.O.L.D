"""Speech synthesis through OpenAI, matching the voice Jarvis uses on the Pi.

Jarvis speaks with `gpt-4o-mini-tts` (config.py:134-135) and, in realtime mode,
`gpt-realtime-2`. Piper cannot approach either, so when the point is for both
machines to sound like the same assistant, this is the backend to use.

The delivery instruction matters as much as the voice. Jarvis carries a long
"refined British RP butler" prompt (REALTIME_INSTRUCTIONS_ADDENDUM in
assistant.py) and that accent is most of its character; `gpt-4o-mini-tts`
accepts the same kind of direction via `instructions`, so it is passed through
here rather than relying on the voice name alone.

Falls back to Piper when there is no API key or the network is down - a silent
assistant is worse than one that briefly sounds different.
"""

from __future__ import annotations

import io
import logging
import os
import threading

import numpy as np

log = logging.getLogger(__name__)

# Mirrors the accent section of Jarvis's realtime prompt, condensed to a
# delivery instruction. Keep both in step or the two machines drift apart.
BUTLER_INSTRUCTIONS = (
    "Speak with a refined British English (Received Pronunciation) accent at all "
    "times - the crisp, cut-glass diction of an English butler. Composed, warm but "
    "efficient; dry, precise, unflappable. Never theatrical, never rushed. "
    "Think the J.A.R.V.I.S. of the Iron Man films."
)


class OpenAISpeaker:
    def __init__(
        self,
        model: str = "gpt-4o-mini-tts",
        voice: str = "fable",
        instructions: str = BUTLER_INSTRUCTIONS,
        api_key: str = "",
        output_device: int | None = None,
        fallback=None,
    ) -> None:
        self.model = model
        self.voice = voice
        self.instructions = instructions
        self.output_device = output_device
        self.fallback = fallback
        self._lock = threading.Lock()
        self._playing = threading.Event()
        self._degraded = False

        key = api_key or os.environ.get("OPENAI_API_KEY", "")
        if not key:
            raise RuntimeError(
                "no OpenAI API key. Set OPENAI_API_KEY, or use tts_backend: piper."
            )
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError(
                'the openai package is missing - uv pip install -e ".[voice]"'
            ) from exc

        self._client = OpenAI(api_key=key, timeout=20.0)
        log.info("openai tts ready (%s, voice=%s)", model, voice)

    @property
    def speaking(self) -> bool:
        return self._playing.is_set()

    def synthesize(self, text: str) -> tuple[np.ndarray, int]:
        """Return 24 kHz mono PCM. Raises so callers can fall back."""
        response = self._client.audio.speech.create(
            model=self.model,
            voice=self.voice,
            input=text[:4000],
            instructions=self.instructions,
            response_format="pcm",  # raw 24 kHz s16le: no decoder needed
        )
        raw = response.read() if hasattr(response, "read") else response.content
        return np.frombuffer(raw, dtype=np.int16), 24000

    def say(self, text: str, blocking: bool = True) -> float:
        text = (text or "").strip()
        if not text:
            return 0.0

        import sounddevice as sd

        with self._lock:
            try:
                audio, rate = self.synthesize(text)
                if self._degraded:
                    log.info("openai tts recovered")
                    self._degraded = False
            except Exception as exc:
                if not self._degraded:
                    log.warning("openai tts failed (%s)", str(exc)[:120])
                    self._degraded = True
                if self.fallback is not None:
                    return self.fallback.say(text, blocking=blocking)
                return 0.0

            if len(audio) == 0:
                return 0.0
            self._playing.set()
            try:
                sd.play(audio, rate, device=self.output_device)
                if blocking:
                    sd.wait()
            finally:
                if blocking:
                    self._playing.clear()
            return len(audio) / rate

    def stop(self) -> None:
        import sounddevice as sd

        try:
            sd.stop()
        finally:
            self._playing.clear()


def build_speaker(config, output_device: int | None):
    """Pick a speech backend, with Piper as the safety net."""
    voice_cfg = config.voice
    from .session_config import delivery_for
    from .tts import Speaker as PiperSpeaker

    if voice_cfg.tts_backend == "piper":
        return PiperSpeaker(voice_cfg.piper_voice, voice_cfg.piper_dir, output_device)

    piper_fallback = None
    try:
        piper_fallback = PiperSpeaker(voice_cfg.piper_voice, voice_cfg.piper_dir, output_device)
    except Exception as exc:
        log.debug("no piper fallback available: %s", exc)

    try:
        return OpenAISpeaker(
            model=voice_cfg.openai_tts_model,
            voice=config.tts_voice(),
            instructions=delivery_for(config) or BUTLER_INSTRUCTIONS,
            output_device=output_device,
            fallback=piper_fallback,
        )
    except Exception as exc:
        if piper_fallback is None:
            raise
        log.warning("falling back to Piper: %s", exc)
        return piper_fallback
