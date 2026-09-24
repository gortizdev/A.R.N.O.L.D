from arnold.humanize import (
    bytes_human,
    bytes_speech,
    duration_speech,
    join_speech,
    sentence_case,
)
from arnold.monitors.collector import get_metric


class TestMetricPaths:
    SNAP = {
        "cpu": {"percent": 42.0},
        "disks": {"C": {"free_bytes": 100, "percent": 90.0}},
        "gpus": [{"index": 0, "temperature_c": 71}],
        "processes": {"watched": {"steam.exe": {"running": True}}},
    }

    def test_simple_path(self):
        assert get_metric(self.SNAP, "cpu.percent") == 42.0

    def test_nested_path(self):
        assert get_metric(self.SNAP, "disks.C.free_bytes") == 100

    def test_list_index(self):
        assert get_metric(self.SNAP, "gpus.0.temperature_c") == 71

    def test_key_containing_a_dot(self):
        """`steam.exe` is one key, not two path segments."""
        assert get_metric(self.SNAP, "processes.watched.steam.exe.running") is True

    def test_missing_key_returns_none(self):
        assert get_metric(self.SNAP, "disks.Z.free_bytes") is None

    def test_missing_list_index_returns_none(self):
        assert get_metric(self.SNAP, "gpus.5.temperature_c") is None

    def test_descending_into_scalar_returns_none(self):
        assert get_metric(self.SNAP, "cpu.percent.nope") is None


class TestByteFormatting:
    def test_bytes_stay_bytes(self):
        assert bytes_human(512) == "512 B"

    def test_scales_to_gigabytes(self):
        assert bytes_human(10 * 1024**3) == "10.0 GB"

    def test_speech_drops_trailing_zeros(self):
        assert bytes_speech(2.5 * 1024**3) == "2.5 gigabytes"

    def test_speech_rounds_large_values(self):
        assert bytes_speech(143.27 * 1024**3) == "143 gigabytes"

    def test_singular_unit(self):
        assert bytes_speech(1024**3) == "1 gigabyte"

    def test_singular_byte(self):
        assert bytes_speech(1) == "1 byte"

    def test_handles_none(self):
        assert bytes_speech(None) == "an unknown amount"
        assert bytes_human(None) == "unknown"


class TestDurationSpeech:
    def test_seconds(self):
        assert duration_speech(45) == "45 seconds"

    def test_singular_second(self):
        assert duration_speech(1) == "1 second"

    def test_minutes(self):
        assert duration_speech(300) == "5 minutes"

    def test_two_units_only(self):
        assert duration_speech(4 * 86400 + 3 * 3600 + 30 * 60) == "4 days and 3 hours"

    def test_singular_units(self):
        assert duration_speech(86400 + 3600) == "1 day and 1 hour"

    def test_sub_minute_after_rounding(self):
        assert duration_speech(0) == "0 seconds"


class TestJoinSpeech:
    def test_single(self):
        assert join_speech(["a"]) == "a"

    def test_pair_has_no_comma(self):
        assert join_speech(["a", "b"]) == "a and b"

    def test_three_or_more(self):
        assert join_speech(["a", "b", "c"]) == "a, b, and c"

    def test_empty(self):
        assert join_speech([]) == ""

    def test_drops_blanks(self):
        assert join_speech(["a", "", "b"]) == "a and b"


class TestSentenceCase:
    def test_preserves_later_capitals(self):
        """`str.capitalize()` would turn this into 'Drive c has ...'."""
        assert sentence_case("drive C has 91 gigabytes free") == "Drive C has 91 gigabytes free"

    def test_empty_string(self):
        assert sentence_case("") == ""
