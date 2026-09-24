"""The observers that watch for a moment rather than a level.

A disk filling up is a level, and the existing observers cover those. These
are about something having just *changed*: a process that has closed, a drive
that has gone, an update that has been waiting a fortnight. That makes the
interesting cases the ones where nothing should be said at all - the very
first pass, the machine that was rebooted this morning, the USB stick that was
never really there.
"""

from __future__ import annotations

import time

import pytest

from arnold.config import Config
from arnold.history import HOUR_SECONDS, History
from arnold.notices import (
    OBSERVERS,
    NoticeBook,
    World,
    battery_low,
    drive_vanished,
    long_session,
    pending_reboot,
    pi_unreachable,
    watched_process_ended,
)

GB = 1024 ** 3


@pytest.fixture
def config(tmp_path):
    cfg = Config()
    cfg.state_file = str(tmp_path / "state.json")
    cfg.log_file = str(tmp_path / "agent.log")
    cfg.proactive.quiet_hours = ""
    return cfg


@pytest.fixture
def book(tmp_path):
    return NoticeBook(tmp_path / "notices.json")


def world_with(config, book, snap, history=None, links=None):
    world = World(config, snap, history, [], links=links)
    world.book = book
    return world


class TestTheMemo:
    """Edge detection needs somewhere to write down what was seen last time.
    The history keeps numbers and drops booleans, so this is that place."""

    def test_it_survives_a_reload(self, tmp_path):
        first = NoticeBook(tmp_path / "n.json")
        first.remember("drives_seen", ["C", "D"])
        assert NoticeBook(tmp_path / "n.json").recall("drives_seen") == ["C", "D"]

    def test_a_missing_key_gives_the_default(self, book):
        assert book.recall("nothing", "fallback") == "fallback"

    def test_it_is_capped_so_it_cannot_grow_forever(self, book):
        for i in range(NoticeBook.MAX_MEMO_KEYS + 20):
            book.remember(f"key{i}", i)
        assert len(book._memo) <= NoticeBook.MAX_MEMO_KEYS
        # The newest survive; the oldest make way.
        assert book.recall(f"key{NoticeBook.MAX_MEMO_KEYS + 19}") is not None


class TestPendingReboot:
    def probe(self, monkeypatch, waiting):
        import arnold.notices as notices

        monkeypatch.setattr(notices, "_reboot_is_pending", lambda: waiting)

    def test_nothing_without_a_marker(self, config, book, monkeypatch):
        self.probe(monkeypatch, False)
        world = world_with(config, book, {"uptime_seconds": 20 * 86400})
        assert pending_reboot(world) == []

    def test_a_marker_on_a_freshly_booted_machine_waits(self, config, book, monkeypatch):
        """They have just rebooted. Saying it now is nagging."""
        self.probe(monkeypatch, True)
        world = world_with(config, book, {"uptime_seconds": 3600})
        assert pending_reboot(world) == []

    def test_a_marker_and_a_long_uptime_is_worth_saying(self, config, book, monkeypatch):
        self.probe(monkeypatch, True)
        world = world_with(config, book, {"uptime_seconds": 11 * 86400})
        notices = pending_reboot(world)
        assert len(notices) == 1
        assert "restart" in notices[0].text
        assert notices[0].priority == 6

    def test_it_gets_urgent_after_three_weeks(self, config, book, monkeypatch):
        self.probe(monkeypatch, True)
        world = world_with(config, book, {"uptime_seconds": 22 * 86400})
        assert pending_reboot(world)[0].priority == 8

    def test_switching_it_off(self, config, book, monkeypatch):
        self.probe(monkeypatch, True)
        config.proactive.reboot_after_hours = 0.0
        world = world_with(config, book, {"uptime_seconds": 30 * 86400})
        assert pending_reboot(world) == []

    def test_the_registry_is_not_read_on_every_pass(self, config, book, monkeypatch):
        """It cannot change without a reboot, so it stays out of the tick."""
        import arnold.notices as notices

        calls = []
        monkeypatch.setattr(
            notices, "_reboot_is_pending", lambda: calls.append(1) or True
        )
        world = world_with(config, book, {"uptime_seconds": 11 * 86400})
        pending_reboot(world)
        pending_reboot(world)
        assert len(calls) == 1

    def test_the_real_probe_never_raises(self):
        """Whatever the registry does, an observer may not throw on the tick."""
        from arnold.notices import _reboot_is_pending

        assert isinstance(_reboot_is_pending(), bool)


