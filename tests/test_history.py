"""The telemetry series.

The point of this file is not that numbers get written down - it is that the
trend it reports is honest. A projection made from too little data, or from a
line that happens to slope because the machine rebooted, is worse than no
projection at all: it will be said aloud as though it were a fact.
"""

from __future__ import annotations

import json
import time

import pytest

from arnold.history import (
    HOUR_SECONDS,
    History,
    _slope_per_day,
    flatten,
)

GB = 1024 ** 3


def snapshot(cpu=20.0, memory=50.0, free=500 * GB, temp=60.0):
    return {
        "ts": time.time(),
        "cpu": {"percent": cpu, "per_core": [1.0, 2.0]},
        "memory": {"percent": memory, "total_bytes": 64 * GB},
        "swap": {"percent": 3.0},
        "disks": {"C": {"percent": 46.0, "free_bytes": free, "total_bytes": 931 * GB}},
        "gpus": [{"index": 0, "utilization_percent": 12, "temperature_c": temp}],
        "network": {"sent_rate_bps": 1000.0, "recv_rate_bps": 2000.0},
        "processes": {"count": 300},
    }


@pytest.fixture
def store(tmp_path):
    return History(tmp_path / "history.json", fine=10, hourly=100)


class TestFlatten:
    def test_it_keeps_the_numbers_worth_a_series(self):
        sample = flatten(snapshot())
        assert sample["cpu.percent"] == 20.0
        assert sample["memory.percent"] == 50.0
        assert sample["disks.C.free_bytes"] == 500 * GB
        assert sample["gpus.0.temperature_c"] == 60.0

    def test_absent_hardware_is_simply_absent(self):
        bare = {"cpu": {"percent": 5.0}, "disks": {}, "gpus": []}
        sample = flatten(bare)
        assert sample == {"cpu.percent": 5.0}

    def test_nulls_and_booleans_are_not_series(self):
        sample = flatten({"cpu": {"percent": None}, "battery": {"percent": True}})
        assert sample == {}

    def test_infinities_do_not_reach_the_file(self):
        """json.dump writes Infinity, which is not JSON, and no reader survives it."""
        sample = flatten({"cpu": {"percent": float("inf")}})
        assert sample == {}


class TestFineRing:
    def test_samples_are_kept_in_order(self, store):
        for cpu in (10.0, 20.0, 30.0):
            store.record(snapshot(cpu=cpu))
        assert [s["cpu.percent"] for s in store.recent()] == [10.0, 20.0, 30.0]

    def test_the_ring_is_bounded(self, store):
        for _ in range(50):
            store.record(snapshot())
        assert len(store.recent(1000)) == 10

    def test_it_survives_a_restart(self, tmp_path):
        first = History(tmp_path / "history.json", fine=10)
        first.record(snapshot(cpu=42.0))
        again = History(tmp_path / "history.json", fine=10)
        assert again.recent()[-1]["cpu.percent"] == 42.0

    def test_a_corrupt_file_is_not_fatal(self, tmp_path):
        path = tmp_path / "history.json"
        path.write_text("{not json", encoding="utf-8")
        store = History(path)
        store.record(snapshot())
        assert len(store.recent()) == 1

    def test_the_file_is_valid_json_after_every_write(self, store):
        store.record(snapshot())
        payload = json.loads(store.path.read_text(encoding="utf-8"))
        assert payload["samples"][0]["cpu.percent"] == 20.0


