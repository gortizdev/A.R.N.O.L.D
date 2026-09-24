"""Who gets to talk, and when.

Two complaints drive all of this: it answers before you have finished a
sentence, and talking over it achieves nothing. The first is turn detection
(what gets sent in session.update), the second is barge-in (what the mic worker
does with audio arriving while a reply is playing).

Neither is testable against a live socket, so what is checked here is the
decision - the events that go up the wire, and the conditions under which the
assistant yields the floor - with the socket replaced by a list.
"""

import numpy as np
import pytest

from arnold.config import Config
from arnold.voice.realtime import RATE, RealtimeConversation, TurnTaking
from arnold.voice.session_config import (
    SessionConfig,
    turn_detection_for,
)

PI_SETTINGS = {
    "type": "server_vad",
    "threshold": 0.5,
    "prefix_padding_ms": 300,
    "silence_duration_ms": 700,
}


class TestTurnDetection:
    def test_semantic_replaces_the_timer_with_a_model(self):
        voice = Config().voice
        assert voice.turn_detection == "semantic"  # the default, deliberately
        detection = turn_detection_for(voice, PI_SETTINGS)
        assert detection == {"type": "semantic_vad", "eagerness": "low"}

    def test_server_mode_is_more_patient_than_the_api_default(self):
        voice = Config().voice
        voice.turn_detection = "server"
        detection = turn_detection_for(voice, PI_SETTINGS)
        assert detection["type"] == "server_vad"
        # 500ms is the API default and 700ms is the Pi's; both cut in on a
        # pause for breath at this range.
        assert detection["silence_duration_ms"] > 700

    def test_pi_mode_keeps_what_the_pi_reported(self):
        voice = Config().voice
        voice.turn_detection = "pi"
        assert turn_detection_for(voice, PI_SETTINGS) == PI_SETTINGS

    def test_an_unknown_mode_falls_back_rather_than_failing(self):
        voice = Config().voice
        voice.turn_detection = "nonsense"
        assert turn_detection_for(voice, PI_SETTINGS) == PI_SETTINGS

    def test_the_override_does_not_mutate_the_pi_settings(self):
        voice = Config().voice
        voice.turn_detection = "pi"
        turn_detection_for(voice, PI_SETTINGS)["threshold"] = 0.99
        assert PI_SETTINGS["threshold"] == 0.5

    @pytest.mark.parametrize(
        "field, value",
        [
            ("turn_detection", "shouty"),
            ("turn_eagerness", "immediate"),
            ("barge_in_threshold", 1.5),
        ],
    )
    def test_bad_settings_are_caught_by_validate(self, field, value):
        cfg = Config()
        setattr(cfg.voice, field, value)
        assert any(field in problem for problem in cfg.validate())


