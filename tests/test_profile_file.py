"""Switching profiles edits one line of a hand-written config.

config.yaml is full of comments explaining every setting, and a YAML round
trip would delete all of them. So the edit is a text edit, and what these
check is that it touches the `profile:` line inside `assistant:` and not a
byte more - not another section's `profile:`, not the comments, not the
line endings.
"""

import os

import pytest

from arnold.config import (
    ConfigError,
    IdentityWatcher,
    load_config,
    set_active_profile_in_file,
)

SAMPLE = """# top comment
device:
  id: desk

# who it is
assistant:
  profile: mycroft           # switch with `profile use`
  name: Mycroft
  voice: marin

code:
  projects:
    profile: "C:/not/this/one"
"""


@pytest.fixture
def path(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(SAMPLE, encoding="utf-8")
    return p


class TestEdit:
    def test_replaces_the_existing_profile_line_keeping_its_comment(self, path):
        set_active_profile_in_file(path, "jarvis")
        text = path.read_text(encoding="utf-8")
        assert "  profile: jarvis           # switch with `profile use`\n" in text

    def test_leaves_every_other_byte_alone(self, path):
        set_active_profile_in_file(path, "jarvis")
        before = SAMPLE.splitlines(keepends=True)
        after = path.read_text(encoding="utf-8").splitlines(keepends=True)
        assert len(before) == len(after)
        changed = [i for i, (a, b) in enumerate(zip(before, after)) if a != b]
        assert changed == [6]

    def test_does_not_touch_a_profile_key_in_another_section(self, path):
        set_active_profile_in_file(path, "jarvis")
        assert '    profile: "C:/not/this/one"\n' in path.read_text(encoding="utf-8")

    def test_inserts_after_assistant_when_absent_using_the_block_indent(self, tmp_path):
        p = tmp_path / "config.yaml"
        p.write_text("assistant:\n    name: Mycroft\n    voice: marin\nmqtt:\n    host: pi\n", encoding="utf-8")
        set_active_profile_in_file(p, "jarvis")
        assert p.read_text(encoding="utf-8") == (
            "assistant:\n    profile: jarvis\n    name: Mycroft\n    voice: marin\nmqtt:\n    host: pi\n"
        )

    def test_a_commented_out_profile_line_does_not_count(self, tmp_path):
        p = tmp_path / "config.yaml"
        p.write_text("assistant:\n  # profile: old\n  name: Mycroft\n", encoding="utf-8")
        set_active_profile_in_file(p, "jarvis")
        text = p.read_text(encoding="utf-8")
        assert text == "assistant:\n  profile: jarvis\n  # profile: old\n  name: Mycroft\n"

    def test_appends_a_block_when_there_is_no_assistant_section(self, tmp_path):
        p = tmp_path / "config.yaml"
        p.write_text("device:\n  id: desk\n", encoding="utf-8")
        set_active_profile_in_file(p, "jarvis")
        assert p.read_text(encoding="utf-8") == "device:\n  id: desk\n\nassistant:\n  profile: jarvis\n"
        assert load_config(p).assistant_name() == "Jarvis"

    def test_keeps_crlf_line_endings(self, tmp_path):
        p = tmp_path / "config.yaml"
        p.write_bytes(SAMPLE.replace("\n", "\r\n").encode("utf-8"))
        set_active_profile_in_file(p, "jarvis")
        raw = p.read_bytes()
        assert b"\r\n" in raw
        assert b"\n" not in raw.replace(b"\r\n", b"")
        assert b"  profile: jarvis           # switch with `profile use`\r\n" in raw

    def test_the_result_loads(self, path):
        set_active_profile_in_file(path, "jarvis")
        assert load_config(path).assistant_name() == "Jarvis"

    @pytest.mark.parametrize("bad", ["", "Dr. Who", "a b", "../x"])
    def test_rejects_a_non_slug_name(self, path, bad):
        with pytest.raises(ConfigError):
            set_active_profile_in_file(path, bad)
        assert path.read_text(encoding="utf-8") == SAMPLE

    def test_leaves_no_temp_file_behind(self, path):
        set_active_profile_in_file(path, "jarvis")
        assert sorted(p.name for p in path.parent.iterdir()) == ["config.yaml"]

    def test_refuses_a_one_line_assistant_section(self, tmp_path):
        """Appending a block would leave two `assistant:` keys, and YAML
        keeps only the last - the name and voice would silently vanish."""
        p = tmp_path / "config.yaml"
        original = "assistant: {name: Mycroft, voice: marin}\n"
        p.write_text(original, encoding="utf-8")
        with pytest.raises(ConfigError, match="one line"):
            set_active_profile_in_file(p, "jarvis")
        assert p.read_text(encoding="utf-8") == original

    def test_a_relative_model_path_is_config_relative(self, tmp_path):
        p = tmp_path / "config.yaml"
        p.write_text("voice:\n  wake_word: models/wake/hey_athena.onnx\n", encoding="utf-8")
        loaded = load_config(p)
        assert loaded.voice.wake_word == str(tmp_path / "models/wake/hey_athena.onnx")
        p.write_text("voice:\n  wake_word: hey_athena\n", encoding="utf-8")
        assert load_config(p).voice.wake_word == "hey_athena"


def _touch(path, seconds):
    os.utime(path, (seconds, seconds))


class TestIdentityWatcher:
    def test_reports_only_identity_changes(self, path):
        config = load_config(path)
        watcher = IdentityWatcher(config)
        assert watcher.changed(now=10.0) is None

        # An unrelated edit moves the mtime but not the identity.
        path.write_text(SAMPLE.replace("id: desk", "id: bench"), encoding="utf-8")
        _touch(path, 1_700_000_100)
        assert watcher.changed(now=20.0) is None

        set_active_profile_in_file(path, "jarvis")
        _touch(path, 1_700_000_200)
        fresh = watcher.changed(now=30.0)
        assert fresh is not None
        assert fresh.assistant_name() == "Jarvis"

    def test_is_rate_limited(self, path):
        config = load_config(path)
        watcher = IdentityWatcher(config, interval=2.0)
        assert watcher.changed(now=10.0) is None
        set_active_profile_in_file(path, "jarvis")
        _touch(path, 1_700_000_100)
        assert watcher.changed(now=11.0) is None  # too soon to look
        assert watcher.changed(now=12.5) is not None

    def test_a_broken_file_is_skipped_not_raised(self, path, caplog):
        config = load_config(path)
        watcher = IdentityWatcher(config)
        watcher.changed(now=10.0)
        path.write_text("assistant: [not a mapping\n", encoding="utf-8")
        _touch(path, 1_700_000_100)
        assert watcher.changed(now=20.0) is None
        assert "could not be read" in caplog.text
        # Not re-read until it changes again.
        assert watcher.changed(now=30.0) is None

    def test_no_source_path_means_nothing_to_watch(self):
        from arnold.config import Config

        assert IdentityWatcher(Config()).changed(now=10.0) is None
