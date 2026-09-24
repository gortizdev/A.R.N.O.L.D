"""Face animation state.

Keeps the visual state separate from where the information came from: sources
push `mode` and audio `levels`, this module turns them into the smoothly
interpolated values the renderer draws.

Two ideas do most of the work of making it look alive rather than merely
animated:

* **A spring, not a fade.** Mode changes fire an impulse into a damped spring,
  so the orb overshoots and settles instead of easing politely into place. Its
  velocity also drives squash-and-stretch, which is what sells the motion as
  physical.
* **Saccades, not drifting.** Real eyes hold still and then jump. The gaze
  snaps to a new target in ~60 ms and then holds, which reads as attention;
  a slow lerp between targets reads as a screensaver.

Everything is smoothed with `1 - exp(-rate * dt)` rather than a per-frame
fraction, so the animation looks identical at 30 fps and 120 fps.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from enum import Enum

from ..mouth import TUNING

EQ_BANDS = 16  # matches HudBroadcaster.BANDS on the Pi (assistant.py:328)


class FaceMode(str, Enum):
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"
    ALERT = "alert"
    OFFLINE = "offline"


@dataclass
class FaceState:
    """What the renderer draws this frame."""

    mode: FaceMode = FaceMode.IDLE
    t: float = 0.0
    # 0 = eyes open, 1 = fully shut
    blink: float = 0.0
    # 0..1 overall mouth openness
    level: float = 0.0
    # Per-band audio levels, 0..1
    eq: list[float] = field(default_factory=lambda: [0.0] * EQ_BANDS)
    # Eases 0->1 as a mode change settles, for cross-fading colours
    transition: float = 1.0
    previous_mode: FaceMode = FaceMode.IDLE
    # Where the eyes are looking, as a fraction of the orb radius
    gaze_x: float = 0.0
    gaze_y: float = 0.0

    # Orb size: breathing, plus the spring's overshoot on a mode change
    scale: float = 1.0
    # >0 tall and narrow, <0 wide and flat. Driven by the spring's velocity.
    squash: float = 0.0
    # Slow positional drift, as a fraction of the window, so it never sits dead still
    bob_x: float = 0.0
    bob_y: float = 0.0
    # 0..1 excitement, driving glow strength and rim brightness
    energy: float = 0.0
    # -1 furrowed, 0 neutral, +1 raised
    brow: float = 0.0
    # 1 -> 0 after a mode change, drawn as an expanding ring
    flash: float = 0.0

    # How far the mouth is open, 0 (shut) to 1 (a full "ah").
    mouth_open: float = 0.0
    # Mouth shape, 0 rounded like "oo" to 1 spread like "ee". Taken from where
    # the energy sits in the spectrum: bright sounds are made with a wide
    # mouth, dark ones with a rounded one.
    mouth_wide: float = 0.35


def _ease(x: float) -> float:
    """Smoothstep - no abrupt starts or stops."""
    x = max(0.0, min(1.0, x))
    return x * x * (3 - 2 * x)


def _approach(current: float, target: float, rate: float, dt: float) -> float:
    """Exponential smoothing that is independent of frame rate."""
    return current + (target - current) * (1.0 - math.exp(-rate * dt))


def _clamp(value: float, lo: float, hi: float) -> float:
    return lo if value < lo else hi if value > hi else value


class FaceAnimator:
    """Drives a FaceState forward in time."""

    # Seconds to cross-fade between palettes. Shorter than the spring, so the
    # colour has arrived by the time the motion settles.
    TRANSITION_SECONDS = 0.30

    # Mouth envelope. Attack is much quicker than release so speech looks
    # crisp on the way up and doesn't chatter on the way down. Shared with the
    # audio side, so both ends of the pipe can be tuned from one place.
    ATTACK_TAU = TUNING.attack_ms / 1000.0
    RELEASE_TAU = TUNING.release_ms / 1000.0

    # Damped spring behind every "pop". Underdamped on purpose: the overshoot
    # is the whole point.
    SPRING_K = 190.0
    SPRING_C = 13.0
    # Fixed integration step. Long frames are substepped rather than fed in
    # whole, which would let the spring go unstable.
    SPRING_STEP = 1.0 / 240.0

    # Impulse when entering each mode, in units of orb scale.
    POP = {
        FaceMode.IDLE: 0.55,
        FaceMode.LISTENING: 1.5,
        FaceMode.THINKING: 0.8,
        FaceMode.SPEAKING: 1.0,
        FaceMode.ALERT: 2.3,
        FaceMode.OFFLINE: 0.3,
    }
    # Modes that announce themselves with an expanding ring.
    FLASH_MODES = (FaceMode.LISTENING, FaceMode.ALERT)

    def __init__(self) -> None:
        self.state = FaceState()
        # Every timer runs off this clock, accumulated from the dt handed to
        # tick(), rather than the wall clock. That keeps the animation
        # frame-rate independent and lets tests step time explicitly.
        self._clock = 0.0
        self._mode_changed_at = 0.0

        self._blink_pending: list[float] = []
        self._blink_start: float | None = None
        self._next_blink = random.uniform(1.5, 4.0)

        self._gaze_target = (0.0, 0.0)
        self._next_gaze = random.uniform(0.8, 2.2)
        # A pointer hovering the orb overrides the idle saccades until it leaves.
        self._look: tuple[float, float] | None = None
        self._look_until = 0.0

        self._spring = 0.0
        self._spring_v = 0.0
        self._spring_carry = 0.0

        self._target_eq = [0.0] * EQ_BANDS
        self._has_levels = False
        self._external_mouth: tuple[float, float] | None = None
        self._c_mean = self.CENTROID_MEAN
        self._c_dev = self.CENTROID_DEV

    # -- inputs -------------------------------------------------------------

    def set_mode(self, mode: FaceMode) -> None:
        if mode == self.state.mode:
            return
        self.state.previous_mode = self.state.mode
        self.state.mode = mode
        self.state.transition = 0.0
        self._mode_changed_at = self._clock
        self._spring_v += self.POP.get(mode, 0.6)
        if mode in self.FLASH_MODES:
            self.state.flash = 1.0
        # Look straight ahead when addressed, away when working.
        if mode is FaceMode.LISTENING:
            self._gaze_target = (0.0, 0.0)
            self._next_gaze = self._clock + random.uniform(1.4, 2.6)

    def set_levels(self, levels: list[float]) -> None:
        """Feed audio-band levels (0..1). Short lists are padded, long ones binned.

        An empty list means "no audio source"; a list of zeros means "this
        source is silent right now". They must not be confused: the first
        falls back to a synthesised talking envelope, and the second has to
        shut the mouth.
        """
        self._has_levels = bool(levels)
        if not levels:
            self._target_eq = [0.0] * EQ_BANDS
            return
        if len(levels) == EQ_BANDS:
            self._target_eq = [_clamp(float(v), 0.0, 1.0) for v in levels]
            return
        # Resample to EQ_BANDS so a bridge sending a different band count works.
        out: list[float] = []
        for i in range(EQ_BANDS):
            lo = int(i * len(levels) / EQ_BANDS)
            hi = max(lo + 1, int((i + 1) * len(levels) / EQ_BANDS))
            chunk = levels[lo:hi] or [0.0]
            out.append(_clamp(float(sum(chunk) / len(chunk)), 0.0, 1.0))
        self._target_eq = out

    # How far the eyes travel at full deflection, as a fraction of the radius.
    LOOK_REACH = 0.20

    def set_mouth(self, shape: tuple[float, float] | None) -> None:
        """Take a mouth shape worked out from the raw audio, if there is one.

        The source that has the PCM can measure the bands properly and, more
        importantly, time the message to when that audio is actually heard.
        When it does, its answer is better than anything derivable here from
        16 summarised levels, so it wins.
        """
        self._external_mouth = shape

    def look_at(self, dx: float, dy: float) -> None:
        """Track a point, as a fraction of the orb radius from its centre.

        Used for the mouse pointer: being followed by the eyes is the cheapest
        possible proof that the thing on screen is running rather than a
        picture of itself.

        Clamped by magnitude rather than per axis. Clamping each axis
        separately would drag any distant point towards the nearest diagonal,
        so the eyes would end up looking at a corner instead of at the mouse.
        """
        distance = math.hypot(dx, dy)
        if distance > self.LOOK_REACH:
            scale = self.LOOK_REACH / distance
            dx, dy = dx * scale, dy * scale
        self._look = (dx, dy)
        self._look_until = self._clock + 1.2

    def nudge(self, strength: float = 1.2) -> None:
        """Poke the spring - used when the orb is clicked."""
        self._spring_v += strength

    # -- clock --------------------------------------------------------------

    def tick(self, dt: float) -> FaceState:
        dt = max(0.0, dt)
        self._clock += dt
        now = self._clock
        st = self.state
        st.t = now

        elapsed = now - self._mode_changed_at
        st.transition = _ease(min(1.0, elapsed / self.TRANSITION_SECONDS))

        self._tick_blink(now)
        self._tick_gaze(now, dt)
        self._tick_mouth(dt)
        self._tick_body(now, dt)
        return st

    # -- eyes ---------------------------------------------------------------

    BLINK_SECONDS = 0.13

    def _tick_blink(self, now: float) -> None:
        st = self.state
        if st.mode == FaceMode.OFFLINE:
            st.blink = 0.85  # eyes almost shut when there is nothing to report
            self._blink_start = None
            return

        if now >= self._next_blink:
            self._blink_pending.append(now)
            # Occasional double-blink. Perfectly regular blinking is one of the
            # things that makes an animated face look mechanical.
            if random.random() < 0.22:
                self._blink_pending.append(now + 0.21)
            gap = {
                # Listening blinks less - it reads as attentiveness.
                FaceMode.LISTENING: (4.0, 9.0),
                FaceMode.ALERT: (1.0, 2.4),
                FaceMode.THINKING: (1.8, 4.5),
            }.get(st.mode, (2.2, 6.0))
            self._next_blink = now + random.uniform(*gap)

        if self._blink_start is None and self._blink_pending and now >= self._blink_pending[0]:
            self._blink_start = self._blink_pending.pop(0)

        if self._blink_start is None:
            st.blink = 0.0
            return

        phase = (now - self._blink_start) / self.BLINK_SECONDS
        if phase >= 1.0:
            st.blink = 0.0
            self._blink_start = None
        elif phase < 0.4:
            st.blink = _ease(phase / 0.4)  # snaps shut
        else:
            st.blink = _ease(1.0 - (phase - 0.4) / 0.6)  # opens more slowly

    def _tick_gaze(self, now: float, dt: float) -> None:
        st = self.state

        if self._look is not None and now < self._look_until:
            target = self._look
            rate = 11.0
        else:
            self._look = None
            if now >= self._next_gaze:
                if st.mode == FaceMode.THINKING:
                    # Eyes up and away, the universal look of working it out.
                    target = (random.uniform(-0.13, 0.13), random.uniform(-0.11, -0.02))
                    hold = random.uniform(0.5, 1.3)
                elif st.mode == FaceMode.LISTENING:
                    # Mostly straight at whoever is talking.
                    target = (
                        (0.0, 0.0)
                        if random.random() < 0.55
                        else (random.uniform(-0.07, 0.07), random.uniform(-0.05, 0.05))
                    )
                    hold = random.uniform(1.0, 2.4)
                elif st.mode == FaceMode.ALERT:
                    target = (random.uniform(-0.09, 0.09), random.uniform(-0.04, 0.06))
                    hold = random.uniform(0.3, 0.8)
                else:
                    target = (random.uniform(-0.11, 0.11), random.uniform(-0.09, 0.09))
                    hold = random.uniform(0.9, 2.6)
                self._gaze_target = target
                self._next_gaze = now + hold
            target = self._gaze_target
            # Fast enough to read as a jump rather than a glide.
            rate = 17.0

        st.gaze_x = _approach(st.gaze_x, target[0], rate, dt)
        st.gaze_y = _approach(st.gaze_y, target[1], rate, dt)

    # -- mouth --------------------------------------------------------------

    # Listening moves the mouth a little - attentive, not mouthing along with
    # whoever is talking, which is unsettling.
    LISTENING_GAIN = 0.22
    # Vowel shape changes more slowly than loudness does.
    SHAPE_TAU = 0.09

    # Where the spectral centroid of ordinary speech sits. Measured over four
    # seconds of synthesised British speech through the same band splitting
    # the session publishes: the voiced frames land between 0.30 and 0.37,
    # with sibilants alone reaching 0.68.
    #
    # Those bounds are only a starting point. A different voice - and the one
    # actually speaking is OpenAI's, not the one measured - sits somewhere
    # else, and hard-coding this range would leave its mouth stuck at one
    # shape. So the range tracks whatever is being spoken, and drifts back
    # together so that a single sibilant does not widen it permanently.
    CENTROID_MEAN = 0.34
    CENTROID_DEV = 0.02
    # Seconds for the range to settle on a new voice.
    CENTROID_TAU = 1.5
    # Deviations either side of the mean that span the full width of the
    # mouth. Wider than 1 so ordinary vowels do not slam into both stops.
    CENTROID_WIDTH = 2.2
    CENTROID_MIN_SPREAD = 0.006
    # Below this the frame is silence, and its centroid is meaningless.
    VOICED_LEVEL = 0.25

    def _tick_mouth(self, dt: float) -> None:
        st = self.state

        if st.mode == FaceMode.SPEAKING:
            # With no audio source at all, synthesise a plausible envelope so
            # the face still looks like it is talking. Note this is keyed on
            # having a source, not on the source being loud - real silence
            # from a real source must close the mouth.
            if self._has_levels:
                target = self._target_eq
            else:
                target = [
                    0.25
                    + 0.55
                    * abs(math.sin(st.t * 7.5 + i * 0.55))
                    * (0.55 + 0.45 * math.sin(st.t * 2.1))
                    for i in range(EQ_BANDS)
                ]
        elif st.mode == FaceMode.LISTENING:
            # Follow the mic if the source gives us levels, otherwise a shallow
            # ripple: present, but clearly not speech.
            if self._has_levels:
                target = [0.06 + 0.55 * v for v in self._target_eq]
            else:
                target = [0.10 + 0.06 * math.sin(st.t * 3.0 + i * 0.4) for i in range(EQ_BANDS)]
        else:
            target = [0.0] * EQ_BANDS

        rising = 1.0 - math.exp(-dt / self.ATTACK_TAU)
        falling = 1.0 - math.exp(-dt / self.RELEASE_TAU)
        for i in range(EQ_BANDS):
            st.eq[i] += (target[i] - st.eq[i]) * (rising if target[i] > st.eq[i] else falling)

        st.level = sum(st.eq) / len(st.eq)
        self._tick_lips(dt)

    def _tick_lips(self, dt: float) -> None:
        """Turn the spectrum into a jaw opening and a mouth shape.

        Loudness drives how far the jaw drops; where that loudness sits in the
        spectrum drives whether the mouth is rounded or spread. It is not real
        phoneme recognition - that would need the text, which never exists on
        this machine - but it tracks the same things a mouth does, so it reads
        as speech rather than as a meter.
        """
        st = self.state
        bands = st.eq
        total = sum(bands)

        external = self._external_mouth

        # The loudest band, not the average of all of them. A vowel puts nearly
        # all its energy in two or three bands, so the mean is tiny however
        # loudly it is sung, and averaging would leave the jaw almost shut for
        # every sound that is not broadband noise.
        loudness = external[0] if external else (max(bands) if bands else 0.0)
        gain = 1.0 if external else 1.15

        if st.mode == FaceMode.SPEAKING:
            open_target = min(1.0, loudness * gain)
            if open_target > 0.02:
                open_target = max(TUNING.min_openness, open_target)
        elif st.mode == FaceMode.LISTENING:
            open_target = min(1.0, loudness * gain) * self.LISTENING_GAIN
        else:
            open_target = 0.0

        if external:
            wide_target = external[1]
        elif total > 1e-4:
            # Spectral centroid, 0 at the bottom band and 1 at the top.
            centroid = sum(i * v for i, v in enumerate(bands)) / (total * (len(bands) - 1))
            wide_target = self._shape_from(centroid, loudness, dt)
        else:
            wide_target = TUNING.rest_width

        # The jaw drops faster than it closes, like a real one.
        tau = self.ATTACK_TAU if open_target > st.mouth_open else self.RELEASE_TAU
        st.mouth_open += (open_target - st.mouth_open) * (1.0 - math.exp(-dt / tau))
        st.mouth_wide += (wide_target - st.mouth_wide) * (
            1.0 - math.exp(-dt / self.SHAPE_TAU)
        )

    def _shape_from(self, centroid: float, loudness: float, dt: float) -> float:
        """Map this frame's brightness onto 0 (rounded) .. 1 (spread).

        The range follows the voice as a running mean and spread rather than a
        running minimum and maximum. Min and max are set by the extremes, so a
        single /s/ - which sits far brighter than any vowel - stretches the
        scale once and leaves every vowel afterwards squashed against the
        bottom of it. A mean barely moves for one outlier.
        """
        if loudness > self.VOICED_LEVEL:
            alpha = 1.0 - math.exp(-dt / self.CENTROID_TAU)
            self._c_mean += (centroid - self._c_mean) * alpha
            self._c_dev += (abs(centroid - self._c_mean) - self._c_dev) * alpha

        spread = max(self.CENTROID_MIN_SPREAD, self._c_dev) * self.CENTROID_WIDTH
        return _clamp(0.5 + (centroid - self._c_mean) / (2 * spread), 0.0, 1.0)

    # -- body ---------------------------------------------------------------

    def _tick_body(self, now: float, dt: float) -> None:
        st = self.state
        self._step_spring(dt)

        breathe = 0.014 * math.sin(now * 1.35)
        pulse = 0.06 * st.level if st.mode == FaceMode.SPEAKING else 0.0
        st.scale = _clamp(1.0 + breathe + pulse + self._spring, 0.86, 1.12)
        # Moving outward stretches the orb, settling back squashes it.
        st.squash = _clamp(self._spring_v * 0.011, -0.13, 0.13)

        sway = 1.7 if st.mode == FaceMode.THINKING else 1.0
        st.bob_x = sway * (0.011 * math.sin(now * 0.53) + 0.006 * math.sin(now * 0.91 + 1.2))
        st.bob_y = 0.009 * math.sin(now * 0.67 + 0.4) + 0.005 * math.sin(now * 1.13)

        st.brow = _approach(st.brow, self._brow_target(now), 8.0, dt)
        st.energy = _approach(st.energy, self._energy_target(now), 9.0, dt)
        st.flash = _approach(st.flash, 0.0, 3.4, dt)
        if st.flash < 0.005:
            st.flash = 0.0

    def _step_spring(self, dt: float) -> None:
        """Integrate the pop spring at a fixed step, carrying the remainder."""
        self._spring_carry += dt
        # Bounded so a stalled frame cannot spend a second catching up.
        steps = min(int(self._spring_carry / self.SPRING_STEP), 64)
        self._spring_carry -= steps * self.SPRING_STEP
        h = self.SPRING_STEP
        for _ in range(steps):
            self._spring_v += (-self.SPRING_K * self._spring - self.SPRING_C * self._spring_v) * h
            self._spring += self._spring_v * h
        if abs(self._spring) < 1e-4 and abs(self._spring_v) < 1e-4:
            self._spring = self._spring_v = 0.0

    def _brow_target(self, now: float) -> float:
        st = self.state
        if st.mode == FaceMode.LISTENING:
            return 0.55 + 0.08 * math.sin(now * 1.9)
        if st.mode == FaceMode.THINKING:
            return -0.40 + 0.14 * math.sin(now * 1.1)
        if st.mode == FaceMode.SPEAKING:
            return 0.12 + 0.5 * min(1.0, st.level * 1.8)
        if st.mode == FaceMode.ALERT:
            return -0.85
        if st.mode == FaceMode.OFFLINE:
            return -0.15
        return 0.05 * math.sin(now * 0.7)

    def _energy_target(self, now: float) -> float:
        st = self.state
        if st.mode == FaceMode.LISTENING:
            # Lit well above idle from the first frame, before anyone speaks:
            # the open mic has to be obvious in a silent room.
            return 0.62 + 0.3 * min(1.0, st.level * 2.0)
        if st.mode == FaceMode.THINKING:
            return 0.34 + 0.30 * (0.5 + 0.5 * math.sin(now * 3.1))
        if st.mode == FaceMode.SPEAKING:
            return 0.45 + 0.55 * min(1.0, st.level * 1.7)
        if st.mode == FaceMode.ALERT:
            return 0.60 + 0.40 * (0.5 + 0.5 * math.sin(now * 6.5))
        if st.mode == FaceMode.OFFLINE:
            return 0.05
        return 0.14 + 0.07 * (0.5 + 0.5 * math.sin(now * 1.3))
