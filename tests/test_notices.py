"""Speaking without being asked.

Most of these are about *not* speaking. An assistant that volunteers something
useful once a week is a good one; the same assistant with the policy removed is
one you turn off, so the interesting cases are all the ways a notice must be
kept quiet.
"""

from __future__ import annotations

import time
from datetime import datetime

import pytest

from arnold.config import Config
from arnold.history import HOUR_SECONDS, History
from arnold.notices import (
    Notice,
    NoticeBook,
    ProactiveEngine,
    RuleJudge,
    World,
    _in_quiet_hours,
    disk_filling,
    log_errors,
    memory_creeping,
)

GB = 1024 ** 3


@pytest.fixture
def config(tmp_path):
    cfg = Config()
    cfg.state_file = str(tmp_path / "state.json")
    cfg.log_file = str(tmp_path / "agent.log")
    cfg.proactive.speak = True  # the fixture tests the policy, not the switch
    cfg.proactive.quiet_hours = ""
    return cfg


def snapshot(free=100 * GB, memory=80.0, top_memory=None):
    return {
        "cpu": {"percent": 20.0},
        "memory": {"percent": memory},
        "disks": {"C": {"percent": 89.0, "free_bytes": free, "total_bytes": 931 * GB}},
        "gpus": [],
        "processes": {"top_memory": top_memory if top_memory is not None else []},
        "active_window": {"title": "Visual Studio Code", "process": "Code.exe"},
    }


