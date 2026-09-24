"""The eyes follow the mouse anywhere on screen.

Polled rather than bound to <Motion>: a colour-keyed window only receives
mouse messages over its own opaque pixels, so an event binding would only
notice the pointer once it was already sitting on the orb.

FaceWindow needs a display, so these drive the tracking method against a stand
-in for the Tk window rather than opening a real one.
"""

import math
import types

import pytest

from arnold.config import Config
from arnold.face.app import CURSOR_ATTENTION_SECONDS, FaceWindow
from arnold.face.state import FaceAnimator


class FakeRoot:
    """Just the two geometry queries _track_cursor asks for."""

    def __init__(self, x, y):
        self._x, self._y = x, y

    def winfo_rootx(self):
        return self._x

    def winfo_rooty(self):
        return self._y


@pytest.fixture
def face(monkeypatch):
    """A FaceWindow with only the parts _track_cursor touches."""
    window = FaceWindow.__new__(FaceWindow)
    window.config = Config()
    window.size = 200
    window.root = FakeRoot(1000, 800)  # orb centre at (1100, 900)
    window.animator = FaceAnimator()
    window._last_cursor = None
    window._cursor_moved_at = 0.0
    return window


def place_cursor(monkeypatch, position):
    monkeypatch.setattr(
        "arnold.face.app.window",
        types.SimpleNamespace(cursor_position=lambda: position),
    )


def settle(face, seconds=0.4):
    for _ in range(int(seconds * 60)):
        face._track_cursor()
        face.animator.tick(1 / 60)
    return face.animator.state


class TestDirection:
    def test_a_cursor_to_the_right_looks_right(self, face, monkeypatch):
        place_cursor(monkeypatch, (1800, 900))
        state = settle(face)
        assert state.gaze_x > 0.1
        assert abs(state.gaze_y) < 0.02

    def test_a_cursor_to_the_left_looks_left(self, face, monkeypatch):
        place_cursor(monkeypatch, (200, 900))
        assert settle(face).gaze_x < -0.1

    def test_a_cursor_above_looks_up(self, face, monkeypatch):
        place_cursor(monkeypatch, (1100, 100))
        state = settle(face)
        assert state.gaze_y < -0.1
        assert abs(state.gaze_x) < 0.02

    def test_a_diagonal_is_not_squared_off(self, face, monkeypatch):
        """Clamping per axis would send the eyes to the corner instead."""
        place_cursor(monkeypatch, (1100 + 900, 900 + 300))  # 3:1, down-right
        state = settle(face)
        assert state.gaze_x / state.gaze_y == pytest.approx(3.0, rel=0.08)


class TestReach:
    def test_deflection_never_exceeds_the_limit(self, face, monkeypatch):
        place_cursor(monkeypatch, (9000, -9000))
        state = settle(face)
        assert math.hypot(state.gaze_x, state.gaze_y) <= FaceAnimator.LOOK_REACH + 1e-6

    def test_a_near_cursor_deflects_less_than_a_far_one(self, face, monkeypatch):
        place_cursor(monkeypatch, (1160, 900))  # just outside the orb
        near = abs(settle(face).gaze_x)
        place_cursor(monkeypatch, (2500, 900))
        far = abs(settle(face).gaze_x)
        assert near < far

    def test_the_cursor_on_the_centre_is_ignored(self, face, monkeypatch):
        """Zero distance has no direction; it must not divide by zero."""
        place_cursor(monkeypatch, (1100, 900))
        settle(face)  # would raise if unguarded


class TestAttention:
    def test_a_parked_cursor_releases_the_eyes(self, face, monkeypatch):
        """Staring at an abandoned pointer forever looks switched off."""
        place_cursor(monkeypatch, (1800, 900))
        settle(face, 0.4)
        held = face.animator.state.gaze_x
        assert held > 0.1

        # Time passes with the cursor untouched.
        for _ in range(int((CURSOR_ATTENTION_SECONDS + 3.0) * 60)):
            face._track_cursor()
            face.animator.tick(1 / 60)
        assert face.animator._look is None

    def test_moving_again_recaptures_them(self, face, monkeypatch):
        place_cursor(monkeypatch, (1800, 900))
        settle(face, 0.3)
        for _ in range(int((CURSOR_ATTENTION_SECONDS + 2.0) * 60)):
            face._track_cursor()
            face.animator.tick(1 / 60)

        place_cursor(monkeypatch, (300, 900))
        assert settle(face, 0.5).gaze_x < -0.1

    def test_it_can_be_switched_off(self, face, monkeypatch):
        face.config.face.follow_cursor = False
        place_cursor(monkeypatch, (1800, 900))
        assert abs(settle(face).gaze_x) < 0.15  # idle wandering only

    def test_an_unreadable_cursor_is_survivable(self, face, monkeypatch):
        place_cursor(monkeypatch, None)
        settle(face)  # must not raise
