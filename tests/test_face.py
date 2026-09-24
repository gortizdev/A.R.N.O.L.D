"""Face animation.

The animator advances on the dt handed to tick(), never the wall clock, so
these step time explicitly rather than sleeping.
"""

import math

import pytest

from arnold.face.holo import HoloRenderer
from arnold.face.render import TRANSPARENT_KEY, FaceRenderer, Theme, lerp
from arnold.face.state import EQ_BANDS, FaceAnimator, FaceMode


def advance(animator: FaceAnimator, seconds: float, fps: int = 60) -> None:
    for _ in range(int(seconds * fps)):
        animator.tick(1.0 / fps)


class TestTransitions:
    def test_starts_idle(self):
        assert FaceAnimator().state.mode is FaceMode.IDLE

    def test_mode_change_restarts_the_transition(self):
        a = FaceAnimator()
        advance(a, 1.0)
        a.set_mode(FaceMode.SPEAKING)
        assert a.state.transition == 0.0
        assert a.state.previous_mode is FaceMode.IDLE

    def test_transition_completes(self):
        a = FaceAnimator()
        a.set_mode(FaceMode.ALERT)
        advance(a, 1.0)
        assert a.state.transition == pytest.approx(1.0)

    def test_transition_is_partway_through_midway(self):
        a = FaceAnimator()
        a.set_mode(FaceMode.ALERT)
        advance(a, FaceAnimator.TRANSITION_SECONDS / 2)
        assert 0.0 < a.state.transition < 1.0

    def test_setting_the_same_mode_does_not_restart(self):
        a = FaceAnimator()
        a.set_mode(FaceMode.SPEAKING)
        advance(a, 1.0)
        a.set_mode(FaceMode.SPEAKING)
        assert a.state.transition == pytest.approx(1.0)

    def test_clock_is_independent_of_frame_rate(self):
        slow, fast = FaceAnimator(), FaceAnimator()
        advance(slow, 2.0, fps=10)
        advance(fast, 2.0, fps=120)
        assert slow.state.t == pytest.approx(fast.state.t, abs=0.05)


