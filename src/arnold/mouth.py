"""Turning speech audio into a mouth shape.

All of the DSP happens here, in the audio thread, once per chunk. The renderer
does an interpolation and nothing else - no FFT, no filtering, no allocation
per frame.

Two bands rather than one overall level, because a single amplitude gives a
blob that bounces. Energy below `low_crossover_hz` is what vowels are made of
and drives how far the jaw drops; energy above `high_crossover_hz` is where
sibilants and fricatives live and drives how far the mouth spreads. An "ee" or
an "s" is wide and thin, an "ah" is tall and round, and they move
independently.

Tuning lives at the top so it can be adjusted from the WAV harness
(`arnold mouth-test some.wav`) without a conversation.
"""

from __future__ import annotations

from dataclasses import dataclass

try:  # only the audio side needs it; the renderer imports the tuning alone
    import numpy as np
except ImportError:  # pragma: no cover
    np = None  # type: ignore[assignment]


@dataclass(frozen=True)
class MouthTuning:
    # -- envelope -----------------------------------------------------------
    # Analysis hop. 20 ms is 50 values a second, comfortably above the rate at
    # which a mouth actually changes shape.
    hop_ms: float = 20.0
    # Fast enough that a plosive snaps the mouth open on the frame it happens.
    attack_ms: float = 25.0
    # Slow enough that the mouth does not strobe shut between syllables.
    release_ms: float = 120.0
    # A resting mouth is slightly parted, not clamped shut.
    min_openness: float = 0.10
    max_openness: float = 1.0
    # Below this fraction of the utterance peak, treat the frame as a gap.
    silence_gate: float = 0.06

    # -- bands --------------------------------------------------------------
    low_crossover_hz: float = 1000.0
    high_crossover_hz: float = 3000.0
    # Scales the low band into jaw opening before clamping. Just over 1,
    # because the level is already normalised against this utterance's own
    # peak - the headroom is so that ordinary speech reaches a full open
    # mouth without every syllable slamming into the clamp.
    open_gain: float = 1.15
    # Scales the sibilant fraction into mouth spread. Measured over real
    # speech, high/total sits around 0.13 for vowels and reaches 0.75 on an
    # /s/, so this puts vowels near the middle and sibilants at full width.
    width_gain: float = 3.2
    # Mouth shape when there is no sound to judge it by.
    rest_width: float = 0.35

    # -- normalisation ------------------------------------------------------
    # The reference peak decays so a quiet reply still animates fully, rather
    # than being scaled against a shout from a minute ago. At a 20 ms hop
    # this halves in about a second: slow enough to hold a reference across
    # a pause between words, fast enough to follow a change in delivery.
    peak_decay_per_frame: float = 0.99
    # Never divide by a reference smaller than this (silence is not loud).
    peak_floor: float = 1e-4

    @property
    def hop_seconds(self) -> float:
        return self.hop_ms / 1000.0


TUNING = MouthTuning()

# Bands published alongside the mouth values, for the equaliser display.
EQ_BANDS = 16


class MouthAnalyser:
    """Per-chunk DSP: PCM in, mouth shape out.

    Stateful only in the loudness reference, which is what makes the
    normalisation per-utterance rather than absolute. Call `reset()` between
    utterances.
    """

    def __init__(self, tuning: MouthTuning = TUNING) -> None:
        if np is None:  # pragma: no cover
            raise RuntimeError("numpy is required to analyse audio")
        self.tuning = tuning
        self._peak = 0.0
        self._window_cache: dict[int, "np.ndarray"] = {}

    def reset(self) -> None:
        """Start a new utterance: forget how loud the last one was."""
        self._peak = 0.0

    def _window(self, size: int):
        """Hann windows are cached - allocating one per chunk is the kind of
        per-frame garbage this is supposed to avoid."""
        cached = self._window_cache.get(size)
        if cached is None:
            cached = np.hanning(size)
            # A handful of distinct chunk sizes at most; bound it anyway.
            if len(self._window_cache) < 8:
                self._window_cache[size] = cached
        return cached

    def analyse(self, pcm, rate: int) -> dict[str, object]:
        """One chunk of int16 PCM to `{bands, open, wide}`.

        `open` and `wide` are raw targets: the renderer applies attack and
        release to them, because that smoothing has to run at frame rate to be
        frame-rate independent.
        """
        tuning = self.tuning
        samples = pcm.astype(np.float32) / 32768.0
        if len(samples) < 32:
            return {"bands": [0.0] * EQ_BANDS, "open": 0.0, "wide": tuning.rest_width}

        spectrum = np.abs(np.fft.rfft(samples * self._window(len(samples))))
        freqs = np.fft.rfftfreq(len(samples), 1.0 / rate)

        # One pass, three slices. Skipping bin 0 drops DC offset, which would
        # otherwise read as a permanently open mouth.
        low = float(spectrum[(freqs > 0) & (freqs < tuning.low_crossover_hz)].sum())
        high = float(spectrum[freqs >= tuning.high_crossover_hz].sum())
        total = float(spectrum[freqs > 0].sum())

        # Track the loudest low-band energy recently seen, decaying, so the
        # scale follows this utterance rather than an absolute level.
        self._peak = max(low, self._peak * tuning.peak_decay_per_frame)
        reference = max(self._peak, tuning.peak_floor)

        loudness = low / reference
        if loudness < tuning.silence_gate:
            openness = 0.0
        else:
            openness = min(tuning.max_openness, loudness * tuning.open_gain)

        # Spread comes from how much of the energy is up in the sibilant
        # range, not from its absolute size - otherwise a loud vowel would
        # read as wide simply for being loud.
        #
        # Only judged on frames that have sound in them. In a gap the ratio is
        # measured against near-zero energy, so it swings between extremes and
        # the mouth flickers wide and narrow through every silence.
        if openness > 0.0 and total > tuning.peak_floor:
            wide = min(1.0, (high / total) * tuning.width_gain)
        else:
            wide = tuning.rest_width

        return {
            "bands": self._bands(spectrum),
            "open": round(float(openness), 4),
            "wide": round(float(wide), 4),
        }

    def _bands(self, spectrum) -> list[float]:
        """Log-spaced band levels, for the equaliser rather than the mouth."""
        edges = np.geomspace(1, max(2, len(spectrum) - 1), EQ_BANDS + 1).astype(int)
        levels = [
            float(spectrum[edges[i] : max(edges[i] + 1, edges[i + 1])].mean())
            for i in range(EQ_BANDS)
        ]
        peak = max(levels) or 1.0
        return [round(min(1.0, v / peak), 3) for v in levels]


def envelope_from_wav(pcm, rate: int, tuning: MouthTuning = TUNING) -> list[dict]:
    """Analyse a whole buffer up front, one hop at a time.

    Only possible when the audio exists before playback - a file, not a live
    stream. Used by the WAV harness so constants can be tuned offline.
    """
    if np is None:  # pragma: no cover
        raise RuntimeError("numpy is required to analyse audio")
    hop = max(32, int(rate * tuning.hop_seconds))
    analyser = MouthAnalyser(tuning)
    frames = []
    for start in range(0, max(1, len(pcm) - hop + 1), hop):
        frame = analyser.analyse(pcm[start : start + hop], rate)
        frame["at"] = start / rate
        frames.append(frame)
    return frames
