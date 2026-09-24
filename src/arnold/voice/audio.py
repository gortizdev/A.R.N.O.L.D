"""Microphone capture, wake-word detection, and utterance recording.

Everything runs at 16 kHz mono because that is what both openWakeWord and
Whisper expect; resampling once at the driver level is cheaper and cleaner than
converting per frame.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
# openWakeWord consumes 80 ms frames.
FRAME_SAMPLES = 1280


class AudioError(RuntimeError):
    pass


def resolve_device(name: str, *, input: bool) -> int | None:
    """Find a device by case-insensitive substring. None means system default.

    Matching by name rather than index matters because Windows renumbers audio
    devices when things are plugged in or Bluetooth reconnects.
    """
    if not name:
        return None
    import sounddevice as sd

    needle = name.strip().lower()
    matches = []
    for index, device in enumerate(sd.query_devices()):
        channels = device["max_input_channels"] if input else device["max_output_channels"]
        if channels > 0 and needle in device["name"].lower():
            matches.append((index, device["name"]))

    if not matches:
        kind = "input" if input else "output"
        available = ", ".join(
            sorted(
                {
                    d["name"]
                    for d in sd.query_devices()
                    if (d["max_input_channels"] if input else d["max_output_channels"]) > 0
                }
            )
        )
        raise AudioError(f"no {kind} device matching {name!r}. Available: {available}")

    if len(matches) > 1:
        log.debug("%r matched %d devices; using [%d] %s", name, len(matches), *matches[0])
    return matches[0][0]


class Microphone:
    """Continuous 16 kHz mono capture into a queue of frames."""

    def __init__(self, device: int | None = None) -> None:
        self.device = device
        self._queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=100)
        self._stream = None
        self._overflows = 0

    def _callback(self, indata, frames, time_info, status) -> None:
        if status and status.input_overflow:
            self._overflows += 1
            if self._overflows in (1, 10, 100):
                log.warning("microphone overflow x%d - audio may stutter", self._overflows)
        try:
            self._queue.put_nowait(indata[:, 0].copy())
        except queue.Full:
            # Dropping the oldest frame keeps latency bounded; a backlog is
            # worse than a gap because it delays every later detection.
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(indata[:, 0].copy())
            except queue.Empty:
                pass

    def __enter__(self) -> "Microphone":
        import sounddevice as sd

        try:
            self._stream = sd.InputStream(
                samplerate=SAMPLE_RATE,
                blocksize=FRAME_SAMPLES,
                channels=1,
                dtype="float32",
                device=self.device,
                callback=self._callback,
            )
            self._stream.start()
        except Exception as exc:
            raise AudioError(f"could not open the microphone: {exc}") from exc
        return self

    def __exit__(self, *exc) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    def frames(self, timeout: float = 1.0):
        """Yield 80 ms frames as they arrive."""
        while True:
            try:
                yield self._queue.get(timeout=timeout)
            except queue.Empty:
                yield None

    def drain(self) -> None:
        """Discard buffered audio - used after speaking so the assistant does
        not transcribe its own voice."""
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return


@dataclass(frozen=True)
class WakeModel:
    """Where a wake word's model comes from, resolved from what config said."""

    source: str     # the configured string
    model_arg: str  # what openWakeWord is handed: a bundled name or an absolute path
    key: str        # the key it files predictions under
    kind: str       # bundled | file | custom

    @property
    def path(self) -> Path | None:
        return None if self.kind == "bundled" else Path(self.model_arg)


_MODEL_SUFFIXES = (".onnx", ".tflite")


def resolve_wake_word(value: str, wake_dir: str | Path = "") -> WakeModel:
    """Turn `voice.wake_word` into something openWakeWord can load.

    Three forms, in this order: a path to a model file; a bare name that is a
    file under `wake_dir` (where `wake install` and `wake train` put things);
    otherwise a bundled name. openWakeWord keys a file by its stem, so a
    custom `hey_athena.onnx` is `hey_athena` in every prediction dict, and
    the key is what the detector reads - not the string from config.

    A custom model under `wake_dir` beats a bundled one of the same name:
    whoever installed it there meant it.
    """
    value = (value or "").strip()
    if not value:
        raise AudioError("no wake word configured (voice.wake_word is empty)")

    looks_like_path = value.lower().endswith(_MODEL_SUFFIXES) or any(
        sep in value for sep in ("/", "\\")
    )
    if looks_like_path:
        path = Path(value).expanduser()
        if not path.is_file():
            raise AudioError(f"wake word model not found: {path}")
        path = path.resolve()
        return WakeModel(value, str(path), path.stem, "file")

    if wake_dir:
        candidate = Path(wake_dir).expanduser() / f"{value}.onnx"
        if candidate.is_file():
            candidate = candidate.resolve()
            log.info("wake word %r is the custom model %s", value, candidate)
            return WakeModel(value, str(candidate), candidate.stem, "custom")

    return WakeModel(value, value, value, "bundled")


def bundled_wake_words() -> list[str]:
    """The pretrained models openWakeWord ships and can load as ONNX."""
    try:
        import openwakeword
    except ImportError:
        return []
    names = []
    for name, entry in openwakeword.MODELS.items():
        onnx = str(entry["model_path"]).replace(".tflite", ".onnx")
        if os.path.exists(onnx):
            names.append(name)
    return sorted(names)