def falling_disk(tmp_path, gb_per_hour=-2.0, hours=72, start=100 * GB):
    """A history where drive C is losing space at a steady rate."""
    store = History(tmp_path / "history.json", fine=5)
    base = (time.time() // HOUR_SECONDS) * HOUR_SECONDS - hours * HOUR_SECONDS
    for i in range(hours):
        store.record(
            {"disks": {"C": {"free_bytes": start + gb_per_hour * GB * i, "percent": 50.0}}},
            now=base + i * HOUR_SECONDS + 30,
        )
    return store


class TestDiskObserver:
    def test_it_projects_the_day_the_disk_fills(self, config, tmp_path):
        store = falling_disk(tmp_path)
        # 72 hours at 2 GB/h from 100 GB leaves it already empty; start higher.
        store = falling_disk(tmp_path, gb_per_hour=-0.5, start=400 * GB)
        world = World(config, snapshot(free=364 * GB), store, [])
        notices = disk_filling(world)
        assert notices, "a disk losing 12 GB a day should be worth mentioning"
        assert "gigabytes a day" in notices[0].text
        assert notices[0].detail["days_to_zero"] < 40

    def test_a_steady_disk_is_never_mentioned(self, config, tmp_path):
        store = falling_disk(tmp_path, gb_per_hour=0.0, start=100 * GB)
        assert disk_filling(World(config, snapshot(), store, [])) == []

    def test_a_full_but_stable_disk_is_not_news(self, config, tmp_path):
        """91% for a year is how the machine lives, not something to announce."""
        store = falling_disk(tmp_path, gb_per_hour=0.0, start=5 * GB)
        world = World(config, snapshot(free=5 * GB), store, [])
        assert disk_filling(world) == []

    def test_a_distant_projection_is_left_alone(self, config, tmp_path):
        store = falling_disk(tmp_path, gb_per_hour=-0.01, start=800 * GB)
        config.proactive.disk_days_ahead = 21
        assert disk_filling(World(config, snapshot(free=799 * GB), store, [])) == []

    def test_urgency_rises_as_the_day_approaches(self, config, tmp_path):
        soon = falling_disk(tmp_path, gb_per_hour=-2.0, start=200 * GB)
        world = World(config, snapshot(free=56 * GB), soon, [])
        notices = disk_filling(world)
        assert notices and notices[0].priority >= 9


class TestMemoryObserver:
    def climbing(self, tmp_path, from_percent=50.0, to_percent=85.0, hours=12):
        store = History(tmp_path / "history.json", fine=5)
        base = (time.time() // HOUR_SECONDS) * HOUR_SECONDS - hours * HOUR_SECONDS
        step = (to_percent - from_percent) / max(1, hours - 1)
        for i in range(hours):
            store.record(
                {"memory": {"percent": from_percent + step * i}},
                now=base + i * HOUR_SECONDS + 30,
            )
        return store

    def test_it_names_the_process_holding_the_memory(self, config, tmp_path):
        store = self.climbing(tmp_path)
        world = World(
            config,
            snapshot(memory=85.0, top_memory=[{"name": "chrome.exe", "memory_bytes": 12 * GB}]),
            store,
            [],
        )
        notices = memory_creeping(world)
        assert notices and "chrome.exe" in notices[0].text

    def test_without_a_culprit_it_says_nothing(self, config, tmp_path):
        """"Memory is high" is something the user can already see."""
        world = World(config, snapshot(memory=85.0, top_memory=[]), self.climbing(tmp_path), [])
        assert memory_creeping(world) == []

    def test_a_small_process_is_not_the_culprit(self, config, tmp_path):
        world = World(
            config,
            snapshot(memory=85.0, top_memory=[{"name": "tiny.exe", "memory_bytes": 200_000_000}]),
            self.climbing(tmp_path),
            [],
        )
        assert memory_creeping(world) == []

    def test_flat_memory_is_not_creeping(self, config, tmp_path):
        store = self.climbing(tmp_path, from_percent=80.0, to_percent=80.0)
        world = World(
            config,
            snapshot(memory=80.0, top_memory=[{"name": "chrome.exe", "memory_bytes": 12 * GB}]),
            store,
            [],
        )
        assert memory_creeping(world) == []


class TestLogObserver:
    def write_log(self, config, lines):
        with open(config.log_file, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines))

    def stamp(self, minutes_ago=1):
        when = datetime.fromtimestamp(time.time() - minutes_ago * 60)
        return when.strftime("%Y-%m-%d %H:%M:%S")

    def test_errors_this_hour_are_counted(self, config, tmp_path):
        self.write_log(config, [
            f"{self.stamp(i)} ERROR arnold.jarvis - could not speak" for i in range(8)
        ])
        notices = log_errors(World(config, snapshot(), None, []))
        assert notices and "8 errors" in notices[0].text

    def test_yesterdays_errors_are_not_this_hour(self, config):
        self.write_log(config, [
            f"{self.stamp(minutes_ago=60 * 26)} ERROR something - old news" for _ in range(20)
        ])
        assert log_errors(World(config, snapshot(), None, [])) == []

    def test_a_few_errors_are_not_worth_interrupting_for(self, config):
        self.write_log(config, [f"{self.stamp()} ERROR x - y"])
        assert log_errors(World(config, snapshot(), None, [])) == []

    def test_a_missing_log_is_not_an_error(self, config):
        config.log_file = ""
        assert log_errors(World(config, snapshot(), None, [])) == []


class TestQuietHours:
    @pytest.mark.parametrize("hour,quiet", [(23, True), (2, True), (7, True), (9, False), (18, False)])
    def test_a_window_across_midnight(self, config, hour, quiet):
        config.proactive.quiet_hours = "22:30-08:00"
        when = datetime.now().replace(hour=hour, minute=0, second=0).timestamp()
        assert _in_quiet_hours(config, when) is quiet

    @pytest.mark.parametrize("hour,quiet", [(10, True), (13, False), (9, False)])
    def test_a_window_inside_one_day(self, config, hour, quiet):
        config.proactive.quiet_hours = "09:30-12:00"
        when = datetime.now().replace(hour=hour, minute=0, second=0).timestamp()
        assert _in_quiet_hours(config, when) is quiet

    def test_nonsense_is_ignored_rather_than_silencing_everything(self, config):
        config.proactive.quiet_hours = "all night"
        assert _in_quiet_hours(config, time.time()) is False

    def test_blank_means_never_quiet(self, config):
        config.proactive.quiet_hours = ""
        assert _in_quiet_hours(config, time.time()) is False


class TestNoticeBook:
    def test_it_remembers_across_a_restart(self, tmp_path):
        book = NoticeBook(tmp_path / "notices.json")
        book.record(Notice(key="disk", text="x", ts=time.time()), spoken=True)
        assert NoticeBook(tmp_path / "notices.json").said_at("disk") > 0

    def test_unspoken_notices_are_recorded_but_not_marked_said(self, tmp_path):
        book = NoticeBook(tmp_path / "notices.json")
        book.record(Notice(key="disk", text="x"), spoken=False)
        assert book.said_at("disk") == 0
        assert book.recent()[0]["spoken"] is False

    def test_a_corrupt_book_starts_empty(self, tmp_path):
        path = tmp_path / "notices.json"
        path.write_text("{{{", encoding="utf-8")
        assert NoticeBook(path).recent() == []


class TestPolicy:
    def engine(self, config, tmp_path, judge=None):
        store = falling_disk(tmp_path, gb_per_hour=-2.0, start=200 * GB)
        engine = ProactiveEngine(
            config, store, NoticeBook(tmp_path / "notices.json"), judge or RuleJudge()
        )
        world = World(config, snapshot(free=56 * GB), store, [])
        return engine, world

    def test_it_speaks_when_everything_lines_up(self, config, tmp_path):
        engine, world = self.engine(config, tmp_path)
        assert engine.run(world) is not None

    def test_the_same_thing_is_not_said_twice(self, config, tmp_path):
        engine, world = self.engine(config, tmp_path)
        assert engine.run(world) is not None
        assert engine.run(world) is None

    def test_it_says_nothing_during_quiet_hours(self, config, tmp_path):
        config.proactive.quiet_hours = "00:00-23:59"
        engine, world = self.engine(config, tmp_path)
        assert engine.run(world) is None

    def test_a_firing_alert_holds_the_floor(self, config, tmp_path):
        """Two voices about the same machine at once is one too many."""
        engine, world = self.engine(config, tmp_path)
        world.alerts = ["disk_c_low"]
        assert engine.run(world) is None

    def test_speak_off_records_but_stays_silent(self, config, tmp_path):
        config.proactive.speak = False
        engine, world = self.engine(config, tmp_path)
        assert engine.run(world) is None
        recorded = engine.book.recent()
        assert recorded and recorded[-1]["spoken"] is False
        assert "gigabytes" in recorded[-1]["text"], "it must record what it would have said"

    def test_the_daily_budget_is_enforced(self, config, tmp_path):
        config.proactive.max_per_day = 1
        engine, world = self.engine(config, tmp_path)
        engine.book.record(Notice(key="other", text="already said", ts=time.time()), spoken=True)
        assert engine.run(world) is None

    def test_it_does_not_speak_twice_in_quick_succession(self, config, tmp_path):
        config.proactive.min_gap_minutes = 60
        engine, world = self.engine(config, tmp_path)
        engine.book.record(Notice(key="other", text="just said", ts=time.time()), spoken=True)
        assert engine.run(world) is None

    def test_low_priority_notices_are_never_spoken(self, config, tmp_path):
        config.proactive.min_priority = 10
        engine, world = self.engine(config, tmp_path)
        assert engine.run(world) is None

    def test_an_observer_that_throws_does_not_stop_the_others(self, config, tmp_path, monkeypatch):
        from arnold import notices as module

        def broken(world):
            raise RuntimeError("boom")

        monkeypatch.setattr(module, "OBSERVERS", (broken, module.disk_filling))
        engine, world = self.engine(config, tmp_path)
        assert engine.run(world) is not None

    def test_the_pass_is_rate_limited(self, config, tmp_path):
        engine, _ = self.engine(config, tmp_path)
        assert engine.due() is True
        engine._last_run = time.time()
        assert engine.due() is False


class TestRuleJudge:
    def test_the_most_urgent_wins(self, config, tmp_path):
        world = World(config, snapshot(), None, [])
        chosen = RuleJudge().choose(
            [Notice(key="a", text="a", priority=4), Notice(key="b", text="b", priority=8)], world
        )
        assert chosen.key == "b"

    def test_nothing_clears_a_high_bar(self, config, tmp_path):
        config.proactive.min_priority = 9
        world = World(config, snapshot(), None, [])
        assert RuleJudge().choose([Notice(key="a", text="a", priority=4)], world) is None


class TestModelJudge:
    """It must never be the reason the assistant goes quiet or hangs."""

    def test_no_api_key_falls_back_to_the_rules(self, config, tmp_path, monkeypatch):
        from arnold.notices import ModelJudge

        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        world = World(config, snapshot(), None, [])
        world.book = NoticeBook(tmp_path / "n.json")
        chosen = ModelJudge().choose([Notice(key="a", text="a", priority=9)], world)
        assert chosen is not None

    def test_a_network_failure_falls_back_to_the_rules(self, config, tmp_path, monkeypatch):
        from arnold import notices as module

        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.setattr(
            module.urllib.request, "urlopen",
            lambda *a, **k: (_ for _ in ()).throw(OSError("no network")),
        )
        world = World(config, snapshot(), None, [])
        world.book = NoticeBook(tmp_path / "n.json")
        assert module.ModelJudge().choose([Notice(key="a", text="a", priority=9)], world)

    def test_a_null_answer_means_silence(self, config, tmp_path, monkeypatch):
        from arnold import notices as module

        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.setattr(
            module.ModelJudge, "choose",
            lambda self, candidates, world: None if candidates else None,
        )
        world = World(config, snapshot(), None, [])
        assert module.ModelJudge().choose([Notice(key="a", text="a", priority=9)], world) is None
