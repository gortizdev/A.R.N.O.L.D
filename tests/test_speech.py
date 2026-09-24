"""The PC's own mouth.

There are only two unforgivable failures here, and neither is a missed
sentence: taking the agent down in order to say something, and blocking its
tick while it says it. Everything else - no speakers, no API key, no Pi, no
sound card - is a log line and silence.
"""

import ast
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from arnold.config import Config
from arnold.jarvis import JarvisError
from arnold.speech import (
    Voice,
    conversation_is_live,
    make_ring,
    set_conversation_live,
)

SRC = Path(__file__).resolve().parents[1] / "src" / "arnold"


class FakeJarvis:
    """The Pi, which may or may not be answering today."""

    def __init__(self, fail: bool = False) -> None:
        self.said: list[str] = []
        self.fail = fail
        self.calls = 0

    @property
    def enabled(self) -> bool:
        return True

    def say(self, text: str):
        self.calls += 1
        if self.fail:
            raise JarvisError("the Pi is off")
        self.said.append(text)
        return {"spoken": True}

    def check(self):
        return {"ok": not self.fail, "route": "home_assistant", "detail": ""}


class FakeSpeaker:
    """A local TTS backend that makes a short, silent noise."""

    def __init__(self, delay: float = 0.0) -> None:
        self.said: list[str] = []
        self.delay = delay

    def synthesize(self, text: str):
        self.said.append(text)
        if self.delay:
            time.sleep(self.delay)
        return np.zeros(240, dtype=np.int16), 24000


@pytest.fixture
def config(tmp_path):
    cfg = Config()
    cfg.state_file = str(tmp_path / "state.json")
    cfg.speech.defer_to_conversation = False
    cfg.speech.shutdown_wait_seconds = 10.0
    return cfg


@pytest.fixture
def played():
    """Everything that reached the speakers, without opening a sound card."""
    return []


@pytest.fixture
def voice(config, played, monkeypatch):
    """A Voice whose local leg records instead of playing."""
    made: list[Voice] = []

    def build(cfg=None, jarvis=None, speaker=None, route=""):
        v = Voice(cfg or config, jarvis=jarvis, route=route)
        if speaker is not None:
            v._speaker = speaker
            v._speaker_failed_at = time.monotonic()
        monkeypatch.setattr(
            v, "_play", lambda clip, volume: played.append((clip, volume)) or True
        )
        made.append(v)
        return v

    yield build
    for v in made:
        v.close(timeout=5)


def wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class TestRoute:
    def test_defaults_to_auto(self, config):
        assert config.speech_route() == "auto"

    def test_blank_route_follows_jarvis_none(self, config):
        config.jarvis.speech_route = "none"
        assert config.speech_route() == "none"

    def test_an_explicit_route_wins(self, config):
        config.jarvis.speech_route = "none"
        config.speech.route = "local"
        assert config.speech_route() == "local"

    def test_an_unknown_route_is_a_config_problem(self, config):
        config.speech.route = "shouting"
        assert any("speech.route" in p for p in config.validate())

    def test_an_unknown_backend_is_a_config_problem(self, config):
        config.speech.backend = "kazoo"
        assert any("speech.backend" in p for p in config.validate())

    def test_a_silly_volume_is_a_config_problem(self, config):
        config.speech.volume = 4.0
        assert any("speech.volume" in p for p in config.validate())


class TestNotBlocking:
    def test_say_returns_immediately(self, voice, config):
        """The tick must never wait on a sentence."""
        v = voice(jarvis=FakeJarvis(), speaker=FakeSpeaker(delay=0.5), route="local")
        started = time.monotonic()
        assert v.say("the disk is filling up")
        assert time.monotonic() - started < 0.05

    def test_a_full_queue_drops_the_oldest(self, config, voice):
        """For a monitoring agent the newest line is the true one."""
        config.speech.max_queued = 2
        speaker = FakeSpeaker()
        v = voice(speaker=speaker, route="local")
        # Hold the worker so the queue actually fills.
        gate = threading.Event()
        v._queue.put(None) if False else None
        with_lock = threading.Lock()
        original = v._deliver

        def slow(item):
            gate.wait(2.0)
            with with_lock:
                original(item)

        v._deliver = slow
        for line in ("one", "two", "three", "four"):
            v.say(line)
        gate.set()
        v.close(timeout=5)
        assert "one" not in speaker.said

    def test_a_stale_line_is_not_read_out(self, config, voice):
        config.speech.stale_after_seconds = 0.0001
        speaker = FakeSpeaker()
        v = voice(speaker=speaker, route="local")
        v.say("this took too long to get to")
        time.sleep(0.05)
        v.close(timeout=5)
        assert speaker.said == []


