"""The spoken "Yes?" that answers the wake word.

The thing worth testing here is not the audio - it is that this can never
leave the assistant mute or, worse, block the wake word waiting on a network
round trip. So: the chime always exists, a dead TTS backend degrades to it,
and a rendered phrase is cached rather than re-synthesised.
"""

import numpy as np
import pytest

from arnold.config import Config
from arnold.voice.wake_ack import WakeAck, make_chime


class FakeSynth:
    """Stands in for Piper/OpenAI. Counts calls so caching is observable."""

    def __init__(self, fail: bool = False) -> None:
        self.calls = 0
        self.fail = fail

    def synthesize(self, text: str):
        self.calls += 1
        if self.fail:
            raise RuntimeError("no network")
        # A distinct length per phrase, so clips are told apart.
        return np.zeros(1000 + len(text), dtype=np.int16), 24000


@pytest.fixture
def config(tmp_path):
    cfg = Config()
    cfg.voice.wake_ack_cache_dir = str(tmp_path / "ack")
    cfg.voice.wake_ack_phrases = ["Yes?", "Listening."]
    return cfg


class TestMode:
    @pytest.mark.parametrize("value", ["off", "none", "no", False, ""])
    def test_off_means_silent(self, config, value):
        config.voice.wake_ack = value
        ack = WakeAck(config, synth=FakeSynth())
        assert not ack.enabled
        assert ack.play() == 0.0

    @pytest.mark.parametrize("value", ["speech", True, "SPEECH"])
    def test_speech_is_the_default_reading(self, config, value):
        config.voice.wake_ack = value
        assert WakeAck(config, synth=FakeSynth()).enabled

    def test_chime_never_calls_the_synthesiser(self, config):
        config.voice.wake_ack = "chime"
        synth = FakeSynth()
        ack = WakeAck(config, synth=synth)
        ack.prepare(background=False)
        assert synth.calls == 0


class TestRendering:
    def test_phrases_are_rendered_once_and_cached(self, config, tmp_path):
        synth = FakeSynth()
        WakeAck(config, synth=synth).prepare(background=False)
        assert synth.calls == 2
        assert len(list((tmp_path / "ack").glob("*.wav"))) == 2

        # A second assistant (a restart) reads the cache instead.
        again = FakeSynth()
        second = WakeAck(config, synth=again)
        second.prepare(background=False)
        assert again.calls == 0
        assert len(second._clips) == 2

    def test_changing_voice_re_renders(self, config):
        WakeAck(config, synth=FakeSynth()).prepare(background=False)
        config.voice.piper_voice = "en_US-someone-else"
        fresh = FakeSynth()
        WakeAck(config, synth=fresh).prepare(background=False)
        assert fresh.calls == 2

    def test_the_session_voice_gets_its_own_cache(self, config):
        # The Pi can change the session voice under us; the old rendering must
        # not be replayed in the new voice's place.
        first = WakeAck(config, tts_voice="fable")._cache_path("Yes?")
        second = WakeAck(config, tts_voice="cedar")._cache_path("Yes?")
        assert first != second

    def test_the_cache_key_holds_still_across_phrases(self, config):
        """The lazily built speaker must not rename the files mid-render."""
        ack = WakeAck(config, synth=FakeSynth())
        before = [ack._cache_path(p) for p in config.voice.wake_ack_phrases]
        ack.prepare(background=False)
        after = [ack._cache_path(p) for p in config.voice.wake_ack_phrases]
        assert before == after

        # And a restart finds every one of them, not just the first.
        again = FakeSynth()
        WakeAck(config, synth=again).prepare(background=False)
        assert again.calls == 0

    def test_a_dead_backend_falls_back_to_the_chime(self, config):
        ack = WakeAck(config, synth=FakeSynth(fail=True))
        ack.prepare(background=False)
        assert ack._clips == []
        assert ack.enabled
        assert ack._pick() is ack._chime


class TestPlayback:
    def test_the_chime_answers_before_the_render_finishes(self, config):
        ack = WakeAck(config, synth=FakeSynth())  # prepare() not called yet
        assert ack._pick() is ack._chime

    def test_phrases_do_not_repeat_back_to_back(self, config):
        config.voice.wake_ack_phrases = ["a", "b", "c"]
        ack = WakeAck(config, synth=FakeSynth())
        ack.prepare(background=False)
        picks = [ack._pick() for _ in range(20)]
        assert all(a is not b for a, b in zip(picks, picks[1:]))

    def test_the_chime_is_short_and_does_not_clip(self):
        audio = make_chime()
        assert 0.1 < len(audio) / 24000 < 0.5
        assert np.max(np.abs(audio)) < 32767
        # No click at either end: a bare sine would start at full amplitude.
        assert abs(int(audio[0])) < 200 and abs(int(audio[-1])) < 200


class TestName:
    def test_phrases_take_the_name(self, config):
        config.voice.wake_ack_phrases = ["{name} here."]
        synth = FakeSynth()
        ack = WakeAck(config, synth=synth)
        ack.prepare(background=False)
        assert ack._phrases == ["Arnold here."]
        assert synth.calls == 1

        # A new name is a new phrase, so a fresh assistant renders it again.
        config.assistant.name = "Athena"
        again = FakeSynth()
        fresh = WakeAck(config, synth=again)
        fresh.prepare(background=False)
        assert fresh._phrases == ["Athena here."]
        assert again.calls == 1

    def test_a_stray_brace_is_harmless(self, config):
        config.voice.wake_ack_phrases = ["Yes {sir?"]
        assert WakeAck(config, synth=FakeSynth())._phrases == ["Yes {sir?"]
