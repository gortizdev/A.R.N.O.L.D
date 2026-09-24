"""The gate in front of the wake-word model.

A single frame over the line is not a wake word. These stand in a fake
openWakeWord so the scores are chosen rather than heard, and check the two
things that matter: that a fluke does not fire, and that a real run does.
"""

import logging
import os

import numpy as np
import openwakeword.model as oww
import pytest

from arnold.voice.audio import AudioError, WakeWordDetector, resolve_wake_word

FRAME = np.zeros(1280, dtype=np.float32)


class FakeModel:
    """Scripted scores. The buffer keeps the raw score and predict() returns
    the gated one, which is exactly how the real model separates the two.

    Keys by file stem when handed a path, as openWakeWord does."""

    instances: list["FakeModel"] = []

    def __init__(self, wakeword_models=None, inference_framework="onnx", vad_threshold=0.0, **_):
        self.wakeword_models = wakeword_models
        self.vad_threshold = vad_threshold
        key = "hey_jarvis"
        if wakeword_models:
            first = wakeword_models[0]
            key = os.path.splitext(os.path.basename(first))[0] if os.path.exists(first) else first
        self.key = key
        self.models = {key: object()}
        self.prediction_buffer = {key: []}
        self.scores: list[float] = []
        self.gate_open = True
        self.resets = 0
        FakeModel.instances.append(self)

    def predict(self, x):
        assert x.dtype == np.int16
        score = self.scores.pop(0) if self.scores else 0.0
        self.prediction_buffer[self.key].append(score)
        return {self.key: score if self.gate_open else 0.0}

    def reset(self):
        self.resets += 1
        self.prediction_buffer[self.key].clear()


@pytest.fixture
def make(monkeypatch):
    monkeypatch.setattr(oww, "Model", FakeModel)
    FakeModel.instances.clear()

    def build(**kwargs):
        detector = WakeWordDetector("hey_jarvis", 0.5, **kwargs)
        return detector, FakeModel.instances[-1]

    return build


def feed(detector, model, scores):
    model.scores = list(scores)
    return [detector.triggered(FRAME) for _ in scores]


class TestPatience:
    def test_default_is_the_raw_model(self, make):
        detector, model = make()
        assert feed(detector, model, [0.9]) == [True]

    def test_a_single_frame_is_not_enough(self, make):
        detector, model = make(patience=2)
        assert feed(detector, model, [0.9, 0.1, 0.1]) == [False, False, False]

    def test_two_in_a_row_fire_on_the_second(self, make):
        detector, model = make(patience=2)
        assert feed(detector, model, [0.9, 0.8]) == [False, True]

    def test_the_run_has_to_be_consecutive(self, make):
        detector, model = make(patience=2)
        assert feed(detector, model, [0.9, 0.1, 0.9, 0.1]) == [False] * 4

    def test_the_threshold_still_applies_to_every_frame(self, make):
        detector, model = make(patience=2)
        assert feed(detector, model, [0.9, 0.49]) == [False, False]

    def test_firing_starts_the_count_over(self, make):
        detector, model = make(patience=2)
        assert feed(detector, model, [0.9, 0.9, 0.9]) == [False, True, False]

    def test_reset_forgets_a_run_in_progress(self, make):
        detector, model = make(patience=2)
        feed(detector, model, [0.9])
        detector.reset()
        assert feed(detector, model, [0.9]) == [False]
        assert model.resets == 1

    def test_patience_below_one_is_one(self, make):
        detector, _ = make(patience=0)
        assert detector.patience == 1


class TestNearMisses:
    def test_a_run_that_fell_short_is_logged(self, make, caplog):
        detector, model = make(patience=3)
        with caplog.at_level(logging.INFO, logger="arnold.voice.audio"):
            feed(detector, model, [0.6, 0.7, 0.1])
        assert "peaked at 0.70 for 2 frame(s)" in caplog.text
        assert "need 3 in a row at 0.50" in caplog.text

    def test_silence_is_not_logged(self, make, caplog):
        detector, model = make(patience=2)
        with caplog.at_level(logging.INFO, logger="arnold.voice.audio"):
            feed(detector, model, [0.1, 0.2, 0.0])
        assert "wake-word candidate" not in caplog.text

    def test_last_score_is_what_was_heard(self, make):
        detector, model = make()
        feed(detector, model, [0.37])
        assert detector.last_score == pytest.approx(0.37)