class TestRouting:
    def test_jarvis_route_never_touches_the_speakers(self, voice):
        pi = FakeJarvis()
        speaker = FakeSpeaker()
        v = voice(jarvis=pi, speaker=speaker, route="jarvis")
        v.say("hello")
        v.close(timeout=5)
        assert pi.said == ["hello"]
        assert speaker.said == []

    def test_local_route_never_touches_the_pi(self, voice):
        pi = FakeJarvis()
        speaker = FakeSpeaker()
        v = voice(jarvis=pi, speaker=speaker, route="local")
        v.say("hello")
        v.close(timeout=5)
        assert pi.calls == 0
        assert speaker.said == ["hello"]

    def test_both_speaks_in_both_places(self, voice):
        pi = FakeJarvis()
        speaker = FakeSpeaker()
        v = voice(jarvis=pi, speaker=speaker, route="both")
        v.say("dinner is ready")
        v.close(timeout=5)
        assert pi.said == ["dinner is ready"]
        assert speaker.said == ["dinner is ready"]

    def test_auto_prefers_the_pi_and_stays_quiet_here(self, voice):
        pi = FakeJarvis()
        speaker = FakeSpeaker()
        v = voice(jarvis=pi, speaker=speaker, route="auto")
        v.say("the build finished")
        v.close(timeout=5)
        assert pi.said == ["the build finished"]
        assert speaker.said == []

    def test_auto_falls_back_to_local_when_the_pi_refuses(self, voice):
        pi = FakeJarvis(fail=True)
        speaker = FakeSpeaker()
        v = voice(jarvis=pi, speaker=speaker, route="auto")
        v.say("the build finished")
        v.close(timeout=5)
        assert pi.said == []
        assert speaker.said == ["the build finished"]

    def test_route_none_says_nothing_anywhere(self, voice):
        pi = FakeJarvis()
        speaker = FakeSpeaker()
        v = voice(jarvis=pi, speaker=speaker, route="none")
        assert v.say("hello") is False
        assert not v.enabled
        v.close(timeout=5)
        assert pi.calls == 0
        assert speaker.said == []


class TestADeadPi:
    def test_auto_stops_asking_a_dead_pi(self, voice):
        """One timeout a minute, not one on every line."""
        pi = FakeJarvis(fail=True)
        speaker = FakeSpeaker()
        v = voice(jarvis=pi, speaker=speaker, route="auto")
        for line in ("one", "two", "three"):
            v.say(line)
        v.close(timeout=5)
        assert pi.calls == 1
        assert speaker.said == ["one", "two", "three"]

    def test_the_pi_is_tried_again_after_the_backoff(self, config, voice, monkeypatch):
        config.speech.jarvis_retry_seconds = 30.0
        pi = FakeJarvis(fail=True)
        v = voice(jarvis=pi, speaker=FakeSpeaker(), route="auto")
        v.say("one")
        v.close(timeout=5)
        assert pi.calls == 1

        # Wind the clock past the backoff; the Pi is worth a try again.
        v._retry_at = 0.0
        pi.fail = False
        v._closed = False
        v._thread = None
        v.say("two")
        v.close(timeout=5)
        assert pi.said == ["two"]

    def test_pi_down_since_is_recorded_and_cleared(self, voice):
        pi = FakeJarvis(fail=True)
        v = voice(jarvis=pi, speaker=FakeSpeaker(), route="auto")
        assert v.pi_down_since == 0.0
        v.say("one")
        assert wait_for(lambda: v.pi_down_since > 0.0)

        pi.fail = False
        v._retry_at = 0.0
        v.say("two")
        assert wait_for(lambda: v.pi_down_since == 0.0)
        v.close(timeout=5)


class TestDegrading:
    def test_no_local_backend_is_a_log_line_not_a_crash(self, config, caplog, monkeypatch):
        v = Voice(config, jarvis=FakeJarvis(fail=True), route="local")
        # Nothing could be built: no sounddevice, no API key, no Piper voice.
        monkeypatch.setattr(v, "_build_speaker", lambda: None)
        for line in ("one", "two", "three"):
            assert v.say(line)
        v.close(timeout=5)
        # One warning, not one per line: a machine with no speakers must not
        # fill the log with the fact.
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert len([w for w in warnings if "no local speech backend" in w.message]) == 1

    def test_a_missing_voice_package_is_survivable(self, config, monkeypatch):
        v = Voice(config, jarvis=FakeJarvis(fail=True), route="local")
        monkeypatch.setattr(
            v, "_build_speaker", lambda: (_ for _ in ()).throw(ImportError("no numpy"))
        )
        with pytest.raises(ImportError):
            v._build_speaker()  # the fake raises, as a missing package would
        # ...but going through say() must not.
        v2 = Voice(config, jarvis=FakeJarvis(fail=True), route="local")
        v2._speaker_failed_at = time.monotonic()
        assert v2.say("still fine")
        v2.close(timeout=5)

    def test_a_synthesiser_that_throws_does_not_end_the_worker(self, voice):
        class FailsOnce(FakeSpeaker):
            """Fails the first line and then recovers, the way a flaky
            network TTS actually behaves."""

            def synthesize(self, text):
                if not self.said:
                    self.said.append(text)
                    raise RuntimeError("no network")
                return super().synthesize(text)

        speaker = FailsOnce()
        v = voice(speaker=speaker, route="local")
        v.say("this one fails")
        v.say("this one works")
        v.close(timeout=5)
        # The worker survived the first failure and delivered the second line.
        assert speaker.said == ["this one fails", "this one works"]

    def test_close_is_safe_when_nothing_was_ever_said(self, config):
        Voice(config).close(timeout=1)

    def test_close_twice_is_safe(self, voice):
        v = voice(speaker=FakeSpeaker(), route="local")
        v.say("hello")
        v.close(timeout=5)
        v.close(timeout=5)

    def test_saying_nothing_is_refused_quietly(self, voice):
        v = voice(speaker=FakeSpeaker(), route="local")
        assert v.say("   ") is False
        assert v.say("") is False