class TestWatchedProcess:
    def snap(self, running):
        return {"processes": {"watched": {"steam.exe": {"running": running, "count": 1}}}}

    def test_nothing_on_the_first_pass(self, config, book):
        """Not having seen it before is not the same as it having stopped."""
        assert watched_process_ended(world_with(config, book, self.snap(False))) == []

    def test_nothing_while_it_is_still_running(self, config, book):
        watched_process_ended(world_with(config, book, self.snap(True)))
        assert watched_process_ended(world_with(config, book, self.snap(True))) == []

    def test_it_notices_the_transition_to_gone(self, config, book):
        watched_process_ended(world_with(config, book, self.snap(True)))
        notices = watched_process_ended(world_with(config, book, self.snap(False)))
        assert len(notices) == 1
        assert notices[0].text.startswith("Steam has closed")

    def test_it_does_not_say_it_twice(self, config, book):
        watched_process_ended(world_with(config, book, self.snap(True)))
        watched_process_ended(world_with(config, book, self.snap(False)))
        assert watched_process_ended(world_with(config, book, self.snap(False))) == []

    def test_it_says_how_long_it_ran(self, config, book):
        watched_process_ended(world_with(config, book, self.snap(True)))
        book.remember(
            "watched_running:steam.exe", {"running": True, "since": time.time() - 4 * 3600}
        )
        notices = watched_process_ended(world_with(config, book, self.snap(False)))
        assert "4 hours" in notices[0].text

    def test_a_short_run_does_not_get_a_duration(self, config, book):
        watched_process_ended(world_with(config, book, self.snap(True)))
        notices = watched_process_ended(world_with(config, book, self.snap(False)))
        assert notices[0].text == "Steam has closed."

    def test_switching_it_off(self, config, book):
        config.proactive.announce_watched_exit = False
        watched_process_ended(world_with(config, book, self.snap(True)))
        assert watched_process_ended(world_with(config, book, self.snap(False))) == []


class TestBattery:
    def snap(self, **battery):
        return {"battery": battery or None}

    def test_a_desktop_with_no_battery_is_silent(self, config, book):
        assert battery_low(world_with(config, book, self.snap())) == []

    def test_plugged_in_is_silent_even_at_five_percent(self, config, book):
        snap = self.snap(percent=5.0, plugged_in=True, seconds_left=None)
        assert battery_low(world_with(config, book, snap)) == []

    def test_low_and_unplugged_is_urgent(self, config, book):
        snap = self.snap(percent=14.0, plugged_in=False, seconds_left=20 * 60)
        notices = battery_low(world_with(config, book, snap))
        assert len(notices) == 1
        assert notices[0].priority == 8
        assert "14 percent" in notices[0].text

    def test_time_left_alone_can_trigger_it(self, config, book):
        """Ninety percent with ten minutes left is still ten minutes."""
        snap = self.snap(percent=90.0, plugged_in=False, seconds_left=10 * 60)
        assert battery_low(world_with(config, book, snap))

    def test_a_healthy_battery_is_silent(self, config, book):
        snap = self.snap(percent=80.0, plugged_in=False, seconds_left=4 * 3600)
        assert battery_low(world_with(config, book, snap)) == []

    def test_the_key_bands_so_it_can_speak_again_on_the_way_down(self, config, book):
        def key_at(percent):
            snap = self.snap(percent=percent, plugged_in=False, seconds_left=None)
            return battery_low(world_with(config, book, snap))[0].key

        assert key_at(18.0) != key_at(8.0)
        assert key_at(8.0) != key_at(4.0)


class TestPiUnreachable:
    def test_no_links_means_no_notice(self, config, book):
        """A World built the old way, with four arguments, stays inert."""
        assert pi_unreachable(world_with(config, book, {})) == []

    def test_a_brief_blip_says_nothing(self, config, book):
        links = {"pi_down_since": time.time() - 300}
        assert pi_unreachable(world_with(config, book, {}, links=links)) == []

    def test_two_hours_down_is_worth_saying(self, config, book):
        links = {"pi_down_since": time.time() - 3 * 3600}
        notices = pi_unreachable(world_with(config, book, {}, links=links))
        assert len(notices) == 1
        assert "these speakers" in notices[0].text


