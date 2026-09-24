"""Timers and alarms that ring on this PC.

The failure this exists to prevent is a timer that is silently swallowed, and
the commonest cause used to be the Pi being switched off. So: a timer belongs
to this machine, it rings here, and cancelling one can never take a reminder
with it.
"""

from __future__ import annotations

import time
from datetime import datetime

import pytest

from arnold.alerts import AlertEngine
from arnold.commands import CommandContext, build_registry
from arnold.config import Config
from arnold.monitors.collector import Collector
from arnold.scheduler import (
    LATE_TOLERANCE_SECONDS,
    Schedule,
    ScheduleError,
    parse_duration,
    path_for,
)

NOW = datetime(2026, 8, 12, 14, 30, 0).timestamp()


@pytest.fixture
def config(tmp_path):
    cfg = Config()
    cfg.state_file = str(tmp_path / "state.json")
    return cfg


@pytest.fixture
def ctx(config):
    return CommandContext(
        config=config,
        collector=Collector(config.monitors),
        alerts=AlertEngine([], "the desktop"),
    )


@pytest.fixture
def registry():
    return build_registry()


def run(registry, ctx, command, **args):
    # `command`, not `name`: a timer's own argument is called name.
    return registry.dispatch(command, args, ctx)


class TestParseDuration:
    @pytest.mark.parametrize(
        "said,seconds",
        [
            ("twenty minutes", 1200),
            ("20 minutes", 1200),
            ("20m", 1200),
            ("90 seconds", 90),
            ("an hour", 3600),
            ("an hour and a half", 5400),
            ("two and a half hours", 9000),
            ("half an hour", 1800),
            ("1:30", 5400),
            ("1:30:00", 5400),
            ("1 hour 30 minutes", 5400),
            ("five mins", 300),
        ],
    )
    def test_spoken_lengths(self, said, seconds):
        assert parse_duration(said) == pytest.approx(seconds)

    def test_a_bare_number_is_refused_by_naming_the_choice(self):
        """'Set a timer for twenty' is twenty of something. Guessing burns
        the dinner."""
        with pytest.raises(ScheduleError) as exc:
            parse_duration("20")
        assert "minutes or seconds" in str(exc.value)

    @pytest.mark.parametrize("said", ["", "   ", "banana", "twenty"])
    def test_nonsense_is_refused(self, said):
        with pytest.raises(ScheduleError):
            parse_duration(said)


class TestSetting:
    def test_a_timer_is_a_job_with_a_length(self, registry, ctx, config):
        result = run(registry, ctx, "timer.set", minutes=20, name="pasta")
        assert result.ok
        job = result.result["timer"]
        assert job["kind"] == "timer"
        assert job["total_seconds"] == 1200
        assert job["name"] == "pasta"
        assert "20 minutes" in result.speech and "pasta" in result.speech

    def test_a_spoken_length_works_too(self, registry, ctx):
        job = run(registry, ctx, "timer.set", **{"for": "an hour and a half"}).result["timer"]
        assert job["total_seconds"] == pytest.approx(5400)

    def test_hours_and_minutes_add_up(self, registry, ctx):
        job = run(registry, ctx, "timer.set", hours=1, minutes=30).result["timer"]
        assert job["total_seconds"] == pytest.approx(5400)

    def test_a_clock_time_is_an_alarm_not_a_countdown(self, registry, ctx):
        """An alarm reads back as a time, because the time is what was chosen."""
        job = run(registry, ctx, "timer.set", when="at 7", name="wake up").result["timer"]
        assert job["kind"] == "timer"
        assert job["total_seconds"] == 0

    def test_longer_than_a_day_is_refused_with_a_suggestion(self, registry, ctx):
        result = run(registry, ctx, "timer.set", hours=30)
        assert not result.ok
        assert "reminder" in result.error

    def test_no_length_at_all_is_refused(self, registry, ctx):
        assert not run(registry, ctx, "timer.set", name="pasta").ok

    def test_a_number_it_cannot_read_is_refused(self, registry, ctx):
        assert not run(registry, ctx, "timer.set", minutes="soon").ok

    def test_timers_off_in_config_is_refused_with_a_sentence(self, registry, ctx, config):
        config.schedule.timers = False
        result = run(registry, ctx, "timer.set", minutes=5)
        assert not result.ok
        assert "switched off" in result.error