class TestHourlyBuckets:
    def test_samples_fold_into_one_bucket_per_hour(self, store):
        # Pinned to the top of the hour, as the next test does. Starting from
        # `now` meant six one-minute samples straddled two buckets whenever
        # this happened to run in the last few minutes of an hour, and the
        # test failed on the clock rather than on the code.
        base = (time.time() // HOUR_SECONDS) * HOUR_SECONDS
        for i in range(6):
            store.record(snapshot(cpu=10.0 * i), now=base + i * 60)
        # All within the hour, so nothing has closed yet, but the series can
        # still see the open one.
        points = store.series("cpu.percent", hours=2)
        assert len(points) == 1
        assert points[0][1] == pytest.approx(25.0)  # mean of 0..50

    def test_a_new_hour_closes_the_last(self, store):
        base = (time.time() // HOUR_SECONDS) * HOUR_SECONDS
        store.record(snapshot(cpu=10.0), now=base + 10)
        store.record(snapshot(cpu=30.0), now=base + HOUR_SECONDS + 10)
        assert len(store.series("cpu.percent", hours=5)) == 2

    def test_a_metric_appearing_late_does_not_skew_its_mean(self, store):
        """A GPU plugged in mid-hour must average over its own samples only."""
        base = time.time()
        store.record({"cpu": {"percent": 10.0}}, now=base)
        store.record(snapshot(temp=80.0), now=base + 60)
        points = store.series("gpus.0.temperature_c", hours=2)
        assert points[0][1] == pytest.approx(80.0)

    def test_the_open_hour_survives_a_restart(self, tmp_path):
        base = (time.time() // HOUR_SECONDS) * HOUR_SECONDS + 5
        first = History(tmp_path / "history.json")
        first.record(snapshot(cpu=10.0), now=base)
        first.flush()

        again = History(tmp_path / "history.json")
        again.record(snapshot(cpu=30.0), now=base + 60)
        points = again.series("cpu.percent", hours=3)
        assert len(points) == 1, "the restart split one hour into two buckets"
        assert points[0][1] == pytest.approx(20.0)


class TestSlope:
    def test_a_straight_line_has_its_gradient(self):
        points = [(i * HOUR_SECONDS, i * 2.0) for i in range(10)]
        assert _slope_per_day(points) == pytest.approx(48.0)  # 2/hour = 48/day

    def test_too_few_points_say_nothing(self):
        assert _slope_per_day([(0.0, 1.0), (HOUR_SECONDS, 2.0)]) is None

    def test_too_short_a_span_says_nothing(self):
        points = [(float(i * 60), float(i)) for i in range(10)]
        assert _slope_per_day(points) is None

    def test_a_flat_line_is_flat(self):
        points = [(i * HOUR_SECONDS, 50.0) for i in range(10)]
        assert _slope_per_day(points) == pytest.approx(0.0)

    def test_one_outlier_does_not_own_the_line(self):
        """First-to-last would; least squares should not."""
        steady = [(i * HOUR_SECONDS, 100.0 - i) for i in range(24)]
        with_spike = steady[:-1] + [(23 * HOUR_SECONDS, 5.0)]
        assert _slope_per_day(with_spike) > _slope_per_day(steady) * 3
        # ...but it is still recognisably a downward line, not a cliff.
        assert _slope_per_day(with_spike) < 0


class TestTrend:
    def build(self, tmp_path, per_hour, hours=48, start=500 * GB):
        store = History(tmp_path / "history.json", fine=5)
        base = (time.time() // HOUR_SECONDS) * HOUR_SECONDS - hours * HOUR_SECONDS
        for i in range(hours):
            store.record(
                snapshot(free=start + per_hour * i), now=base + i * HOUR_SECONDS + 30
            )
        return store

    def test_a_falling_disk_projects_a_date(self, tmp_path):
        store = self.build(tmp_path, per_hour=-2 * GB)  # 48 GB a day
        trend = store.trend("disks.C.free_bytes", hours=24 * 7)
        assert trend is not None
        assert trend["per_day"] == pytest.approx(-48 * GB, rel=0.05)
        # Started at 500 GB, lost 96 over two days, so ~404 left at ~48/day.
        assert trend["days_to_zero"] == pytest.approx(8.4, rel=0.1)

    def test_a_rising_series_never_runs_out(self, tmp_path):
        store = self.build(tmp_path, per_hour=+1 * GB)
        trend = store.trend("disks.C.free_bytes", hours=24 * 7)
        assert trend["per_day"] > 0
        assert "days_to_zero" not in trend

    def test_a_fresh_install_refuses_to_guess(self, store):
        store.record(snapshot())
        assert store.trend("disks.C.free_bytes") is None

    def test_an_unknown_metric_is_none_not_an_error(self, tmp_path):
        assert self.build(tmp_path, per_hour=-GB).trend("nothing.like.this") is None

    def test_the_window_is_respected(self, tmp_path):
        """Asking about the last day must not measure across the last week."""
        store = self.build(tmp_path, per_hour=-2 * GB, hours=72)
        assert store.trend("disks.C.free_bytes", hours=24)["span_hours"] <= 25