class TestVoiceGate:
    def test_threshold_reaches_the_model(self, make):
        _, model = make(vad_threshold=0.6)
        assert model.vad_threshold == 0.6

    def test_off_by_default(self, make):
        _, model = make()
        assert model.vad_threshold == 0.0

    def test_uses_the_gated_score_not_the_buffer(self, make):
        """The VAD zeroes what predict() returns and leaves the buffer alone.
        Reading the buffer would quietly bypass the gate."""
        detector, model = make(vad_threshold=0.5)
        model.gate_open = False
        assert feed(detector, model, [0.95, 0.95]) == [False, False]
        assert model.prediction_buffer["hey_jarvis"] == [0.95, 0.95]
        assert detector.last_score == 0.0


class TestResolution:
    """What config says versus what openWakeWord is handed and keys on."""

    def test_bundled_name_passes_through(self):
        model = resolve_wake_word("hey_jarvis")
        assert (model.kind, model.key, model.model_arg) == ("bundled", "hey_jarvis", "hey_jarvis")

    def test_a_path_is_keyed_by_its_stem(self, tmp_path):
        path = tmp_path / "hey_athena.onnx"
        path.write_bytes(b"x")
        model = resolve_wake_word(str(path))
        assert model.kind == "file"
        assert model.key == "hey_athena"
        assert os.path.isabs(model.model_arg)

    def test_a_missing_path_is_an_audio_error(self, tmp_path):
        with pytest.raises(AudioError, match="not found"):
            resolve_wake_word(str(tmp_path / "nope.onnx"))

    def test_a_bare_name_resolves_under_the_wake_dir(self, tmp_path):
        (tmp_path / "hey_athena.onnx").write_bytes(b"x")
        model = resolve_wake_word("hey_athena", tmp_path)
        assert model.kind == "custom"
        assert model.key == "hey_athena"
        assert model.model_arg == str((tmp_path / "hey_athena.onnx").resolve())

    def test_the_wake_dir_beats_a_bundled_name_of_the_same_stem(self, tmp_path):
        (tmp_path / "hey_jarvis.onnx").write_bytes(b"x")
        assert resolve_wake_word("hey_jarvis", tmp_path).kind == "custom"
        assert resolve_wake_word("hey_jarvis").kind == "bundled"

    def test_an_empty_wake_word_is_refused(self):
        with pytest.raises(AudioError):
            resolve_wake_word("  ")

    def test_detector_uses_the_stem_key_for_a_file_model(self, monkeypatch, tmp_path):
        monkeypatch.setattr(oww, "Model", FakeModel)
        FakeModel.instances.clear()
        path = tmp_path / "hey_athena.onnx"
        path.write_bytes(b"x")
        detector = WakeWordDetector(str(path), 0.5)
        model = FakeModel.instances[-1]
        assert detector.model_key == "hey_athena"
        assert model.wakeword_models == [str(path.resolve())]
        assert feed(detector, model, [0.9]) == [True]

    def test_detector_finds_a_custom_model_by_name(self, monkeypatch, tmp_path):
        monkeypatch.setattr(oww, "Model", FakeModel)
        (tmp_path / "hey_athena.onnx").write_bytes(b"x")
        detector = WakeWordDetector("hey_athena", 0.5, wake_dir=tmp_path)
        assert detector.model.kind == "custom"
        assert detector.wake_word == "hey_athena"

    def test_a_broken_custom_file_does_not_fall_back_to_every_bundled_model(
        self, monkeypatch, tmp_path
    ):
        attempts = []

        class Broken(FakeModel):
            def __init__(self, wakeword_models=None, **kw):
                attempts.append(wakeword_models)
                if wakeword_models and os.path.exists(wakeword_models[0]):
                    raise RuntimeError("not an onnx graph")
                super().__init__(wakeword_models, **kw)

        monkeypatch.setattr(oww, "Model", Broken)
        path = tmp_path / "hey_athena.onnx"
        path.write_bytes(b"garbage")
        with pytest.raises(AudioError, match="could not load"):
            WakeWordDetector(str(path), 0.5)
        assert len(attempts) == 1

    def test_a_bundled_name_still_falls_back_to_the_full_set(self, monkeypatch):
        attempts = []

        class Picky(FakeModel):
            def __init__(self, wakeword_models=None, **kw):
                attempts.append(wakeword_models)
                if wakeword_models is not None:
                    raise ValueError("Could not find pretrained model")
                super().__init__(None, **kw)

        monkeypatch.setattr(oww, "Model", Picky)
        WakeWordDetector("hey_jarvis", 0.5)
        assert attempts == [["hey_jarvis"], None]