class WakeWordDetector:
    """openWakeWord, with two stricter gates in front of its raw score.

    The raw model fires the moment one 80 ms frame crosses the threshold, and
    ordinary speech clears 0.5 for a single frame surprisingly often - which
    is a wake word that fires on nobody saying it. So:

    * **patience** - the score has to stay over the threshold for N frames in
      a row. A real "hey jarvis" holds it up for several; a fluke holds it
      for one.
    * **voice activity** - Silero VAD (bundled with openWakeWord) has to have
      heard speech in the half second before the frame, or the score is
      zeroed. Non-speech noise never gets a vote.

    A run that crossed the threshold but not for long enough is logged, so a
    wake word that stops firing when it should can be tuned from the log
    rather than by feel.
    """

    def __init__(
        self,
        wake_word: str = "hey_jarvis",
        threshold: float = 0.5,
        *,
        patience: int = 1,
        vad_threshold: float = 0.0,
        wake_dir: str | Path = "",
    ) -> None:
        from openwakeword.model import Model

        self.wake_word = wake_word
        self.model = resolve_wake_word(wake_word, wake_dir)
        # What the model files its scores under - the stem for a file, the
        # name for a bundled model. Never the raw config string.
        self.model_key = self.model.key
        self.threshold = threshold
        self.patience = max(1, int(patience))
        self.vad_threshold = max(0.0, float(vad_threshold))
        # The score behind the most recent triggered() call, for the log.
        self.last_score = 0.0
        self._run = 0
        self._peak = 0.0

        options = {"inference_framework": "onnx", "vad_threshold": self.vad_threshold}
        try:
            self._model = Model(wakeword_models=[self.model.model_arg], **options)
        except Exception as exc:
            if self.model.kind != "bundled":
                # A custom file that will not open is a broken file, and
                # quietly listening for every bundled word instead would be
                # the wrong assistant answering to the wrong name.
                raise AudioError(
                    f"could not load wake word model {self.model.model_arg}: {exc}"
                ) from exc
            # Fall back to loading every bundled model, then select ours.
            self._model = Model(**options)
        if self.model_key not in self._model.models:
            available = ", ".join(sorted(self._model.models))
            raise AudioError(f"wake word {wake_word!r} not available. Have: {available}")

    def score(self, frame: np.ndarray) -> float:
        # openWakeWord expects int16 samples.
        pcm = (np.clip(frame, -1.0, 1.0) * 32767).astype(np.int16)
        predictions = self._model.predict(pcm)
        # The returned score, not the buffer: the VAD gate is applied to what
        # predict() hands back, and the buffer keeps the ungated value.
        try:
            return float(predictions[self.model_key])
        except (KeyError, TypeError):
            return float(self._model.prediction_buffer[self.model_key][-1])

    def triggered(self, frame: np.ndarray) -> bool:
        score = self.score(frame)
        self.last_score = score
        if score >= self.threshold:
            self._run += 1
            self._peak = max(self._peak, score)
            if self._run >= self.patience:
                self._run = 0
                self._peak = 0.0
                return True
            return False
        if self._run:
            log.info(
                "ignored a wake-word candidate: score peaked at %.2f for %d frame(s), "
                "need %d in a row at %.2f",
                self._peak, self._run, self.patience, self.threshold,
            )
            self._run = 0
            self._peak = 0.0
        return False

    def reset(self) -> None:
        """Clear internal buffers so the previous utterance cannot re-trigger."""
        self._run = 0
        self._peak = 0.0
        try:
            self._model.reset()
        except Exception:
            for buffer in self._model.prediction_buffer.values():
                buffer.clear()


class UtteranceRecorder:
    """Records until the speaker stops, then returns the audio.

    Keeps a short pre-roll so the first syllable after the wake word is not
    clipped, which is a common cause of a dropped first word.
    """

    PREROLL_FRAMES = 5  # 400 ms

    def __init__(
        self,
        *,
        silence_threshold: float = 0.010,
        silence_timeout: float = 1.1,
        max_seconds: float = 12.0,
        min_seconds: float = 0.4,
    ) -> None:
        self.silence_threshold = silence_threshold
        self.silence_timeout = silence_timeout
        self.max_seconds = max_seconds
        self.min_seconds = min_seconds
        self._preroll: deque[np.ndarray] = deque(maxlen=self.PREROLL_FRAMES)

    def observe(self, frame: np.ndarray) -> None:
        """Feed frames while idle so pre-roll is always warm."""
        self._preroll.append(frame)

    def record(self, mic: Microphone, cancel: threading.Event | None = None) -> np.ndarray | None:
        collected: list[np.ndarray] = list(self._preroll)
        frame_seconds = FRAME_SAMPLES / SAMPLE_RATE
        silent_for = 0.0
        total = 0.0
        heard_speech = False

        for frame in mic.frames(timeout=1.0):
            if cancel is not None and cancel.is_set():
                return None
            if frame is None:
                continue

            collected.append(frame)
            total += frame_seconds

            rms = float(np.sqrt(np.mean(frame**2)))
            if rms >= self.silence_threshold:
                heard_speech = True
                silent_for = 0.0
            else:
                silent_for += frame_seconds

            # Only treat silence as the end once something was actually said,
            # otherwise a pause before speaking ends the recording immediately.
            if heard_speech and silent_for >= self.silence_timeout:
                break
            if total >= self.max_seconds:
                log.info("utterance hit the %.0fs limit", self.max_seconds)
                break

        self._preroll.clear()
        if not heard_speech:
            return None

        audio = np.concatenate(collected) if collected else np.array([], dtype=np.float32)
        if len(audio) / SAMPLE_RATE < self.min_seconds:
            return None
        return audio
