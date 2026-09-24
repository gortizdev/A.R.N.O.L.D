"""Speech synthesis via Piper, played through the PC's speakers."""

from __future__ import annotations

import logging
import threading
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)


class Speaker:
    def __init__(
        self,
        voice: str = "en_GB-alan-medium",
        model_dir: str = "models/piper",
        output_device: int | None = None,
    ) -> None:
        self.voice_name = voice
        self.output_device = output_device
        self._lock = threading.Lock()
        self._playing = threading.Event()

        directory = Path(model_dir)
        directory.mkdir(parents=True, exist_ok=True)
        model_path = directory / f"{voice}.onnx"

        if not model_path.is_file():
            log.info("downloading Piper voice %s (about 60 MB)", voice)
            try:
                from piper.download_voices import download_voice

                download_voice(voice, directory)
            except Exception as exc:
                raise RuntimeError(
                    f"could not download the Piper voice {voice!r}: {exc}"
                ) from exc

        from piper import PiperVoice

        self._voice = PiperVoice.load(str(model_path))
        log.info("piper voice %s ready", voice)

    @property
    def speaking(self) -> bool:
        return self._playing.is_set()

    def synthesize(self, text: str) -> tuple[np.ndarray, int]:
        chunks = list(self._voice.synthesize(text))
        if not chunks:
            return np.zeros(0, dtype=np.int16), 22050
        audio = np.concatenate(
            [np.frombuffer(c.audio_int16_bytes, dtype=np.int16) for c in chunks]
        )
        return audio, chunks[0].sample_rate

    def say(self, text: str, blocking: bool = True) -> float:
        """Speak `text`. Returns the audio duration in seconds."""
        text = (text or "").strip()
        if not text:
            return 0.0

        import sounddevice as sd

        # Serialised: two replies talking over each other is worse than a pause.
        with self._lock:
            audio, rate = self.synthesize(text)
            if len(audio) == 0:
                return 0.0
            duration = len(audio) / rate
            self._playing.set()
            try:
                sd.play(audio, rate, device=self.output_device)
                if blocking:
                    sd.wait()
            finally:
                if blocking:
                    self._playing.clear()
            return duration

    def stop(self) -> None:
        import sounddevice as sd

        try:
            sd.stop()
        finally:
            self._playing.clear()