class TestDriveVanished:
    def disks(self, *letters):
        return {"disks": {letter: {"free_bytes": 100 * GB} for letter in letters}}

    def history_for(self, tmp_path, drive, hours=48):
        store = History(tmp_path / f"h-{drive}.json", fine=5)
        base = (time.time() // HOUR_SECONDS) * HOUR_SECONDS - hours * HOUR_SECONDS
        for i in range(hours):
            store.record(
                {"disks": {drive: {"free_bytes": 100 * GB, "percent": 50.0}}},
                now=base + i * HOUR_SECONDS,
            )
        return store

    def test_nothing_on_the_first_pass(self, config, book, tmp_path):
        world = world_with(
            config, book, self.disks("C", "D"), history=self.history_for(tmp_path, "D")
        )
        assert drive_vanished(world) == []

    def test_a_drive_that_was_there_and_is_gone_now(self, config, book, tmp_path):
        history = self.history_for(tmp_path, "D")
        drive_vanished(world_with(config, book, self.disks("C", "D"), history=history))
        notices = drive_vanished(world_with(config, book, self.disks("C"), history=history))
        assert len(notices) == 1
        assert "Drive D" in notices[0].text

    def test_a_usb_stick_that_was_never_really_there_is_not_news(self, config, book, tmp_path):
        """Too little history to be a fixed drive, so losing it is not a fault."""
        history = self.history_for(tmp_path, "E", hours=2)
        drive_vanished(world_with(config, book, self.disks("C", "E"), history=history))
        assert drive_vanished(world_with(config, book, self.disks("C"), history=history)) == []

    def test_an_empty_snapshot_is_not_every_drive_vanishing(self, config, book, tmp_path):
        """A collector that gathered nothing is a collector problem, and
        announcing that every drive has gone would be the wrong answer."""
        history = self.history_for(tmp_path, "D")
        drive_vanished(world_with(config, book, self.disks("C", "D"), history=history))
        assert drive_vanished(world_with(config, book, {"disks": {}}, history=history)) == []

    def test_switching_it_off(self, config, book, tmp_path):
        config.proactive.announce_missing_drives = False
        history = self.history_for(tmp_path, "D")
        drive_vanished(world_with(config, book, self.disks("C", "D"), history=history))
        assert drive_vanished(world_with(config, book, self.disks("C"), history=history)) == []


class TestLongSession:
    def idle(self, monkeypatch, seconds):
        import arnold.platform_win.window as window

        monkeypatch.setattr(window, "idle_seconds", lambda: seconds)

    def test_off_by_default(self, config, book, monkeypatch):
        self.idle(monkeypatch, 5.0)
        assert long_session(world_with(config, book, {})) == []

    def test_four_hours_without_a_break(self, config, book, monkeypatch):
        config.proactive.break_after_hours = 4.0
        self.idle(monkeypatch, 5.0)
        world = world_with(config, book, {})
        long_session(world)  # starts the clock
        book.remember("session_started", time.time() - 5 * 3600)
        notices = long_session(world)
        assert len(notices) == 1
        assert "without a break" in notices[0].text

    def test_it_sits_below_min_priority_so_it_is_only_recorded(self, config, book, monkeypatch):
        """Two switches for the one feature that can nag: switching it on
        records it, and being told as well needs min_priority lowered too."""
        config.proactive.break_after_hours = 4.0
        self.idle(monkeypatch, 5.0)
        world = world_with(config, book, {})
        long_session(world)
        book.remember("session_started", time.time() - 5 * 3600)
        assert long_session(world)[0].priority < config.proactive.min_priority

    def test_a_real_break_resets_it(self, config, book, monkeypatch):
        config.proactive.break_after_hours = 4.0
        book.remember("session_started", time.time() - 5 * 3600)
        self.idle(monkeypatch, 900.0)  # fifteen minutes away from the desk
        assert long_session(world_with(config, book, {})) == []

    def test_no_idle_clock_means_nothing_to_say(self, config, book, monkeypatch):
        self.idle(monkeypatch, None)
        config.proactive.break_after_hours = 4.0
        assert long_session(world_with(config, book, {})) == []


@pytest.mark.parametrize("observer", OBSERVERS, ids=lambda o: o.__name__)
def test_every_observer_survives_an_empty_snapshot(observer, config, book, tmp_path):
    """The one that keeps the agent's tick alive.

    An observer runs against whatever the collector managed to gather, which
    on a bad day is very little indeed.
    """
    world = World(config, {}, History(tmp_path / "h.json", fine=5), [], links={})
    world.book = book
    assert isinstance(observer(world), list)


@pytest.mark.parametrize("observer", OBSERVERS, ids=lambda o: o.__name__)
def test_every_observer_survives_having_no_book(observer, config, tmp_path):
    """`world.book` is set by the engine before observing, but a caller
    outside the engine may not have done it."""
    world = World(config, {}, History(tmp_path / "h.json", fine=5), [], links={})
    assert isinstance(observer(world), list)