class FakeSocket:
    """Collects what would have gone to the Realtime API."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    def send(self, raw: str) -> None:
        import json

        self.sent.append(json.loads(raw))

    def types(self) -> list[str]:
        return [event["type"] for event in self.sent]


def conversation(**turn_kwargs) -> RealtimeConversation:
    convo = RealtimeConversation(
        "key",
        SessionConfig(),
        tools=[],
        dispatch_tool=lambda name, args: {},
        turn=TurnTaking(**turn_kwargs),
    )
    convo._ws = FakeSocket()
    return convo


def frame(rms: float, samples: int = 3840) -> np.ndarray:
    """A frame of noise at a given RMS, as int16 - what the mic hands over."""
    return np.full(samples, int(rms * 32768), dtype=np.int16)


def speak(
    convo: RealtimeConversation,
    seconds: float = 3.0,
    item: str = "item_1",
    finished: bool = False,
) -> None:
    """Put a reply on the wire, so the mic is gated and there is a turn to cut.

    `finished` is the common case in the wild rather than the exception: the
    model streams audio around six times faster than it plays, so a reply is
    usually generated and done while the speaker is still on its first
    sentence.
    """
    convo._handle_event({"type": "response.created"})
    convo._handle_event(
        {
            "type": "response.output_audio.delta",
            "item_id": item,
            "content_index": 0,
            "delta": _b64(np.zeros(int(RATE * seconds), dtype=np.int16)),
        }
    )
    if finished:
        convo._handle_event({"type": "response.done"})


def _b64(pcm: np.ndarray) -> str:
    import base64

    return base64.b64encode(pcm.tobytes()).decode("ascii")


class TestGating:
    def test_the_mic_is_gated_for_the_whole_reply(self):
        convo = conversation()
        assert not convo._speaking()
        speak(convo, seconds=5.0)
        # The model streams far faster than real time, so five seconds of audio
        # arrives at once; the gate has to last as long as the sound, not as
        # long as the download.
        assert convo._speaking()

    def test_the_echo_guard_extends_the_gate_past_the_last_sample(self):
        convo = conversation(echo_guard_seconds=1.0)
        convo._play_head = __import__("time").monotonic() - 0.5
        assert convo._speaking()
        convo.turn = TurnTaking(echo_guard_seconds=0.1)
        assert not convo._speaking()


class TestBargeIn:
    def test_quiet_room_never_interrupts(self):
        convo = conversation(grace_seconds=0.0)
        speak(convo)
        for _ in range(50):
            assert not convo._consider_barge_in(frame(0.01))
        assert convo._ws.types() == []

    def test_a_short_noise_is_not_an_interruption(self):
        """A cough, a door, one keystroke - loud, but not someone talking."""
        convo = conversation(grace_seconds=0.0, hold_seconds=0.25)
        speak(convo)
        assert not convo._consider_barge_in(frame(0.3))
        assert convo._ws.types() == []

    def test_sustained_speech_takes_the_floor(self, monkeypatch):
        convo = conversation(grace_seconds=0.0, hold_seconds=0.25, threshold=0.05)
        clock = _FakeClock(start=1000.0)
        monkeypatch.setattr("arnold.voice.realtime.time.monotonic", clock)
        speak(convo)

        assert not convo._consider_barge_in(frame(0.2))  # first loud frame
        clock.advance(0.3)
        assert convo._consider_barge_in(frame(0.2))

        assert convo._ws.types() == ["conversation.item.truncate", "response.cancel"]
        assert not convo._speaking()  # the mic is live again

    def test_the_grace_period_ignores_your_own_last_word(self, monkeypatch):
        """The tail of the question, still decaying in the room, must not
        cancel the answer to it."""
        convo = conversation(grace_seconds=1.0, hold_seconds=0.0, threshold=0.05)
        speak(convo)
        for _ in range(10):
            assert not convo._consider_barge_in(frame(0.3))
        assert convo._ws.types() == []

    def test_the_model_is_told_only_what_was_heard(self, monkeypatch):
        """A truncate at the wrong point leaves it following up on a sentence
        you never got - or forgetting one you did."""
        convo = conversation(grace_seconds=0.0, hold_seconds=0.0, threshold=0.05)
        clock = _FakeClock(start=1000.0)
        monkeypatch.setattr("arnold.voice.realtime.time.monotonic", clock)
        speak(convo, seconds=8.0)

        clock.advance(2.0)
        convo._consider_barge_in(frame(0.2))

        truncate = convo._ws.sent[0]
        assert truncate["item_id"] == "item_1"
        assert truncate["content_index"] == 0
        # Two seconds of an eight second reply were audible.
        assert 1900 <= truncate["audio_end_ms"] <= 2100

    def test_it_cannot_claim_more_audio_played_than_exists(self, monkeypatch):
        convo = conversation(grace_seconds=0.0, hold_seconds=0.0, threshold=0.05)
        clock = _FakeClock(start=1000.0)
        monkeypatch.setattr("arnold.voice.realtime.time.monotonic", clock)
        speak(convo, seconds=1.0)

        clock.advance(0.9)
        convo._consider_barge_in(frame(0.2))
        assert convo._ws.sent[0]["audio_end_ms"] <= 1000

    def test_late_audio_for_a_cancelled_reply_is_dropped(self, monkeypatch):
        """Deltas keep arriving after a cancel; playing them would re-gate the
        mic in the middle of the interruption."""
        convo = conversation(grace_seconds=0.0, hold_seconds=0.0, threshold=0.05)
        speak(convo)
        convo._consider_barge_in(frame(0.2))
        assert not convo._speaking()

        speak(convo)  # same item_id, arriving after the cancel
        assert not convo._speaking()

    def test_a_fresh_reply_after_the_interruption_still_plays(self):
        convo = conversation(grace_seconds=0.0, hold_seconds=0.0, threshold=0.05)
        speak(convo)
        convo._consider_barge_in(frame(0.2))

        speak(convo, seconds=1.0, item="item_2")
        assert convo._speaking()

    def test_a_finished_reply_is_truncated_but_not_cancelled(self):
        """The usual case: the audio is still playing but there is nothing left
        to generate, and cancelling then is an error from the server."""
        convo = conversation(grace_seconds=0.0, hold_seconds=0.0, threshold=0.05)
        speak(convo, seconds=8.0, finished=True)
        assert convo._consider_barge_in(frame(0.2))
        assert convo._ws.types() == ["conversation.item.truncate"]

    def test_a_reply_still_being_generated_is_cancelled(self):
        convo = conversation(grace_seconds=0.0, hold_seconds=0.0, threshold=0.05)
        speak(convo, seconds=8.0, finished=False)
        assert convo._consider_barge_in(frame(0.2))
        assert "response.cancel" in convo._ws.types()

    def test_it_does_not_cancel_the_same_response_twice(self):
        convo = conversation(grace_seconds=0.0, hold_seconds=0.0, threshold=0.05)
        speak(convo, seconds=8.0)
        convo._consider_barge_in(frame(0.2))
        convo._play_head = __import__("time").monotonic() + 5  # audio still draining
        convo._consider_barge_in(frame(0.2))
        assert convo._ws.types().count("response.cancel") == 1

    def test_the_interrupting_words_are_not_swallowed_by_the_hold(self, monkeypatch):
        """What you say while it decides you mean it still has to be heard, or
        the interruption arrives with its first syllable missing."""
        convo = conversation(grace_seconds=0.0, hold_seconds=0.25, threshold=0.05)
        clock = _FakeClock(start=1000.0)
        monkeypatch.setattr("arnold.voice.realtime.time.monotonic", clock)
        speak(convo)

        convo._consider_barge_in(frame(0.2))
        clock.advance(0.1)
        convo._consider_barge_in(frame(0.2))
        clock.advance(0.3)
        assert convo._consider_barge_in(frame(0.2))

        convo._flush_barge_buffer(48000)
        appends = [e for e in convo._ws.sent if e["type"] == "input_audio_buffer.append"]
        assert len(appends) == 3

    def test_a_broken_run_of_noise_starts_again(self, monkeypatch):
        convo = conversation(grace_seconds=0.0, hold_seconds=0.25, threshold=0.05)
        clock = _FakeClock(start=1000.0)
        monkeypatch.setattr("arnold.voice.realtime.time.monotonic", clock)
        speak(convo)

        convo._consider_barge_in(frame(0.2))
        clock.advance(0.2)
        convo._consider_barge_in(frame(0.001))  # the noise stops
        clock.advance(0.2)
        assert not convo._consider_barge_in(frame(0.2))  # so the hold restarts
        assert convo._ws.types() == []

    def test_disabling_it_restores_plain_half_duplex(self):
        convo = conversation(barge_in=False, grace_seconds=0.0, hold_seconds=0.0)
        speak(convo)
        for _ in range(20):
            assert not convo._consider_barge_in(frame(0.9))
        assert convo._ws.types() == []


class TestEchoReport:
    def test_it_reports_what_the_mic_heard_of_its_own_voice(self, caplog):
        convo = conversation(grace_seconds=10.0, threshold=0.5)
        speak(convo)
        convo._consider_barge_in(frame(0.08))
        with caplog.at_level("INFO"):
            convo._report_echo()
        assert "0.080" in caplog.text

    def test_a_threshold_below_the_echo_is_called_out(self, caplog):
        """Left like this it interrupts itself, which reads as random cutting
        out rather than as a misconfigured threshold."""
        convo = conversation(grace_seconds=10.0, threshold=0.05)
        speak(convo)
        convo._consider_barge_in(frame(0.06))
        with caplog.at_level("WARNING"):
            convo._report_echo()
        assert "cut itself off" in caplog.text

    def test_nothing_is_said_when_barge_in_is_off(self, caplog):
        convo = conversation(barge_in=False)
        with caplog.at_level("INFO"):
            convo._report_echo()
        assert caplog.text == ""


class _FakeClock:
    """A monotonic clock that only moves when the test says so."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class TestTurnTakingFromConfig:
    def test_milliseconds_become_seconds(self):
        voice = Config().voice
        voice.barge_in_hold_ms = 400
        voice.barge_in_grace_ms = 600
        voice.echo_guard_ms = 150
        turn = TurnTaking.from_config(voice)
        assert (turn.hold_seconds, turn.grace_seconds, turn.echo_guard_seconds) == (
            0.4,
            0.6,
            0.15,
        )
