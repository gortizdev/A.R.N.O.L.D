import pytest

from arnold.alerts import AlertEngine, Rule, RuleError


def snapshot(cpu=10.0, free=500_000_000_000, steam=True):
    return {
        "cpu": {"percent": cpu},
        "disks": {"C": {"percent": 50.0, "free_bytes": free}},
        "processes": {"watched": {"steam.exe": {"running": steam, "count": 1, "pids": [1]}}},
    }


CPU_RULE = {
    "name": "cpu_high",
    "metric": "cpu.percent",
    "op": ">",
    "threshold": 90,
    "clear_threshold": 70,
    "for_seconds": 60,
    "cooldown_seconds": 600,
}


class TestRuleValidation:
    def test_rule_needs_a_name(self):
        with pytest.raises(RuleError, match="needs a 'name'"):
            Rule.from_dict({"metric": "cpu.percent", "threshold": 90})

    def test_rule_needs_metric_or_process(self):
        with pytest.raises(RuleError, match="either 'metric' or 'process'"):
            Rule.from_dict({"name": "x"})

    def test_rule_cannot_set_both(self):
        with pytest.raises(RuleError, match="pick one"):
            Rule.from_dict({"name": "x", "metric": "cpu.percent", "process": "a.exe"})

    def test_unknown_operator_rejected(self):
        with pytest.raises(RuleError, match="has op"):
            Rule.from_dict({"name": "x", "metric": "cpu.percent", "op": "=>", "threshold": 1})

    def test_unknown_key_rejected(self):
        with pytest.raises(RuleError, match="unknown key"):
            Rule.from_dict({"name": "x", "metric": "cpu.percent", "threshold": 1, "typo": 1})

    def test_bad_severity_rejected(self):
        with pytest.raises(RuleError, match="severity"):
            Rule.from_dict(
                {"name": "x", "metric": "cpu.percent", "threshold": 1, "severity": "urgent"}
            )

    def test_duplicate_names_rejected(self):
        with pytest.raises(RuleError, match="duplicate"):
            AlertEngine([CPU_RULE, dict(CPU_RULE)])


class TestFiring:
    def test_below_threshold_stays_quiet(self):
        engine = AlertEngine([CPU_RULE])
        assert engine.evaluate(snapshot(cpu=10), now=1000) == []

    def test_does_not_fire_before_duration_elapses(self):
        engine = AlertEngine([CPU_RULE])
        assert engine.evaluate(snapshot(cpu=95), now=1000) == []
        assert engine.evaluate(snapshot(cpu=95), now=1030) == []

    def test_fires_once_duration_elapses(self):
        engine = AlertEngine([CPU_RULE])
        engine.evaluate(snapshot(cpu=95), now=1000)
        events = engine.evaluate(snapshot(cpu=95), now=1061)
        assert len(events) == 1
        assert events[0].rule == "cpu_high"
        assert events[0].state == "firing"

    def test_brief_spike_resets_the_timer(self):
        """The point of for_seconds: a load spike that passes must stay quiet."""
        engine = AlertEngine([CPU_RULE])
        engine.evaluate(snapshot(cpu=95), now=1000)
        engine.evaluate(snapshot(cpu=20), now=1030)  # dropped back
        engine.evaluate(snapshot(cpu=95), now=1040)  # climbed again
        assert engine.evaluate(snapshot(cpu=95), now=1075) == []

    def test_does_not_refire_within_cooldown(self):
        engine = AlertEngine([CPU_RULE])
        engine.evaluate(snapshot(cpu=95), now=1000)
        assert len(engine.evaluate(snapshot(cpu=95), now=1061)) == 1
        assert engine.evaluate(snapshot(cpu=95), now=1200) == []


class TestHysteresis:
    def test_stays_firing_between_clear_and_fire_thresholds(self):
        engine = AlertEngine([CPU_RULE])
        engine.evaluate(snapshot(cpu=95), now=1000)
        engine.evaluate(snapshot(cpu=95), now=1061)
        # 80 is under the 90 fire point but above the 70 clear point.
        assert engine.evaluate(snapshot(cpu=80), now=1100) == []
        assert engine.active() == ["cpu_high"]

    def test_clears_below_clear_threshold(self):
        engine = AlertEngine([CPU_RULE])
        engine.evaluate(snapshot(cpu=95), now=1000)
        engine.evaluate(snapshot(cpu=95), now=1061)
        events = engine.evaluate(snapshot(cpu=50), now=1100)
        assert len(events) == 1
        assert events[0].state == "cleared"
        assert engine.active() == []

    def test_hovering_does_not_chatter(self):
        engine = AlertEngine([CPU_RULE])
        engine.evaluate(snapshot(cpu=95), now=1000)
        assert len(engine.evaluate(snapshot(cpu=95), now=1061)) == 1
        fired = 0
        for i, value in enumerate([88, 92, 89, 91, 87, 93]):
            fired += len(engine.evaluate(snapshot(cpu=value), now=1100 + i * 10))
        assert fired == 0


class TestProcessRules:
    RULE = {
        "name": "steam_down",
        "process": "steam.exe",
        "expect": "running",
        "for_seconds": 0,
        "cooldown_seconds": 0,
    }

    def test_quiet_while_running(self):
        assert AlertEngine([self.RULE]).evaluate(snapshot(steam=True), now=1000) == []

    def test_fires_when_process_stops(self):
        engine = AlertEngine([self.RULE])
        engine.evaluate(snapshot(steam=True), now=1000)
        events = engine.evaluate(snapshot(steam=False), now=1010)
        assert len(events) == 1
        assert "steam.exe is not running" in events[0].message

    def test_clears_when_process_returns(self):
        engine = AlertEngine([self.RULE])
        engine.evaluate(snapshot(steam=False), now=1000)
        events = engine.evaluate(snapshot(steam=True), now=1010)
        assert events[0].state == "cleared"

    def test_unwatched_process_stays_quiet(self):
        """A rule naming a process absent from watch_processes must not fire."""
        rule = {**self.RULE, "process": "never_watched.exe"}
        assert AlertEngine([rule]).evaluate(snapshot(), now=1000) == []