class TestDescribing:
    def make(self, config, **kw):
        schedule = Schedule(path_for(config))
        return schedule.add("up", "", kind="timer", now=NOW, **kw)

    def test_a_named_timer_reads_back_with_what_is_left(self, config):
        job = self.make(config, seconds=1200, name="pasta")
        assert job.describe(now=NOW + 660) == "the pasta timer, 9 minutes left"

    def test_an_unnamed_timer_says_its_length(self, config):
        job = self.make(config, seconds=1200)
        assert job.describe(now=NOW) == "a 20 minutes timer, 20 minutes left"

    def test_under_a_minute(self, config):
        job = self.make(config, seconds=1200, name="pasta")
        assert "less than a minute left" in job.describe(now=NOW + 1180)

    def test_an_alarm_reads_as_a_time(self, config):
        """No total_seconds means it was set at a clock time, and the time is
        the thing the person chose, so that is what is read back."""
        schedule = Schedule(path_for(config))
        job = schedule.add("up", "at 7", kind="timer", name="wake up", now=NOW)
        assert job.total_seconds == 0
        assert "07:00" in job.describe(now=NOW)
        assert "wake up" in job.describe(now=NOW)

    def test_remaining_never_goes_negative(self, config):
        job = self.make(config, seconds=60)
        assert job.remaining(now=NOW + 999) == 0.0


class TestListing:
    def test_timer_list_ignores_reminders(self, registry, ctx):
        run(registry, ctx, "timer.set", minutes=20, name="pasta")
        run(registry, ctx, "schedule.add", text="check the render", when="in 40 minutes")
        listed = run(registry, ctx, "timer.list")
        assert len(listed.result["timers"]) == 1
        assert "pasta" in listed.speech

    def test_an_empty_list_says_so(self, registry, ctx):
        assert run(registry, ctx, "timer.list").speech == "Nothing running."

    def test_schedule_list_counts_both_apart(self, registry, ctx):
        run(registry, ctx, "timer.set", minutes=20, name="pasta")
        run(registry, ctx, "schedule.add", text="check the render", when="in 40 minutes")
        listed = run(registry, ctx, "schedule.list")
        assert listed.result["timers"] == 1
        assert listed.result["reminders"] == 1
        assert "1 timer" in listed.speech and "1 reminder" in listed.speech


class TestCancelling:
    def test_a_timer_can_be_cancelled_by_name(self, registry, ctx):
        run(registry, ctx, "timer.set", minutes=20, name="pasta")
        result = run(registry, ctx, "timer.cancel", which="pasta")
        assert result.ok
        assert "pasta" in result.speech
        assert run(registry, ctx, "timer.list").result["timers"] == []

    def test_cancelling_a_timer_cannot_drop_a_reminder(self, registry, ctx):
        """The whole reason timer.cancel is its own command."""
        run(registry, ctx, "schedule.add", text="take the pasta out of the freezer",
            when="in 40 minutes")
        result = run(registry, ctx, "timer.cancel", which="pasta")
        assert not result.ok
        assert len(run(registry, ctx, "schedule.list").result["jobs"]) == 1

    def test_cancel_all_only_stops_timers(self, registry, ctx):
        run(registry, ctx, "timer.set", minutes=20, name="pasta")
        run(registry, ctx, "schedule.add", text="check the render", when="in 40 minutes")
        run(registry, ctx, "timer.cancel", which="all")
        remaining = run(registry, ctx, "schedule.list").result
        assert remaining["timers"] == 0
        assert remaining["reminders"] == 1

    def test_cancelling_nothing_says_so(self, registry, ctx):
        assert not run(registry, ctx, "timer.cancel", which="pasta").ok


class FakeVoice:
    """Records what was said and whether it rang."""

    def __init__(self) -> None:
        self.said: list[tuple[str, bool]] = []
        self.enabled = True
        self.pi_down_since = 0.0

    def say(self, text: str, *, ring: bool = False) -> bool:
        self.said.append((text, ring))
        return True


