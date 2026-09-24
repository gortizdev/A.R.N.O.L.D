"""The `wake` and `profile` commands.

Nothing here opens a microphone or trains anything. What matters is that a
custom model is found and marked, that install refuses what it should, that
training without its dependencies says exactly what to do, and that the
stages that do run go through openWakeWord's own trainer.
"""

import types

import pytest

from arnold import cli
from arnold.config import Config
from arnold.voice import wake_tools


@pytest.fixture
def config(tmp_path):
    cfg = Config()
    cfg.voice.wake_word_dir = str(tmp_path / "wake")
    return cfg


@pytest.fixture
def config_file(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("assistant:\n  profile: mycroft\n", encoding="utf-8")
    return path


class TestList:
    def test_marks_the_active_model(self, config, monkeypatch):
        monkeypatch.setattr(wake_tools, "bundled_wake_words", lambda: ["hey_jarvis", "hey_mycroft"])
        config.voice.wake_word = "hey_mycroft"
        entries = {e["name"]: e for e in wake_tools.list_models(config)}
        assert entries["hey_mycroft"]["active"]
        assert not entries["hey_jarvis"]["active"]

    def test_lists_custom_models_and_marks_one_by_name(self, config, monkeypatch, tmp_path):
        monkeypatch.setattr(wake_tools, "bundled_wake_words", lambda: ["hey_jarvis"])
        folder = tmp_path / "wake"
        folder.mkdir()
        (folder / "hey_athena.onnx").write_bytes(b"x")
        config.voice.wake_word = "hey_athena"
        entries = {e["name"]: e for e in wake_tools.list_models(config)}
        assert entries["hey_athena"]["kind"] == "custom"
        assert entries["hey_athena"]["active"]
        assert not entries["hey_jarvis"]["active"]


    def test_a_model_given_by_path_is_listed_as_active(self, config, monkeypatch, tmp_path):
        monkeypatch.setattr(wake_tools, "bundled_wake_words", lambda: ["hey_jarvis"])
        model = tmp_path / "elsewhere" / "hey_athena.onnx"
        model.parent.mkdir()
        model.write_bytes(b"x")
        config.voice.wake_word = str(model)
        entries = {e["name"]: e for e in wake_tools.list_models(config)}
        assert entries["hey_athena"]["kind"] == "file"
        assert entries["hey_athena"]["active"]


class TestInstall:
    def test_copies_under_the_wake_dir_and_refuses_to_overwrite(self, config, tmp_path):
        src = tmp_path / "dl" / "Hey_Athena.onnx"
        src.parent.mkdir()
        src.write_bytes(b"model")
        dest = wake_tools.install_model(config, src, check=False)
        assert dest == tmp_path / "wake" / "hey_athena.onnx"
        assert dest.read_bytes() == b"model"

        src.write_bytes(b"newer")
        with pytest.raises(wake_tools.WakeToolError, match="--force"):
            wake_tools.install_model(config, src, check=False)
        wake_tools.install_model(config, src, check=False, force=True)
        assert dest.read_bytes() == b"newer"

    def test_can_be_renamed(self, config, tmp_path):
        src = tmp_path / "out.onnx"
        src.write_bytes(b"model")
        dest = wake_tools.install_model(config, src, "hey_athena", check=False)
        assert dest.name == "hey_athena.onnx"

    def test_rejects_non_onnx(self, config, tmp_path):
        src = tmp_path / "hey_athena.tflite"
        src.write_bytes(b"model")
        with pytest.raises(wake_tools.WakeToolError, match="onnx"):
            wake_tools.install_model(config, src, check=False)

    def test_rejects_a_bad_name(self, config, tmp_path):
        src = tmp_path / "hey athena.onnx"
        src.write_bytes(b"model")
        with pytest.raises(wake_tools.WakeToolError, match="name"):
            wake_tools.install_model(config, src, check=False)

    def test_checks_that_the_model_loads(self, config, tmp_path, monkeypatch):
        from arnold.voice.audio import AudioError

        def broken(*a, **k):
            raise AudioError("not an onnx graph")

        monkeypatch.setattr(wake_tools, "WakeWordDetector", broken)
        src = tmp_path / "hey_athena.onnx"
        src.write_bytes(b"garbage")
        with pytest.raises(wake_tools.WakeToolError, match="would not load"):
            wake_tools.install_model(config, src)
        assert not (tmp_path / "wake").exists()


class TestTrain:
    def test_without_deps_it_names_them_and_the_notebook(self, config_file, monkeypatch, capsys):
        monkeypatch.setattr(wake_tools, "missing_training_deps", lambda: ["torch", "speechbrain"])
        code = cli.main(["-c", str(config_file), "wake", "train", "hey", "athena"])
        out = capsys.readouterr().out
        assert code == 2
        assert "wake-train" in out
        assert "torch, speechbrain" in out
        assert wake_tools.COLAB_URL in out
        assert '"hey athena"' in out
        assert "wake install hey_athena.onnx" in out

    def test_without_assets_it_lists_them(self, config_file, monkeypatch, capsys, tmp_path):
        monkeypatch.setattr(wake_tools, "missing_training_deps", lambda: [])
        code = cli.main([
            "-c", str(config_file), "wake", "train", "hey athena", "--data-dir", str(tmp_path / "train"),
        ])
        out = capsys.readouterr().out
        assert code == 2
        assert "piper-sample-generator" in out
        assert "features/acav100m.npy" in out
        assert "huggingface.co/datasets/davidscripka/openwakeword_features" in out
        assert wake_tools.COLAB_URL in out

    def test_the_training_config_has_every_key_train_py_reads(self, tmp_path):
        layout = wake_tools.TrainingLayout(tmp_path)
        path = wake_tools.write_training_config(layout, "hey athena", "hey_athena", steps=500, samples=200)
        import yaml

        written = yaml.safe_load(path.read_text(encoding="utf-8"))
        needed = {
            "target_phrase", "model_name", "output_dir", "n_samples", "n_samples_val",
            "tts_batch_size", "custom_negative_phrases", "piper_sample_generator_path",
            "rir_paths", "background_paths", "background_paths_duplication_rate",
            "augmentation_rounds", "augmentation_batch_size", "feature_data_files",
            "false_positive_validation_data_path", "batch_n_per_class", "model_type",
            "layer_size", "steps", "max_negative_weight", "target_false_positives_per_hour",
        }
        assert needed <= set(written)
        assert written["target_phrase"] == ["hey athena"]
        assert written["steps"] == 500
        assert written["n_samples_val"] == 100
        assert {"positive", "adversarial_negative"} <= set(written["batch_n_per_class"])

    def test_stages_go_through_train_py_via_process_run(self, config, tmp_path, monkeypatch):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            return types.SimpleNamespace(returncode=0)

        def fake_train(training_config, out=print):
            model = training_config.parent / "hey_athena.onnx"
            model.write_bytes(b"trained")
            return model

        monkeypatch.setattr(wake_tools.process, "run", fake_run)
        monkeypatch.setattr(wake_tools, "_train_in_process", fake_train)

        model = wake_tools.train(config, "hey athena", data_dir=tmp_path / "train", out=lambda *_: None)
        assert model.read_bytes() == b"trained"
        flags = [a[-1] for a in calls]
        assert flags == ["--generate_clips", "--augment_clips"]
        assert all("--train_model" not in a for a in calls)
        assert all(a[1:3] == ["-m", "openwakeword.train"] for a in calls)

    def test_a_failed_stage_stops_the_run(self, config, tmp_path, monkeypatch):
        monkeypatch.setattr(
            wake_tools.process, "run", lambda argv, **k: types.SimpleNamespace(returncode=1)
        )
        with pytest.raises(wake_tools.WakeToolError, match="generate stage failed"):
            wake_tools.train(config, "hey athena", data_dir=tmp_path, out=lambda *_: None)

    def test_a_subset_of_stages_can_run(self, config, tmp_path, monkeypatch):
        calls = []
        monkeypatch.setattr(
            wake_tools.process, "run",
            lambda argv, **k: calls.append(argv) or types.SimpleNamespace(returncode=0),
        )
        (tmp_path / "out").mkdir()
        (tmp_path / "out" / "hey_athena.onnx").write_bytes(b"old")
        wake_tools.train(
            config, "hey athena", data_dir=tmp_path, stages=("augment",), out=lambda *_: None
        )
        assert [a[-1] for a in calls] == ["--augment_clips"]

    def test_train_honours_as(self, config_file, monkeypatch, tmp_path):
        seen = {}

        def fake_train(config, phrase, name=None, **kwargs):
            seen["name"] = name
            model = tmp_path / "out.onnx"
            model.write_bytes(b"trained")
            return model

        monkeypatch.setattr(wake_tools, "missing_training_deps", lambda: [])
        monkeypatch.setattr(wake_tools.TrainingLayout, "missing", lambda self: [])
        monkeypatch.setattr(wake_tools, "train", fake_train)
        monkeypatch.setattr(
            wake_tools, "install_model",
            lambda config, src, name=None, **k: tmp_path / f"{name}.onnx",
        )
        code = cli.main(["-c", str(config_file), "wake", "train", "hey athena", "--as", "athena"])
        assert code == 0
        assert seen["name"] == "athena"

    def test_phrase_to_name(self):
        assert wake_tools.phrase_to_name("Hey, Athena!") == "hey_athena"
        with pytest.raises(wake_tools.WakeToolError):
            wake_tools.phrase_to_name("!!!")


class TestProfileCli:
    def test_list_prints_the_active_one(self, config_file, capsys):
        assert cli.main(["-c", str(config_file), "profile"]) == 0
        out = capsys.readouterr().out
        assert "* mycroft" in out
        assert "  jarvis" in out

    def test_use_updates_the_file(self, config_file, capsys):
        assert cli.main(["-c", str(config_file), "profile", "use", "jarvis"]) == 0
        assert "profile: jarvis" in config_file.read_text(encoding="utf-8")
        assert "Jarvis" in capsys.readouterr().out

    def test_use_with_an_unknown_name_fails_cleanly(self, config_file, capsys):
        assert cli.main(["-c", str(config_file), "profile", "use", "hal"]) == 1
        assert "hal" in capsys.readouterr().err
        assert "profile: mycroft" in config_file.read_text(encoding="utf-8")

    def test_show(self, config_file, capsys):
        assert cli.main(["-c", str(config_file), "profile", "show"]) == 0
        out = capsys.readouterr().out
        assert "Mycroft" in out
        assert "hey_mycroft" in out