class TestMessages:
    def test_template_fields_render(self):
        rule = {
            **CPU_RULE,
            "for_seconds": 0,
            "message": "CPU on {device} hit {value_speech} percent.",
        }
        engine = AlertEngine([rule], device_name="the desktop")
        events = engine.evaluate(snapshot(cpu=95.4), now=1000)
        assert events[0].speech == "CPU on the desktop hit 95 percent."

    def test_byte_metrics_render_readably(self):
        rule = {
            "name": "disk_low",
            "metric": "disks.C.free_bytes",
            "op": "<",
            "threshold": 21474836480,
            "for_seconds": 0,
            "message": "Drive C is down to {value_human} free.",
        }
        engine = AlertEngine([rule])
        events = engine.evaluate(snapshot(free=10_737_418_240), now=1000)
        assert events[0].message == "Drive C is down to 10.0 GB free."

    def test_bad_template_falls_back_instead_of_crashing(self):
        rule = {**CPU_RULE, "for_seconds": 0, "message": "CPU is {nonexistent_field}."}
        engine = AlertEngine([rule])
        events = engine.evaluate(snapshot(cpu=95), now=1000)
        assert len(events) == 1
        assert "95" in events[0].message


class TestSnapshotPathsInMessages:
    """A message can reach into the snapshot: {mail.latest.subject}.

    A number on its own rarely makes a sentence worth hearing. "A meeting starts
    in 5 minutes" has to say which meeting, and the subject lives elsewhere in
    the same snapshot than the metric that fired.
    """

    MAILBOX = {
        **snapshot(),
        "mail": {
            "unread": 2,
            "seconds_since_latest": 30.0,
            "latest": {"from": "Jane Doe", "subject": "the quarterly numbers"},
        },
        "calendar": {
            "minutes_until_next": 4.7,
            "next": {"subject": "the design review", "provider": "teams"},
        },
    }

    def test_a_nested_path_renders(self):
        rule = {
            "name": "new_mail",
            "metric": "mail.seconds_since_latest",
            "op": "<",
            "threshold": 120,
            "for_seconds": 0,
            "message": "New mail from {mail.latest.from}: {mail.latest.subject}.",
        }
        events = AlertEngine([rule]).evaluate(self.MAILBOX, now=1000)
        assert events[0].speech == "New mail from Jane Doe: the quarterly numbers."

    def test_a_meeting_reminder_names_the_meeting(self):
        rule = {
            "name": "meeting_soon",
            "metric": "calendar.minutes_until_next",
            "op": "<",
            "threshold": 5,
            "for_seconds": 0,
            "message": "{calendar.next.subject} starts in {value_speech} minutes.",
        }
        # Rendered verbatim, so a subject that starts lowercase stays that way.
        # It reads aloud the same either way.
        events = AlertEngine([rule]).evaluate(self.MAILBOX, now=1000)
        assert events[0].speech == "the design review starts in 5 minutes."

    def test_floats_are_rounded_for_speech(self):
        rule = {
            "name": "x",
            "metric": "calendar.minutes_until_next",
            "op": "<",
            "threshold": 5,
            "for_seconds": 0,
            "message": "in {calendar.minutes_until_next} minutes",
        }
        events = AlertEngine([rule]).evaluate(self.MAILBOX, now=1000)
        assert events[0].message == "in 5 minutes"

    def test_a_named_field_still_wins_a_collision(self):
        """An existing rule cannot change meaning because a section shares a name."""
        rule = {**CPU_RULE, "for_seconds": 0, "message": "{device} at {value_speech}"}
        snap = {**self.MAILBOX, "device": {"nonsense": True}}
        engine = AlertEngine([rule], device_name="the desktop")
        assert engine.evaluate({**snap, "cpu": {"percent": 95}}, now=1000)[0].message == (
            "the desktop at 95"
        )

    def test_a_missing_path_falls_back_instead_of_crashing(self):
        rule = {
            "name": "x",
            "metric": "mail.unread",
            "op": ">",
            "threshold": 1,
            "for_seconds": 0,
            "message": "{mail.latest.nonexistent} arrived",
        }
        events = AlertEngine([rule]).evaluate(self.MAILBOX, now=1000)
        assert len(events) == 1
        assert "mail.unread" in events[0].message

    def test_an_absent_next_meeting_keeps_the_rule_quiet(self):
        """No meeting means minutes_until_next is None, which must not fire."""
        rule = {
            "name": "meeting_soon",
            "metric": "calendar.minutes_until_next",
            "op": "<",
            "threshold": 5,
            "for_seconds": 0,
        }
        snap = {**snapshot(), "calendar": {"minutes_until_next": None, "next": None}}
        assert AlertEngine([rule]).evaluate(snap, now=1000) == []


class TestMissingMetrics:
    def test_absent_metric_stays_quiet(self):
        rule = {"name": "x", "metric": "gpus.0.temperature_c", "op": ">", "threshold": 80,
                "for_seconds": 0}
        assert AlertEngine([rule]).evaluate(snapshot(), now=1000) == []

    def test_none_value_does_not_crash(self):
        rule = {"name": "x", "metric": "network.recv_rate_bps", "op": ">", "threshold": 1000,
                "for_seconds": 0}
        snap = {**snapshot(), "network": {"recv_rate_bps": None}}
        assert AlertEngine([rule]).evaluate(snap, now=1000) == []