class TestFiring:
    def fire(self, config, monkeypatch, jobs_setup):
        """Run one agent tick's worth of due-job handling."""
        from arnold.service import AssistantService

        schedule = Schedule(path_for(config))
        jobs_setup(schedule)

        service = AssistantService.__new__(AssistantService)
        service.config = config
        service.schedule = Schedule(path_for(config))
        service.speech = FakeVoice()
        service.transport = None
        service.registry = build_registry()
        monkeypatch.setattr(service, "_toast_timer", lambda job: None)
        service._run_due_jobs()
        return service.speech.said

    def test_a_due_timer_rings_and_speaks(self, config, monkeypatch):
        said = self.fire(
            config, monkeypatch,
            lambda s: s.add("The pasta timer is up.", "", kind="timer",
                            seconds=-5, name="pasta"),
        )
        assert said == [("The pasta timer is up.", True)]

    def test_a_due_reminder_does_not_ring(self, config, monkeypatch):
        said = self.fire(
            config, monkeypatch,
            # Set five minutes ago, for five minutes' time: due now.
            lambda s: s.add("check the render", "in 5 minutes",
                            now=time.time() - 300),
        )
        assert said == [("check the render", False)]

    def test_the_ring_can_be_switched_off(self, config, monkeypatch):
        config.schedule.ring = False
        said = self.fire(
            config, monkeypatch,
            lambda s: s.add("up", "", kind="timer", seconds=-5),
        )
        assert said == [("up", False)]

    def test_a_timer_that_fired_while_the_pc_slept_is_dropped(self, config, monkeypatch):
        said = self.fire(
            config, monkeypatch,
            lambda s: s.add("up", "", kind="timer",
                            seconds=-(LATE_TOLERANCE_SECONDS + 60)),
        )
        assert said == []


class TestReachable:
    def test_the_timer_commands_are_in_the_registry(self, registry):
        for name in ("timer.set", "timer.list", "timer.cancel"):
            assert registry.get(name) is not None

    def test_they_are_offered_to_the_voice_session(self):
        from arnold.voice.tools import PC_COMMANDS

        assert {"timer.set", "timer.list", "timer.cancel"} <= set(PC_COMMANDS)

    def test_they_do_not_need_the_desktop(self, registry):
        """So Jarvis can set one over SSH without a round trip to the agent."""
        for name in ("timer.set", "timer.list", "timer.cancel"):
            assert not registry.get(name).needs_desktop


class TestPersona:
    def test_the_persona_keeps_timers_here(self):
        from arnold.voice.session_config import persona_for

        text = persona_for(Config())
        assert "timer.set" in text
        # The old wording sent them to the Pi, which failed whenever it was off.
        assert "only {jarvis} can do - timers" not in text

    def test_ask_jarvis_no_longer_claims_timers(self):
        from arnold.voice.tools import build_tools

        cfg = Config()
        cfg.jarvis.home_assistant.token = "tok"
        ask = next(t for t in build_tools(cfg) if t["name"] == "ask_jarvis")
        assert "those are yours" in ask["description"]

    def test_mirror_mode_is_told_which_tools_are_really_here(self):
        from arnold.voice.session_config import MIRROR_ADDENDUM

        assert "timer.set" in MIRROR_ADDENDUM
        assert "not the Raspberry Pi" in MIRROR_ADDENDUM


class TestPickingUpOtherProcesses:
    """The agent's tick is the clock, but almost nothing is set in the agent.

    A timer comes from the voice session and a reminder from an SSH `exec`,
    both separate processes that write the schedule file. An agent that only
    ever fires what it loaded at boot looks exactly like a broken timer.
    """

    def test_a_job_added_by_another_process_is_picked_up(self, config):
        agent_side = Schedule(path_for(config))
        assert agent_side.jobs() == []

        other_process = Schedule(path_for(config))
        other_process.add("up", "", kind="timer", seconds=-5, name="pasta")

        due = agent_side.due()
        assert [job.name for job in due] == ["pasta"]

    def test_a_cancellation_elsewhere_is_picked_up(self, config):
        other_process = Schedule(path_for(config))
        other_process.add("up", "", kind="timer", seconds=-5, name="pasta")

        agent_side = Schedule(path_for(config))
        other_process.cancel("pasta", kinds=("timer",))
        assert agent_side.due() == []

    def test_our_own_writes_do_not_count_as_a_change(self, config):
        schedule = Schedule(path_for(config))
        schedule.add("up", "", kind="timer", seconds=3600, name="pasta")
        assert schedule.reload_if_changed() is False

    def test_reloading_a_missing_file_keeps_what_we_have(self, config):
        schedule = Schedule(path_for(config))
        schedule.add("up", "", kind="timer", seconds=3600, name="pasta")
        path_for(config).unlink()
        assert schedule.reload_if_changed() is False
        assert len(schedule.jobs()) == 1