class TestLevels:
    def test_exact_band_count_passes_through(self):
        a = FaceAnimator()
        a.set_mode(FaceMode.SPEAKING)
        a.set_levels([1.0] * EQ_BANDS)
        advance(a, 1.0)
        assert a.state.level > 0.5

    def test_levels_are_resampled(self):
        a = FaceAnimator()
        a.set_levels([1.0] * 4)
        assert len(a._target_eq) == EQ_BANDS
        assert all(v == pytest.approx(1.0) for v in a._target_eq)

    def test_levels_are_clamped(self):
        a = FaceAnimator()
        a.set_levels([5.0, -3.0] * (EQ_BANDS // 2))
        assert all(0.0 <= v <= 1.0 for v in a._target_eq)

    def test_empty_levels_are_safe(self):
        a = FaceAnimator()
        a.set_levels([])
        assert a._target_eq == [0.0] * EQ_BANDS

    def test_mouth_closes_when_not_speaking(self):
        a = FaceAnimator()
        a.set_mode(FaceMode.SPEAKING)
        a.set_levels([1.0] * EQ_BANDS)
        advance(a, 1.0)
        a.set_mode(FaceMode.IDLE)
        a.set_levels([])
        advance(a, 2.0)
        assert a.state.level < 0.02

    def test_speaking_animates_without_audio_data(self):
        """No bridge installed still has to look like talking."""
        a = FaceAnimator()
        a.set_mode(FaceMode.SPEAKING)
        advance(a, 1.0)
        assert a.state.level > 0.05

    def test_idle_mouth_is_still(self):
        a = FaceAnimator()
        advance(a, 3.0)
        assert a.state.level == pytest.approx(0.0, abs=1e-3)


class TestBlink:
    def test_blink_stays_in_range(self):
        a = FaceAnimator()
        for _ in range(60 * 30):
            st = a.tick(1 / 60)
            assert 0.0 <= st.blink <= 1.0

    def test_offline_keeps_eyes_nearly_shut(self):
        a = FaceAnimator()
        a.set_mode(FaceMode.OFFLINE)
        advance(a, 1.0)
        assert a.state.blink > 0.5

    def test_gaze_stays_subtle(self):
        a = FaceAnimator()
        for _ in range(60 * 30):
            st = a.tick(1 / 60)
            assert abs(st.gaze_x) < 0.2 and abs(st.gaze_y) < 0.2


class TestPop:
    """A mode change fires an impulse into a damped spring."""

    def test_entering_a_mode_overshoots_then_settles(self):
        a = FaceAnimator()
        a.set_mode(FaceMode.LISTENING)
        peak = max(a.tick(1 / 60).scale for _ in range(30))
        assert peak > 1.02
        advance(a, 3.0)
        # Back to breathing, which is small.
        assert a.state.scale == pytest.approx(1.0, abs=0.02)

    def test_alert_pops_harder_than_idle(self):
        def peak(mode):
            a = FaceAnimator()
            a.set_mode(mode)
            return max(a.tick(1 / 60).scale for _ in range(40))

        assert peak(FaceMode.ALERT) > peak(FaceMode.IDLE)

    def test_scale_stays_sane_under_repeated_pokes(self):
        """Clicking the orb repeatedly must not fling it off the spring."""
        a = FaceAnimator()
        for _ in range(200):
            a.nudge(2.0)
            st = a.tick(1 / 60)
            assert 0.8 < st.scale < 1.2
            assert abs(st.squash) <= 0.14

    def test_spring_is_stable_across_a_long_stall(self):
        a = FaceAnimator()
        a.set_mode(FaceMode.ALERT)
        for _ in range(20):
            st = a.tick(0.25)  # the dt cap the window applies
            assert 0.8 < st.scale < 1.2

    def test_flash_fires_on_listening_and_decays(self):
        a = FaceAnimator()
        a.set_mode(FaceMode.LISTENING)
        assert a.state.flash == 1.0
        advance(a, 3.0)
        assert a.state.flash == 0.0

    def test_idle_does_not_flash(self):
        a = FaceAnimator()
        a.set_mode(FaceMode.SPEAKING)
        advance(a, 3.0)
        a.set_mode(FaceMode.IDLE)
        assert a.state.flash == 0.0


class TestExpression:
    def test_alert_furrows_and_listening_raises(self):
        def brow(mode):
            a = FaceAnimator()
            a.set_mode(mode)
            advance(a, 1.5)
            return a.state.brow

        assert brow(FaceMode.ALERT) < -0.5
        assert brow(FaceMode.LISTENING) > 0.4

    def test_energy_tracks_the_mode(self):
        def energy(mode):
            a = FaceAnimator()
            a.set_mode(mode)
            advance(a, 2.0)
            return a.state.energy

        assert energy(FaceMode.OFFLINE) < energy(FaceMode.IDLE) < energy(FaceMode.ALERT)

    def test_pointer_tracking_overrides_the_idle_gaze(self):
        a = FaceAnimator()
        a.look_at(0.15, -0.12)
        advance(a, 0.5)
        assert a.state.gaze_x == pytest.approx(0.15, abs=0.02)
        assert a.state.gaze_y == pytest.approx(-0.12, abs=0.02)

    def test_pointer_tracking_expires(self):
        a = FaceAnimator()
        a.look_at(0.16, 0.16)
        advance(a, 6.0)
        assert a._look is None

    def test_look_at_is_clamped(self):
        a = FaceAnimator()
        a.look_at(9.0, -9.0)
        advance(a, 1.0)
        assert abs(a.state.gaze_x) < 0.2 and abs(a.state.gaze_y) < 0.2

    def test_listening_mouth_follows_real_levels(self):
        quiet, loud = FaceAnimator(), FaceAnimator()
        for a, v in ((quiet, 0.05), (loud, 0.95)):
            a.set_mode(FaceMode.LISTENING)
            a.set_levels([v] * EQ_BANDS)
            advance(a, 1.0)
        assert loud.state.level > quiet.state.level * 2


class TestMouth:
    """The mouth is a mouth, not a level meter: a jaw opening plus a shape."""

    def speak(self, bands, seconds=0.6):
        a = FaceAnimator()
        a.set_mode(FaceMode.SPEAKING)
        a.set_levels(bands)
        advance(a, seconds)
        return a.state

    def test_loud_speech_opens_the_jaw(self):
        assert self.speak([0.95] * EQ_BANDS).mouth_open > 0.6

    def test_silence_shuts_it(self):
        a = FaceAnimator()
        a.set_mode(FaceMode.SPEAKING)
        a.set_levels([0.9] * EQ_BANDS)
        advance(a, 1.0)
        a.set_mode(FaceMode.IDLE)
        a.set_levels([])
        advance(a, 2.0)
        assert a.state.mouth_open < 0.02

    def test_speaking_keeps_a_floor_so_it_does_not_chew(self):
        """Between syllables the mouth stays slightly open, as a real one does."""
        state = self.speak([0.06] * EQ_BANDS)
        assert 0.05 < state.mouth_open <= 0.3

    def test_bright_sound_spreads_the_mouth(self):
        """Energy high in the spectrum is made with a wide mouth."""
        low = [0.9] * 4 + [0.0] * (EQ_BANDS - 4)
        high = [0.0] * (EQ_BANDS - 4) + [0.9] * 4
        assert self.speak(high).mouth_wide > self.speak(low).mouth_wide + 0.3

    def test_shape_stays_in_range_on_any_input(self):
        a = FaceAnimator()
        a.set_mode(FaceMode.SPEAKING)
        for i in range(400):
            a.set_levels([(i % 7) / 6.0] * EQ_BANDS if i % 3 else [])
            st = a.tick(1 / 60)
            assert 0.0 <= st.mouth_open <= 1.0
            assert 0.0 <= st.mouth_wide <= 1.0

    def test_listening_moves_the_mouth_far_less_than_speaking(self):
        """Mouthing along while the user talks would be unsettling."""
        listening = FaceAnimator()
        listening.set_mode(FaceMode.LISTENING)
        listening.set_levels([0.95] * EQ_BANDS)
        advance(listening, 0.6)
        assert listening.state.mouth_open < self.speak([0.95] * EQ_BANDS).mouth_open / 2

    def test_it_talks_without_any_audio_data(self):
        a = FaceAnimator()
        a.set_mode(FaceMode.SPEAKING)
        advance(a, 1.0)
        assert a.state.mouth_open > 0.15

    def test_thinking_keeps_its_mouth_shut(self):
        a = FaceAnimator()
        a.set_mode(FaceMode.THINKING)
        a.set_levels([0.9] * EQ_BANDS)
        advance(a, 1.5)
        assert a.state.mouth_open < 0.02


class TestMouthShapeTracking:
    """The wide/round range follows whichever voice is speaking."""

    @staticmethod
    def bands(centre, spread=2.0):
        """A spectrum with its energy around one band."""
        return [
            max(0.0, 1.0 - abs(i - centre) / spread) for i in range(EQ_BANDS)
        ]

    def feed(self, animator, bands, seconds):
        animator.set_levels(bands)
        advance(animator, seconds)

    def test_one_sibilant_does_not_flatten_the_vowels_after_it(self):
        """A running min/max would rescale on the outlier and never recover."""
        a = FaceAnimator()
        a.set_mode(FaceMode.SPEAKING)
        self.feed(a, self.bands(5), 2.0)
        before = a.state.mouth_wide

        self.feed(a, self.bands(14), 0.15)  # a bright /s/
        self.feed(a, self.bands(5), 0.6)  # back to the same vowel
        assert a.state.mouth_wide == pytest.approx(before, abs=0.25)

    def test_a_brighter_sound_is_still_wider(self):
        a = FaceAnimator()
        a.set_mode(FaceMode.SPEAKING)
        self.feed(a, self.bands(6), 1.5)
        dark = a.state.mouth_wide
        self.feed(a, self.bands(11), 0.4)
        assert a.state.mouth_wide > dark

    def test_a_monotone_voice_does_not_pin_the_mouth_shut(self):
        """With no variation at all the mouth should sit mid-range, not at 0."""
        a = FaceAnimator()
        a.set_mode(FaceMode.SPEAKING)
        self.feed(a, self.bands(8), 4.0)
        assert 0.2 < a.state.mouth_wide < 0.8


class TestSilenceVersusNoSource:
    """An empty level list and a list of zeros mean different things."""

    def test_a_silent_source_shuts_the_mouth(self):
        a = FaceAnimator()
        a.set_mode(FaceMode.SPEAKING)
        a.set_levels([0.0] * EQ_BANDS)
        advance(a, 1.5)
        assert a.state.mouth_open < 0.05

    def test_no_source_at_all_still_animates(self):
        """Without a bridge the face must still look like it is talking."""
        a = FaceAnimator()
        a.set_mode(FaceMode.SPEAKING)
        a.set_levels([])
        advance(a, 1.0)
        assert a.state.mouth_open > 0.3


class TestGazeDirection:
    def test_the_eyes_point_at_the_target_not_at_a_corner(self):
        """Clamping each axis separately would drag the look to a diagonal."""
        a = FaceAnimator()
        a.look_at(4.0, 1.0)  # far away, mostly to the right
        advance(a, 0.5)
        assert a.state.gaze_x > 0
        # The 4:1 ratio of the request must survive the clamp.
        assert a.state.gaze_x / a.state.gaze_y == pytest.approx(4.0, rel=0.05)

    def test_deflection_is_capped(self):
        a = FaceAnimator()
        a.look_at(100.0, -100.0)
        advance(a, 0.5)
        assert math.hypot(a.state.gaze_x, a.state.gaze_y) <= FaceAnimator.LOOK_REACH + 1e-6

    def test_a_near_target_is_not_stretched(self):
        a = FaceAnimator()
        a.look_at(0.05, 0.02)
        advance(a, 0.5)
        assert a.state.gaze_x == pytest.approx(0.05, abs=0.005)


class TestRenderer:
    @pytest.mark.parametrize("mode", list(FaceMode))
    def test_every_mode_renders(self, mode):
        a = FaceAnimator()
        a.set_mode(mode)
        advance(a, 0.6)
        img = FaceRenderer(120, supersample=1).render(a.state)
        assert img.size == (120, 120)
        assert img.mode == "RGB"

    def test_corners_are_keyed_out(self):
        """The window is square; the orb must not be."""
        a = FaceAnimator()
        advance(a, 0.5)
        img = FaceRenderer(120, supersample=1).render(a.state)
        px = img.load()
        for xy in [(1, 1), (118, 1), (1, 118), (118, 118)]:
            assert px[xy] == TRANSPARENT_KEY

    def test_centre_is_drawn(self):
        a = FaceAnimator()
        advance(a, 0.5)
        img = FaceRenderer(120, supersample=1).render(a.state)
        assert img.load()[(60, 60)] != TRANSPARENT_KEY

    def test_no_key_colour_inside_the_orb(self):
        """Any key-coloured pixel inside the orb would punch a hole in the face."""
        a = FaceAnimator()
        a.set_mode(FaceMode.SPEAKING)
        a.set_levels([0.9] * EQ_BANDS)
        advance(a, 1.0)
        img = FaceRenderer(160, supersample=2).render(a.state)
        px = img.load()
        for x in range(56, 104):
            for y in range(56, 104):
                assert px[x, y] != TRANSPARENT_KEY

    def test_the_orb_stays_inside_the_window_while_popping(self):
        """The spring must never push the silhouette off the edge."""
        renderer = FaceRenderer(120, supersample=1)
        a = FaceAnimator()
        for i in range(240):
            a.nudge(2.5)
            state = a.tick(1 / 60)
            if i % 12:
                continue
            px = renderer.render(state).load()
            for xy in [(0, 60), (119, 60), (60, 0), (60, 119)]:
                assert px[xy] == TRANSPARENT_KEY

    def test_consecutive_frames_differ(self):
        """Nothing may render as a still image - idle included."""
        renderer = FaceRenderer(120, supersample=1)
        a = FaceAnimator()
        advance(a, 1.0)
        first = renderer.render(a.tick(1 / 30)).tobytes()
        advance(a, 0.4)
        assert renderer.render(a.state).tobytes() != first

    def test_modes_are_visually_distinct(self):
        renderer = FaceRenderer(120, supersample=1)
        seen = {}
        for mode in FaceMode:
            a = FaceAnimator()
            a.set_mode(mode)
            advance(a, 0.6)
            seen[mode] = renderer.render(a.state).tobytes()
        assert len(set(seen.values())) == len(FaceMode)


def _speaking(level: float = 0.9, seconds: float = 1.0) -> FaceAnimator:
    a = FaceAnimator()
    a.set_mode(FaceMode.SPEAKING)
    a.set_levels([level] * EQ_BANDS)
    advance(a, seconds)
    return a


class TestHoloRenderer:
    """The J.A.R.V.I.S. core. Same contract as the face it replaces."""

    @pytest.mark.parametrize("mode", list(FaceMode))
    def test_every_mode_renders(self, mode):
        a = FaceAnimator()
        a.set_mode(mode)
        advance(a, 0.6)
        img = HoloRenderer(120, supersample=1).render(a.state)
        assert img.size == (120, 120)
        assert img.mode == "RGB"

    def test_corners_are_keyed_out(self):
        img = HoloRenderer(120, supersample=1).render(_speaking().state)
        px = img.load()
        for xy in [(1, 1), (118, 1), (1, 118), (118, 118)]:
            assert px[xy] == TRANSPARENT_KEY

    def test_nothing_is_drawn_outside_the_window_while_popping(self):
        """The spring plus a loud voice must not push the corona off the edge."""
        renderer = HoloRenderer(120, supersample=1)
        a = _speaking()
        for i in range(240):
            a.nudge(2.5)
            state = a.tick(1 / 60)
            if i % 12:
                continue
            px = renderer.render(state).load()
            for xy in [(0, 60), (119, 60), (60, 0), (60, 119)]:
                assert px[xy] == TRANSPARENT_KEY

    def test_no_key_colour_inside_the_sphere(self):
        """A keyed pixel inside the silhouette would punch a hole in the core.

        This is the failure mode of drawing with alpha onto the sphere: Pillow
        overwrites the destination's alpha rather than compositing with it, so
        anything translucent drawn straight onto it shows the desktop through.
        """
        img = HoloRenderer(160, supersample=2).render(_speaking().state)
        px = img.load()
        for x in range(64, 96):
            for y in range(64, 96):
                assert px[x, y] != TRANSPARENT_KEY

    def test_it_spins(self):
        renderer = HoloRenderer(120, supersample=1)
        a = FaceAnimator()
        advance(a, 1.0)
        first = renderer.render(a.tick(1 / 30)).tobytes()
        advance(a, 0.4)
        assert renderer.render(a.state).tobytes() != first

    def test_a_change_of_pace_never_whips_the_cage_round(self):
        """The yaw is integrated, not `uptime * rate`.

        Multiplying the rate by the clock meant every flicker of energy, and
        every mode change, jumped the sphere by the rate change times the
        whole uptime - a full spin or more after ten minutes.
        """
        renderer = HoloRenderer(120, supersample=1)
        a = FaceAnimator()
        advance(a, 600.0)
        renderer.render(a.state)
        before = renderer._yaw
        # Thinking more than doubles the rate; after ten minutes that used
        # to be dozens of radians in one frame.
        a.set_mode(FaceMode.THINKING)
        renderer.render(a.tick(1 / 30))
        moved = (renderer._yaw - before) % (2 * math.pi)
        moved = min(moved, 2 * math.pi - moved)
        # Fastest possible rate is SPIN * 1.9 * 2.3, for one frame.
        assert moved < abs(renderer.SPIN) * 1.9 * 2.3 / 30 + 1e-6

    def test_modes_are_visually_distinct(self):
        renderer = HoloRenderer(120, supersample=1)
        seen = set()
        for mode in FaceMode:
            a = FaceAnimator()
            a.set_mode(mode)
            advance(a, 0.6)
            seen.add(renderer.render(a.state).tobytes())
        assert len(seen) == len(FaceMode)

    @staticmethod
    def _lit(renderer, state):
        """How much of the window the core actually covers."""
        img = renderer.render(state)
        px = img.load()
        return sum(
            px[x, y] != TRANSPARENT_KEY
            for x in range(img.width)
            for y in range(img.height)
        )

    def test_a_loud_voice_lights_more_of_the_window(self):
        """The pulse has to be geometry, not only brightness."""
        renderer = HoloRenderer(140, supersample=1)
        quiet = FaceAnimator()
        quiet.set_mode(FaceMode.SPEAKING)
        quiet.set_levels([0.0] * EQ_BANDS)
        advance(quiet, 1.5)

        # Same elapsed time, so the sphere is at the same point in its spin and
        # the difference is the voice and nothing else.
        loud = _speaking(1.0, 1.5)
        assert self._lit(renderer, loud.state) > self._lit(renderer, quiet.state) * 1.05

    def test_the_lip_sync_envelope_drives_the_pulse(self):
        """set_mouth wins over the summarised levels, as it does for the mouth."""
        renderer = HoloRenderer(140, supersample=1)
        silent = FaceAnimator()
        silent.set_mode(FaceMode.SPEAKING)
        silent.set_levels([0.0] * EQ_BANDS)
        advance(silent, 1.5)

        synced = FaceAnimator()
        synced.set_mode(FaceMode.SPEAKING)
        synced.set_levels([0.0] * EQ_BANDS)
        synced.set_mouth((1.0, 0.5))
        advance(synced, 1.5)

        assert synced.state.mouth_open > 0.5
        assert self._lit(renderer, synced.state) > self._lit(renderer, silent.state)

    def test_the_gaze_turns_the_sphere(self):
        """No eyes to point, so the whole object leans towards the cursor."""
        renderer = HoloRenderer(120, supersample=1)
        a = FaceAnimator()
        advance(a, 1.0)
        straight = renderer.render(a.state).tobytes()

        a.state.gaze_x, a.state.gaze_y = 0.2, 0.1
        assert renderer.render(a.state).tobytes() != straight


class TestStyles:
    def test_both_styles_are_selectable(self):
        from arnold.face.app import _STYLES

        assert set(_STYLES) == {"holo", "orb"}
        assert _STYLES["holo"] is HoloRenderer
        assert _STYLES["orb"] is FaceRenderer

    def test_the_default_is_the_jarvis_core(self):
        from arnold.config import FaceConfig

        assert FaceConfig().style == "holo"


class TestTheme:
    def test_lerp_endpoints(self):
        assert lerp((0, 0, 0), (255, 255, 255), 0.0) == (0, 0, 0)
        assert lerp((0, 0, 0), (255, 255, 255), 1.0) == (255, 255, 255)

    def test_lerp_clamps(self):
        assert lerp((0, 0, 0), (100, 100, 100), 5.0) == (100, 100, 100)
        assert lerp((0, 0, 0), (100, 100, 100), -5.0) == (0, 0, 0)

    def test_every_mode_has_a_theme(self):
        for mode in FaceMode:
            assert isinstance(Theme.for_mode(mode), Theme)

    def test_no_theme_colour_collides_with_the_key(self):
        for mode in FaceMode:
            theme = Theme.for_mode(mode)
            for colour in (theme.accent, theme.accent_dim, theme.backdrop, theme.rim):
                assert colour != TRANSPARENT_KEY


class TestListeningIsObvious:
    """An open microphone has to be readable at a glance, in a silent room.

    None of these feed any audio levels: the whole point is that listening
    must look like listening before anyone has said a word.
    """

    @staticmethod
    def _settled(mode):
        a = FaceAnimator()
        a.set_mode(mode)
        advance(a, 2.0)
        return a.state

    @staticmethod
    def _brightness(img):
        px = img.load()
        w, h = img.size
        lit = [
            sum(px[x, y]) / 3
            for x in range(w)
            for y in range(h)
            if px[x, y] != TRANSPARENT_KEY
        ]
        return sum(lit) / len(lit)

    def test_listening_is_lit_well_above_idle(self):
        listening = self._settled(FaceMode.LISTENING).energy
        idle = self._settled(FaceMode.IDLE).energy
        assert listening > idle + 0.3

    def test_listening_is_not_gold(self):
        from arnold.face.holo import HoloTheme

        idle = HoloTheme.for_mode(FaceMode.IDLE).wire
        listening = HoloTheme.for_mode(FaceMode.LISTENING).wire
        assert idle[0] > idle[2], "idle is warm"
        assert listening[2] > listening[0], "listening is cool"

    def test_listening_renders_brighter_than_idle_in_silence(self):
        renderer = HoloRenderer(120, supersample=1)
        idle = self._brightness(renderer.render(self._settled(FaceMode.IDLE)))
        listening = self._brightness(renderer.render(self._settled(FaceMode.LISTENING)))
        assert listening > idle * 1.25

    def test_listening_stays_inside_the_window_while_swelling(self):
        """The corona swell must not push a spike off the edge."""
        renderer = HoloRenderer(120, supersample=1)
        a = FaceAnimator()
        a.set_mode(FaceMode.LISTENING)
        for i in range(180):
            state = a.tick(1 / 60)
            if i % 15:
                continue
            px = renderer.render(state).load()
            for x in range(120):
                assert px[x, 0] == TRANSPARENT_KEY
                assert px[x, 119] == TRANSPARENT_KEY
                assert px[0, x] == TRANSPARENT_KEY
                assert px[119, x] == TRANSPARENT_KEY
