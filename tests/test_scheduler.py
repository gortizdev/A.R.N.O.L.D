"""Reminders and standing jobs.

A reminder that fires at the wrong time is worse than one that was refused, so
the parser's job is to be *certain* or to say it did not understand. These
tests pin a fixed "now" and check the exact moment chosen, because "roughly the
right time" is not a thing a reminder can be.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta

import pytest

from arnold.scheduler import (
    LATE_TOLERANCE_SECONDS,
    Job,
    Schedule,
    ScheduleError,
    parse_when,
)

# A Wednesday at 14:30, so "at 4" and "every monday" have obvious answers.
NOW = datetime(2026, 8, 12, 14, 30, 0).timestamp()


def at(text):
    when, repeat = parse_when(text, now=NOW)
    return datetime.fromtimestamp(when), repeat


class TestRelativeTimes:
    @pytest.mark.parametrize(
        "said,minutes",
        [
            ("in 20 minutes", 20),
            ("in twenty minutes", 20),
            ("in 2 hours", 120),
            ("in an hour", 60),
            ("in a minute", 1),
            ("in three days", 3 * 24 * 60),
        ],
    )
    def test_offsets(self, said, minutes):
        when, repeat = at(said)
        assert when == datetime.fromtimestamp(NOW) + timedelta(minutes=minutes)
        assert repeat == ""

    def test_half_an_hour_is_thirty_minutes_not_thirty_hours(self):
        when, _ = at("in half an hour")
        assert when == datetime.fromtimestamp(NOW) + timedelta(minutes=30)

    def test_something_too_soon_is_refused(self):
        with pytest.raises(ScheduleError, match="too soon"):
            parse_when("in 1 second", now=NOW)


class TestClockTimes:
    def test_this_afternoon(self):
        when, _ = at("at 16:00")
        assert (when.hour, when.minute, when.day) == (16, 0, 12)

    def test_a_bare_small_hour_means_this_afternoon(self):
        """Nobody sets a reminder for four in the morning without saying am."""
        when, _ = at("at 4")
        assert (when.hour, when.day) == (16, 12)

    def test_am_is_respected(self):
        when, _ = at("at 4am")
        assert (when.hour, when.day) == (4, 13), "4am has already passed today"

    def test_a_time_already_gone_lands_tomorrow(self):
        when, _ = at("at 9:00")
        assert (when.hour, when.day) == (9, 13)

    def test_pm_on_a_twelve(self):
        when, _ = at("at 12pm")
        assert (when.hour, when.day) == (12, 13)

    def test_tomorrow_is_honoured(self):
        when, _ = at("at 16:00 tomorrow")
        assert (when.hour, when.day) == (16, 13)

    def test_nonsense_clock_times_are_refused(self):
        with pytest.raises(ScheduleError):
            parse_when("at 99:99", now=NOW)


class TestRepeats:
    def test_every_day_at_a_time(self):
        when, repeat = at("every day at 9")
        assert repeat == "daily"
        assert (when.hour, when.day) == (9, 13)

    def test_every_morning_picks_the_morning(self):
        when, repeat = at("every morning")
        assert repeat == "daily" and when.hour == 8

    def test_every_hour(self):
        when, repeat = at("every hour")
        assert repeat == "hourly"
        assert when == datetime.fromtimestamp(NOW) + timedelta(hours=1)

    def test_a_named_day_lands_on_that_day(self):
        when, repeat = at("every monday at 10")
        assert repeat == "weekly:0"
        assert when.weekday() == 0 and when.hour == 10

    def test_a_named_day_later_this_week(self):
        when, repeat = at("every friday at 10")
        assert repeat == "weekly:4"
        assert when.weekday() == 4 and when.day == 14


class TestRefusals:
    @pytest.mark.parametrize("said", ["", "sometime", "when the render finishes", "later"])
    def test_it_says_so_rather_than_guessing(self, said):
        with pytest.raises(ScheduleError):
            parse_when(said, now=NOW)


class TestSchedule:
    @pytest.fixture
    def schedule(self, tmp_path):
        return Schedule(tmp_path / "schedule.json")

    def test_a_job_is_kept_and_reloaded(self, tmp_path):
        first = Schedule(tmp_path / "schedule.json")
        first.add("check the render", "in 20 minutes", said="check the render")
        again = Schedule(tmp_path / "schedule.json")
        assert [job.what for job in again.jobs()] == ["check the render"]

    def test_nothing_is_due_before_its_time(self, schedule):
        schedule.add("later", "in 20 minutes")
        assert schedule.due() == []

    def test_a_due_job_fires_once(self, schedule):
        schedule.add("now", "in 10 seconds")
        assert [job.what for job in schedule.due(now=time.time() + 11)] == ["now"]
        assert schedule.due(now=time.time() + 12) == []

    def test_a_one_shot_job_is_removed_after_firing(self, schedule):
        schedule.add("once", "in 10 seconds")
        schedule.due(now=time.time() + 11)
        assert schedule.jobs() == []

    def test_a_repeating_job_is_rescheduled(self, schedule):
        schedule.add("stand up", "every hour", now=NOW)
        fired = schedule.due(now=NOW + 3601)
        assert len(fired) == 1
        remaining = schedule.jobs()
        assert len(remaining) == 1
        assert remaining[0].when > NOW + 3601

    def test_a_repeat_catches_up_rather_than_firing_in_a_burst(self, schedule):
        """Back from a week away, an hourly job must not fire 168 times."""
        schedule.add("stand up", "every hour", now=NOW)
        fired = schedule.due(now=NOW + 86400 * 7)
        assert len(fired) <= 1
        assert schedule.jobs()[0].when > NOW + 86400 * 7

    def test_a_slightly_late_job_still_fires(self, schedule):
        """The PC was rebooting. A reminder two minutes late is still useful."""
        schedule.add("now", "in 10 seconds")
        assert schedule.due(now=time.time() + 130) != []

    def test_a_very_late_job_is_dropped_silently(self, schedule):
        schedule.add("yesterday", "in 10 seconds")
        assert schedule.due(now=time.time() + LATE_TOLERANCE_SECONDS + 60) == []
        assert schedule.jobs() == []

    def test_cancelling_by_words(self, schedule):
        schedule.add("check the render", "in 20 minutes", said="check the render")
        schedule.add("call mum", "in 30 minutes", said="call mum")
        assert len(schedule.cancel("render")) == 1
        assert [job.said for job in schedule.jobs()] == ["call mum"]

    def test_cancelling_by_id(self, schedule):
        job = schedule.add("x", "in 20 minutes")
        assert schedule.cancel(job.id) != []
        assert schedule.jobs() == []

    def test_cancelling_everything(self, schedule):
        schedule.add("a", "in 20 minutes")
        schedule.add("b", "in 30 minutes")
        assert len(schedule.cancel("all")) == 2

    def test_cancelling_something_absent_returns_nothing(self, schedule):
        assert schedule.cancel("nothing like this") == []

    def test_the_job_count_is_capped(self, tmp_path):
        schedule = Schedule(tmp_path / "schedule.json", max_jobs=2)
        schedule.add("a", "in 20 minutes")
        schedule.add("b", "in 30 minutes")
        with pytest.raises(ScheduleError):
            schedule.add("c", "in 40 minutes")

    def test_a_corrupt_file_starts_empty(self, tmp_path):
        path = tmp_path / "schedule.json"
        path.write_text("]not json[", encoding="utf-8")
        assert Schedule(path).jobs() == []

    def test_a_broken_job_is_skipped_not_fatal(self, tmp_path):
        path = tmp_path / "schedule.json"
        path.write_text('{"jobs": [{"id": "x"}, 42]}', encoding="utf-8")
        assert Schedule(path).jobs() == []


class TestDescribe:
    def test_a_reminder_reads_back_in_the_users_words(self):
        job = Job(id="a", when=NOW + 1200, what="check the render",
                  said="check the render", created=NOW)
        assert job.describe(now=NOW) == "check the render, in 20 minutes"

    def test_a_daily_job_says_so(self):
        job = Job(id="a", when=NOW + 3600, what="stand up", said="stand up",
                  repeat="daily", created=NOW)
        assert "every day at" in job.describe(now=NOW)

    def test_a_command_job_reads_as_an_action(self):
        job = Job(id="a", when=NOW + 60, what="control.lock", kind="command", created=NOW)
        assert job.describe(now=NOW).startswith("run control.lock")


class TestCommands:
    """The registry surface: what the voice session actually calls."""

    @pytest.fixture
    def ctx(self, tmp_path):
        from arnold.alerts import AlertEngine
        from arnold.commands import CommandContext
        from arnold.config import Config
        from arnold.monitors.collector import Collector

        config = Config()
        config.state_file = str(tmp_path / "state.json")
        return CommandContext(
            config=config,
            collector=Collector(config.monitors),
            alerts=AlertEngine(config.alerts, "the desktop"),
        )

    def run(self, ctx, name, args=None):
        from arnold.commands import build_registry

        return build_registry().dispatch(name, args or {}, ctx)

    def test_adding_a_reminder_says_when(self, ctx):
        result = self.run(ctx, "schedule.add", {"text": "check the render", "when": "in 20 minutes"})
        assert result.ok
        assert "20 minutes" in result.speech

    def test_a_time_it_cannot_parse_is_explained(self, ctx):
        result = self.run(ctx, "schedule.add", {"text": "x", "when": "sometime soon"})
        assert not result.ok
        assert "in twenty minutes" in result.error

    def test_listing_when_empty(self, ctx):
        assert "nothing scheduled" in self.run(ctx, "schedule.list").speech.lower()

    def test_listing_reads_them_back(self, ctx):
        self.run(ctx, "schedule.add", {"text": "call mum", "when": "at 6pm"})
        assert "call mum" in self.run(ctx, "schedule.list").speech

    def test_cancelling(self, ctx):
        self.run(ctx, "schedule.add", {"text": "call mum", "when": "at 6pm"})
        assert self.run(ctx, "schedule.cancel", {"which": "mum"}).ok
        assert "nothing scheduled" in self.run(ctx, "schedule.list").speech.lower()

    def test_a_command_off_the_allowlist_is_refused(self, ctx):
        result = self.run(
            ctx, "schedule.add",
            {"text": "shut down", "when": "at 11pm", "command": "control.shutdown"},
        )
        assert not result.ok
        assert "not allowed" in result.error

    def test_an_allowlisted_command_is_accepted(self, ctx):
        ctx.config.schedule.allow_commands = ["control.lock"]
        result = self.run(
            ctx, "schedule.add",
            {"text": "lock up", "when": "at 11pm", "command": "control.lock"},
        )
        assert result.ok
        assert result.result["job"]["kind"] == "command"

    def test_scheduling_switched_off_is_said_plainly(self, ctx):
        ctx.config.schedule.enabled = False
        result = self.run(ctx, "schedule.list")
        assert not result.ok and "switched off" in result.error


class TestSpokenReminders:
    """The local brain, for pipeline mode. Realtime calls the tool directly."""

    @pytest.fixture
    def brain(self, tmp_path):
        from arnold.alerts import AlertEngine
        from arnold.commands import CommandContext
        from arnold.config import Config
        from arnold.monitors.collector import Collector
        from arnold.voice.brain import LocalBrain

        config = Config()
        config.state_file = str(tmp_path / "state.json")
        return LocalBrain(
            CommandContext(
                config=config,
                collector=Collector(config.monitors),
                alerts=AlertEngine(config.alerts, "the desktop"),
            )
        )

    @pytest.mark.parametrize(
        "said,what,when",
        [
            ("remind me to check the render in 20 minutes", "check the render", "in 20 minutes"),
            ("remind me to call mum at 6pm", "call mum", "at 6pm"),
            ("remind me to stand up every hour", "stand up", "every hour"),
            ("in 10 minutes remind me to take the pizza out",
             "take the pizza out", "in 10 minutes"),
        ],
    )
    def test_it_splits_what_from_when(self, brain, said, what, when):
        matched = brain.match(said)
        assert matched is not None
        assert matched[0] == "schedule.add"
        assert matched[1]["text"] == what
        assert matched[1]["when"] == when

    def test_a_fact_is_still_a_fact_not_a_reminder(self, brain):
        """"remember that X" is memory; "remind me to X at Y" is the schedule."""
        matched = brain.match("remember that my sister's birthday is in march")
        assert matched is None or matched[0] != "schedule.add"

    def test_asking_what_is_scheduled(self, brain):
        matched = brain.match("what have i got scheduled")
        assert matched is not None and matched[0] == "schedule.list"
