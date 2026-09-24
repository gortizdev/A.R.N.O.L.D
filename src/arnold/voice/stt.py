"""Speech to text via faster-whisper."""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

# Whisper hallucinates stock phrases when handed silence or noise. Anything
# that reduces to one of these is treated as nothing said.
_HALLUCINATIONS = {
    "you", "thank you.", "thanks for watching!", "thank you for watching!",
    "bye.", "bye bye.", ".", "!", "?", "so", "um", "uh",
    "please subscribe.", "subtitles by the amara.org community",
}


def _register_cuda_dlls() -> bool:
    """Put the pip-installed CUDA libraries where CTranslate2 can find them.

    The nvidia-*-cu12 wheels drop their DLLs under site-packages/nvidia/*/bin,
    which is not on the search path. CTranslate2's native loader resolves by
    PATH, so os.add_dll_directory alone is not enough - the directories have to
    be prepended to PATH before ctranslate2 is imported.
    """
    base = Path(sys.prefix) / "Lib" / "site-packages" / "nvidia"
    if not base.is_dir():
        return False
    dirs = [str(p) for p in base.glob("*/bin") if p.is_dir()]
    if not dirs:
        return False
    existing = os.environ.get("PATH", "")
    missing = [d for d in dirs if d not in existing]
    if missing:
        os.environ["PATH"] = os.pathsep.join(missing) + os.pathsep + existing
    for d in dirs:
        try:
            os.add_dll_directory(d)
        except (OSError, AttributeError):
            pass
    return True


class Transcriber:
    def __init__(
        self,
        model: str = "small.en",
        device: str = "auto",
        compute_type: str = "",
    ) -> None:
        self.model_name = model
        self._requested_device = device
        _register_cuda_dlls()

        from faster_whisper import WhisperModel

        candidates: list[tuple[str, str]] = []
        if device in ("auto", "cuda"):
            candidates.append(("cuda", compute_type or "float16"))
        if device in ("auto", "cpu"):
            candidates.append(("cpu", compute_type or "int8"))
        if not candidates:
            raise ValueError(f"stt_device must be auto, cuda or cpu (got {device!r})")

        last_error: Exception | None = None
        for dev, ctype in candidates:
            try:
                started = time.time()
                self._model = WhisperModel(model, device=dev, compute_type=ctype)
                self.device, self.compute_type = dev, ctype
                log.info(
                    "whisper %s loaded on %s/%s in %.1fs",
                    model, dev, ctype, time.time() - started,
                )
                break
            except Exception as exc:
                last_error = exc
                if dev == "cuda" and device == "auto":
                    log.warning(
                        "CUDA unavailable (%s); falling back to CPU, which is "
                        "roughly 30x slower. `uv pip install -e \".[cuda]\"` may fix it.",
                        str(exc)[:120],
                    )
                else:
                    log.error("could not load whisper on %s: %s", dev, exc)
        else:
            raise RuntimeError(f"no usable whisper backend: {last_error}")

        self._warm_up()

    def _warm_up(self) -> None:
        """First inference compiles kernels and can take seconds. Pay that cost
        at startup rather than on the user's first question."""
        started = time.time()
        self.transcribe(np.zeros(16000, dtype=np.float32))
        log.info("whisper warm-up took %.1fs", time.time() - started)

    def transcribe(self, audio: np.ndarray) -> str:
        segments, _ = self._model.transcribe(
            audio.astype(np.float32),
            beam_size=1,
            language="en",
            condition_on_previous_text=False,
            vad_filter=True,
        )
        text = " ".join(segment.text.strip() for segment in segments).strip()
        if text.lower().strip() in _HALLUCINATIONS:
            log.debug("discarding likely hallucination: %r", text)
            return ""
        return text