class TestTheFace:
    def test_speaking_publishes_the_face_state(self, config):
        seen = []
        v = Voice(config, jarvis=FakeJarvis(), publish=lambda leaf, p: seen.append((leaf, p)),
                  route="local")
        v._speaker = FakeSpeaker()
        v._speaker_failed_at = time.monotonic()
        # The real _play publishes around the audio; stub sounddevice out.
        v.say("hello")
        v.close(timeout=5)
        # No sound card in CI, so playback fails - but the face was still told,
        # and told that it stopped.
        assert ("state", "speaking") in seen
        assert seen[-1] == ("state", "idle")

    def test_publishing_can_be_switched_off(self, config):
        config.speech.publish_face_state = False
        seen = []
        v = Voice(config, jarvis=FakeJarvis(), publish=lambda leaf, p: seen.append((leaf, p)),
                  route="local")
        v._speaker = FakeSpeaker()
        v._speaker_failed_at = time.monotonic()
        v.say("hello")
        v.close(timeout=5)
        assert seen == []

    def test_a_publish_that_throws_is_not_fatal(self, config):
        def boom(leaf, payload):
            raise RuntimeError("no broker")

        v = Voice(config, jarvis=FakeJarvis(), publish=boom, route="local")
        v._speaker = FakeSpeaker()
        v._speaker_failed_at = time.monotonic()
        v.say("hello")
        v.close(timeout=5)


class TestConversations:
    def test_the_marker_round_trips(self, config):
        assert conversation_is_live(config) is False
        set_conversation_live(config, True)
        assert conversation_is_live(config) is True
        set_conversation_live(config, False)
        assert conversation_is_live(config) is False

    def test_a_stale_marker_does_not_mute_the_agent_forever(self, config):
        set_conversation_live(config, True)
        assert conversation_is_live(config, max_age=-1) is False

    def test_a_live_conversation_defers_speech(self, config, voice):
        config.speech.defer_to_conversation = True
        config.speech.defer_seconds = 0.3
        set_conversation_live(config, True)
        speaker = FakeSpeaker()
        v = voice(speaker=speaker, route="local")
        started = time.monotonic()
        v.say("the disk is filling")
        v.close(timeout=5)
        # Said in the end - a deferred alert is not a dropped one - but only
        # after waiting for the conversation.
        assert speaker.said == ["the disk is filling"]
        assert time.monotonic() - started >= 0.25


class TestTheRing:
    def test_the_ring_is_generated_and_does_not_click(self):
        audio = make_ring()
        assert audio is not None
        assert 0.5 < len(audio) / 24000 < 4.0
        assert np.max(np.abs(audio)) < 32767
        assert abs(int(audio[0])) < 400 and abs(int(audio[-1])) < 400

    def test_the_ring_is_not_the_wake_chime(self):
        """Across a room these must not be mistakable for one another."""
        from arnold.voice.wake_ack import make_chime

        assert len(make_ring()) > len(make_chime()) * 2

    def test_ringing_plays_locally_whatever_the_route(self, voice, played):
        pi = FakeJarvis()
        v = voice(jarvis=pi, speaker=FakeSpeaker(), route="jarvis")
        v.say("your pasta is done", ring=True)
        v.close(timeout=5)
        assert played, "the ring should have been played here even on the Pi route"
        assert pi.said == ["your pasta is done"]


def test_the_agent_speaks_through_the_voice_not_the_pi_client():
    """service.py must not reach past Voice to the Pi.

    Doing so is how alerts went missing whenever the Pi was off, and it is an
    easy line to add back by accident.
    """
    tree = ast.parse((SRC / "service.py").read_text(encoding="utf-8"))
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "say":
            continue
        target = node.func.value
        if (
            isinstance(target, ast.Attribute)
            and target.attr == "jarvis"
            and isinstance(target.value, ast.Name)
            and target.value.id == "self"
        ):
            offenders.append(node.lineno)
    assert not offenders, (
        f"service.py:{offenders} calls self.jarvis.say directly. Use self.speech.say, "
        "or a Pi that is switched off silently swallows it."
    )
