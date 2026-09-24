"""The state file is what lets a one-shot `exec` - the path Jarvis uses over
SSH - answer questions a fresh process cannot work out for itself."""

import json
import time

from arnold.state import read_state, write_state


def test_roundtrip(tmp_path):
    path = tmp_path / "state.json"
    write_state(path, {"alerts": {"active": ["disk_low"], "count": 1}})
    state = read_state(path)
    assert state is not None
    assert state["alerts"]["active"] == ["disk_low"]


def test_timestamp_is_added(tmp_path):
    path = tmp_path / "state.json"
    write_state(path, {"alerts": {}})
    assert abs(read_state(path)["ts"] - time.time()) < 5


def test_missing_file_returns_none(tmp_path):
    assert read_state(tmp_path / "absent.json") is None


def test_stale_state_is_rejected(tmp_path):
    """An agent that died an hour ago must not report its alerts as current."""
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"alerts": {"count": 3}, "ts": time.time() - 3600}))
    assert read_state(path) is None


def test_fresh_state_within_max_age(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"alerts": {"count": 3}, "ts": time.time() - 5}))
    assert read_state(path) is not None


def test_corrupt_file_returns_none(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not valid json")
    assert read_state(path) is None


def test_non_object_payload_returns_none(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps([1, 2, 3]))
    assert read_state(path) is None


def test_missing_timestamp_returns_none(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"alerts": {}}))
    assert read_state(path) is None


def test_write_creates_parent_directory(tmp_path):
    path = tmp_path / "nested" / "dir" / "state.json"
    write_state(path, {"alerts": {}})
    assert path.is_file()


def test_write_leaves_no_temp_files(tmp_path):
    """Writes go via a temp file + atomic replace; none should be left behind."""
    path = tmp_path / "state.json"
    for _ in range(5):
        write_state(path, {"alerts": {}})
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


def test_unwritable_path_does_not_raise(tmp_path):
    """Telemetry must not stop just because state could not be written."""
    write_state(tmp_path / "state.json" / "impossible", {"alerts": {}})
