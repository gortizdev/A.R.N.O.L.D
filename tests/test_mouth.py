"""Lip-sync: the mouth must follow the ear, not the network.

The model streams audio about six times faster than real time, so a whole
reply lands in a second or two. Publishing the envelope as it arrives made the
face perform the entire sentence up front and then fall back to its synthetic
idle loop for the rest - which is what "the mouth pulses on a timer" looked
like from the outside.
"""

import time

import numpy as np
import pytest

from arnold.mouth import TUNING, MouthAnalyser, envelope_from_wav
from arnold.voice.realtime import RATE, RealtimeConversation

SPEECH_HZ = 200


def tone(seconds, rate=RATE, freq=SPEECH_HZ, amp=0.6, bright=0.0):
    """A voiced-sounding buffer, optionally with sibilant energy on top."""
    t = np.arange(int(rate * seconds)) / rate
    sig = sum(np.sin(2 * np.pi * freq * h * t) / h**1.5 for h in range(1, 20))
    sig = sig / np.abs(sig).max()
    if bright:
        rng = np.random.default_rng(0)
        hiss = rng.standard_normal(len(t))
        hiss = np.diff(np.concatenate([[0.0], hiss]))  # crude high-pass
        sig = sig * (1 - bright) + bright * hiss / np.abs(hiss).max()
    return (sig * amp * 20000).astype(np.int16)


class TestAnalyser:
    def test_loud_speech_opens_the_mouth(self):
        frame = MouthAnalyser().analyse(tone(0.02), RATE)
        assert frame["open"] > 0.5

    def test_silence_closes_it(self):
        analyser = MouthAnalyser()
        analyser.analyse(tone(0.02), RATE)  # establish a reference
        for _ in range(5):
            frame = analyser.analyse(np.zeros(480, dtype=np.int16), RATE)
        assert frame["open"] == 0.0

    def test_sibilance_widens_the_mouth(self):
        analyser = MouthAnalyser()
        vowel = analyser.analyse(tone(0.02), RATE)["wide"]
        sibilant = analyser.analyse(tone(0.02, bright=0.9), RATE)["wide"]
        assert sibilant > vowel + 0.2

    def test_width_is_not_judged_during_a_gap(self):
        """A ratio against near-zero energy swings wildly and flickers."""
        analyser = MouthAnalyser()
        analyser.analyse(tone(0.02), RATE)
        quiet = analyser.analyse((np.random.default_rng(1).standard_normal(480) * 3).astype(np.int16), RATE)
        assert quiet["wide"] == pytest.approx(TUNING.rest_width)

    def test_normalisation_is_per_utterance(self):
        """A quiet reply must animate as fully as a loud one."""
        loud = MouthAnalyser()
        quiet = MouthAnalyser()
        for _ in range(6):
            loud_frame = loud.analyse(tone(0.02, amp=0.9), RATE)
            quiet_frame = quiet.analyse(tone(0.02, amp=0.05), RATE)
        assert quiet_frame["open"] == pytest.approx(loud_frame["open"], abs=0.05)

    def test_reset_forgets_the_previous_utterance(self):
        analyser = MouthAnalyser()
        for _ in range(10):
            analyser.analyse(tone(0.02, amp=1.0), RATE)
        analyser.reset()
        assert analyser.analyse(tone(0.02, amp=0.05), RATE)["open"] > 0.5

    def test_values_stay_in_range_on_anything(self):
        analyser = MouthAnalyser()
        rng = np.random.default_rng(4)
        for _ in range(50):
            pcm = (rng.standard_normal(480) * rng.integers(0, 30000)).astype(np.int16)
            frame = analyser.analyse(pcm, RATE)
            assert 0.0 <= frame["open"] <= 1.0
            assert 0.0 <= frame["wide"] <= 1.0

    def test_a_runt_chunk_is_survivable(self):
        assert MouthAnalyser().analyse(np.zeros(4, dtype=np.int16), RATE)["open"] == 0.0


class TestOfflineEnvelope:
    def test_a_frame_per_hop(self):
        frames = envelope_from_wav(tone(1.0), RATE)
        assert len(frames) == pytest.approx(1.0 / TUNING.hop_seconds, abs=1)

    def test_frames_carry_their_own_timestamps(self):
        frames = envelope_from_wav(tone(0.5), RATE)
        assert frames[0]["at"] == 0.0
        assert frames[-1]["at"] == pytest.approx(0.5 - TUNING.hop_seconds, abs=0.03)


class TestPlaybackSync:
    """The heart of it: publish when heard, not when received."""

    def conversation(self):
        published = []
        rt = RealtimeConversation("key", object(), [], lambda n, a: None)
        rt.on_levels = lambda frame: published.append((time.monotonic(), frame))
        return rt, published

    def test_a_burst_of_audio_is_not_a_burst_of_frames(self):
        """Eight seconds of speech arriving at once must not animate at once."""
        rt, published = self.conversation()
        start = time.monotonic()
        for _ in range(20):  # 20 chunks of 0.4s, delivered instantly
            rt._enqueue_audio(tone(0.4))

        with rt._pending_lock:
            scheduled = [when for when, _ in rt._pending_levels]
        assert len(scheduled) > 300  # a frame per 20ms hop across 8 seconds
        span = scheduled[-1] - start
        assert span == pytest.approx(8.0, abs=0.4), (
            "frames should be spread across the audio's own duration"
        )
        # Nothing has actually been published yet - it is all in the future.
        assert not published

    def test_frames_are_scheduled_in_order(self):
        rt, _ = self.conversation()
        for _ in range(4):
            rt._enqueue_audio(tone(0.3))
        with rt._pending_lock:
            times = [when for when, _ in rt._pending_levels]
        assert times == sorted(times)

    def test_the_pacer_releases_frames_as_they_come_due(self):
        rt, published = self.conversation()
        rt._enqueue_audio(tone(0.25))
        import threading

        threading.Thread(target=rt._level_pacer, daemon=True).start()
        time.sleep(0.12)
        early = len(published)
        time.sleep(0.25)
        rt._stop.set()
        rt._level_wake.set()

        assert 0 < early < len(published), "frames should trickle out, not dump"

    def test_barge_in_drops_what_was_queued(self):
        """Audio that will never be heard must not drive the mouth."""
        rt, _ = self.conversation()
        rt._enqueue_audio(tone(2.0))
        assert rt._pending_levels
        rt._handle_event({"type": "input_audio_buffer.speech_started"})
        assert not rt._pending_levels

    def test_barge_in_also_forgets_the_loudness_reference(self):
        rt, _ = self.conversation()
        for _ in range(4):
            rt._enqueue_audio(tone(0.2, amp=1.0))
        rt._handle_event({"type": "input_audio_buffer.speech_started"})
        assert rt.analyser._peak == 0.0

    def test_the_play_head_still_advances_correctly(self):
        """The schedule is derived from it, so it must stay accurate."""
        rt, _ = self.conversation()
        before = time.monotonic()
        rt._enqueue_audio(tone(1.0))
        rt._enqueue_audio(tone(1.0))
        assert rt._play_head - before == pytest.approx(2.0, abs=0.1)
