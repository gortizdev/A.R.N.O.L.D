"""Mail and calendar read out of Outlook.

The COM side cannot be exercised without a live Outlook, so these tests cover
the parts that decide what gets *said*: which appointment counts as next, how
long until it starts, what a join link looks like, and the sentences built from
all of that. The one piece of real-world fragility that is testable in isolation
is the locale-dependent date format Outlook's `Restrict` demands.
"""

import time

import pytest

from arnold.commands import build_registry
from arnold.commands.registry import CommandContext
from arnold.config import Config, OutlookConfig
from arnold.monitors.outlook import (
    OutlookProbe,
    OutlookUnavailable,
    _find_join_url,
    _to_ts,
    date_format_from_pattern,
)

TEAMS_BODY = """
Hi all,
________________________________________________________________________________
Microsoft Teams meeting
Join on your computer, mobile app or room device
Click here to join the meeting<https://teams.microsoft.com/l/meetup-join/19%3ameeting_YWJj@thread.v2/0?context=%7b%22Tid%22%3a%22abc%22%7d>
Meeting ID: 123 456 789
"""


class FakeComDate:
    """Stands in for the datetime subclass pywintypes returns."""

    def __init__(self, y, m, d, hh=0, mm=0, ss=0):
        self.year, self.month, self.day = y, m, d
        self.hour, self.minute, self.second = hh, mm, ss


def event(subject, start_offset_min, minutes=30, **extra):
    """An appointment `start_offset_min` from now, in the probe's cached shape."""
    start = time.time() + start_offset_min * 60
    return {
        "subject": subject,
        "start_ts": start,
        "end_ts": start + minutes * 60,
        "all_day": False,
        "location": "",
        "organizer": "",
        "provider": "",
        "join_url": "",
        "minutes": minutes,
        **extra,
    }


class TestDateFormat:
    """Restrict parses dates in the user's locale, and guessing wrong is silent."""

    @pytest.mark.parametrize(
        "pattern, expected",
        [
            ("M/d/yyyy", "%m/%d/%Y"),
            ("MM/dd/yyyy", "%m/%d/%Y"),
            ("dd/MM/yyyy", "%d/%m/%Y"),
            ("d.M.yyyy", "%d.%m.%Y"),
            ("yyyy-MM-dd", "%Y-%m-%d"),
            ("dd-MMM-yy", "%d-%m-%y"),
        ],
    )
    def test_locale_patterns(self, pattern, expected):
        assert date_format_from_pattern(pattern) == expected

    def test_day_names_are_dropped(self):
        assert "%a" not in date_format_from_pattern("dddd, MMMM d, yyyy")

    def test_unusable_pattern_falls_back(self):
        assert date_format_from_pattern("garbage") == "%m/%d/%Y"

    def test_the_real_machine_gives_a_usable_format(self):
        """A format that cannot render a date would break every calendar poll."""
        from arnold.monitors.outlook import _short_date_format

        rendered = time.strftime(_short_date_format(), time.localtime(1_700_000_000))
        assert any(char.isdigit() for char in rendered)


class TestJoinLinks:
    def test_teams_link_from_a_meeting_body(self):
        provider, url = _find_join_url(TEAMS_BODY)
        assert provider == "teams"
        assert url.startswith("https://teams.microsoft.com/l/meetup-join/")

    def test_the_wrapping_angle_bracket_is_not_part_of_the_url(self):
        assert not _find_join_url(TEAMS_BODY)[1].endswith(">")

    def test_escaped_ampersands_are_undone(self):
        _, url = _find_join_url("https://teams.microsoft.com/meet/123?a=1&amp;b=2")
        assert url == "https://teams.microsoft.com/meet/123?a=1&b=2"

    @pytest.mark.parametrize(
        "text, provider",
        [
            ("please join https://acme.zoom.us/j/9988776655?pwd=x now", "zoom"),
            ("https://meet.google.com/abc-defg-hij", "meet"),
            ("https://acme.webex.com/meet/someone", "webex"),
            ("no link here at all", ""),
        ],
    )
    def test_other_providers(self, text, provider):
        assert _find_join_url(text)[0] == provider


class TestComDates:
    def test_fields_are_read_as_local_time(self):
        stamp = _to_ts(FakeComDate(2026, 8, 12, 14, 30))
        assert time.localtime(stamp).tm_hour == 14
        assert time.localtime(stamp).tm_min == 30

    @pytest.mark.parametrize("value", [None, "not a date", FakeComDate(2026, 13, 40)])
    def test_junk_becomes_none_rather_than_raising(self, value):
        assert _to_ts(value) is None


class TestDerivedTimes:
    """Minutes-until is computed per snapshot, not per poll.

    The whole point: a poll every 60 seconds must still let an alert fire on the
    minute a meeting is due.
    """

    def derive(self, events, now=None):
        return OutlookProbe._with_derived_times(
            {"available": True, "events": events}, now or time.time()
        )

    def test_next_is_the_soonest_future_event(self):
        derived = self.derive([event("later", 90), event("sooner", 10)])
        assert derived["next"]["subject"] == "sooner"
        assert 9 < derived["minutes_until_next"] < 11

    def test_a_past_meeting_is_not_next(self):
        derived = self.derive([event("finished", -120, minutes=30)])
        assert derived["next"] is None
        assert derived["minutes_until_next"] is None

    def test_a_running_meeting_is_current_not_next(self):
        derived = self.derive([event("standup", -5, minutes=30)])
        assert derived["in_progress"] is True
        assert derived["current"]["subject"] == "standup"
        assert derived["next"] is None

    def test_current_and_next_can_both_exist(self):
        derived = self.derive([event("standup", -5), event("review", 40)])
        assert derived["current"]["subject"] == "standup"
        assert derived["next"]["subject"] == "review"

    def test_minutes_until_next_shrinks_between_snapshots(self):
        events = [event("review", 30)]
        first = self.derive(events, now=time.time())
        second = self.derive(events, now=time.time() + 600)
        assert second["minutes_until_next"] < first["minutes_until_next"] - 9

    def test_an_empty_calendar_is_quiet(self):
        derived = self.derive([])
        assert derived["minutes_until_next"] is None
        assert derived["in_progress"] is False


class TestProbeWithoutOutlook:
    def test_disabled_contributes_no_sections(self):
        assert OutlookProbe(OutlookConfig()).snapshot() == {}

    def test_unreachable_outlook_reports_a_speakable_reason(self, monkeypatch):
        """The reason has to be a sentence: it goes straight to text-to-speech."""
        probe = OutlookProbe(OutlookConfig(enabled=True))
        monkeypatch.setattr(
            probe,
            "_connect",
            lambda: (_ for _ in ()).throw(OutlookUnavailable("Outlook isn't running on this PC.")),
        )
        section = probe.snapshot()
        assert section["mail"]["available"] is False
        assert section["mail"]["error"] == "Outlook isn't running on this PC."
        assert section["calendar"]["error"] == "Outlook isn't running on this PC."

    def test_one_half_failing_does_not_take_the_other_down(self, monkeypatch):
        """A calendar that will not read must not hide the unread count."""
        probe = OutlookProbe(OutlookConfig(enabled=True))
        monkeypatch.setattr(probe, "_connect", lambda: object())
        monkeypatch.setattr(
            probe, "_read_mail", lambda ns: {"available": True, "error": None, "unread": 4}
        )
        monkeypatch.setattr(
            probe, "_read_calendar", lambda ns: (_ for _ in ()).throw(RuntimeError("no calendar"))
        )
        section = probe.snapshot()
        assert section["mail"]["unread"] == 4
        assert section["calendar"]["available"] is False

    def test_sections_can_be_switched_off_individually(self, monkeypatch):
        probe = OutlookProbe(OutlookConfig(enabled=True, calendar=False))
        monkeypatch.setattr(probe, "_connect", lambda: (_ for _ in ()).throw(RuntimeError("x")))
        assert "calendar" not in probe.snapshot()
        assert "mail" in probe.snapshot()


class TestExplainingFailures:
    """The reason has to name the fix, because the COM error never does."""

    def probe(self, **kwargs):
        return OutlookProbe(OutlookConfig(enabled=True, **kwargs))

    def test_closed_outlook_says_it_is_closed(self, monkeypatch):
        probe = self.probe()
        monkeypatch.setattr(probe, "outlook_is_running", lambda: False)
        assert "isn't running" in probe._explain(Exception("0x80080005"))

    def test_a_running_outlook_that_will_not_answer_points_at_the_account(self, monkeypatch):
        """OUTLOOK.EXE sitting on 'Add Account' looks identical to no Outlook.

        It registers for automation only once it has a mailbox open, so the COM
        error is the same one a closed Outlook gives.
        """
        probe = self.probe()
        monkeypatch.setattr(probe, "outlook_is_running", lambda: True)
        reason = probe._explain(Exception("0x800401e3"))
        assert "add an account" in reason
        assert "isn't running" not in reason

    def test_a_failed_start_suggests_signing_in_once(self):
        reason = self.probe(require_running=False)._explain(
            Exception("Server execution failed", "Server execution failed")
        )
        assert "add your account" in reason

    def test_every_reason_is_a_sentence(self, monkeypatch):
        """These go straight to text-to-speech, so no bare error codes."""
        for running in (True, False):
            for require in (True, False):
                probe = self.probe(require_running=require)
                monkeypatch.setattr(probe, "outlook_is_running", lambda r=running: r)
                reason = probe._explain(Exception("x", "y"))
                assert reason[0].isupper() and reason.endswith(".")


class TestIgnoreLists:
    def test_sender_and_subject_are_matched_case_insensitively(self):
        probe = OutlookProbe(
            OutlookConfig(enabled=True, ignore_senders=["No-Reply"], ignore_subjects=["NEWSLETTER"])
        )
        assert probe._ignored("no-reply@acme.com", "hello")
        assert probe._ignored("Jane", "Weekly newsletter")
        assert not probe._ignored("Jane", "the numbers")

    def test_nothing_is_ignored_by_default(self):
        assert not OutlookProbe(OutlookConfig(enabled=True))._ignored("anyone", "anything")


class TestSpeech:
    """The sentences Jarvis reads out."""

    @pytest.fixture
    def run(self):
        registry = build_registry()

        class FakeCollector:
            section: dict = {}

            def snapshot(self, include_window=True):
                return dict(self.section)

        collector = FakeCollector()
        config = Config()
        config.outlook.enabled = True
        ctx = CommandContext(config=config, collector=collector, alerts=None, jarvis=None)

        def go(command, **args):
            return registry.dispatch(command, args, ctx)

        go.collector = collector
        return go

    # -- mail ---------------------------------------------------------------

    def test_unread_count_and_newest_message(self, run):
        run.collector.section = {
            "mail": {
                "available": True,
                "unread": 3,
                "unread_important": 1,
                "seconds_since_latest": 240.0,
                "latest": {"from": "Jane Doe", "subject": "the quarterly numbers"},
            }
        }
        speech = run("query.mail").speech
        assert "3 unread messages" in speech
        assert "1 flagged important" in speech
        assert "Jane Doe" in speech
        assert "4 minutes ago" in speech

    def test_a_message_that_just_landed(self, run):
        run.collector.section = {
            "mail": {
                "available": True,
                "unread": 1,
                "seconds_since_latest": 12.0,
                "latest": {"from": "Bob", "subject": "lunch"},
            }
        }
        assert "just now" in run("query.mail").speech

    def test_an_empty_inbox_says_so(self, run):
        run.collector.section = {"mail": {"available": True, "unread": 0, "latest": None}}
        assert "Nothing unread" in run("query.mail").speech

    def test_one_unread_is_singular(self, run):
        run.collector.section = {"mail": {"available": True, "unread": 1, "latest": None}}
        assert "1 unread message." in run("query.mail").speech

    def test_switched_off_explains_the_fix(self, run):
        run.collector.section = {}
        result = run("query.mail")
        assert not result.ok
        assert "outlook section in my config" in result.speech

    def test_outlook_closed_is_reported_verbatim(self, run):
        run.collector.section = {
            "mail": {"available": False, "error": "Outlook isn't running on this PC."}
        }
        result = run("query.mail")
        assert not result.ok
        assert result.speech == "Outlook isn't running on this PC."

    # -- calendar -----------------------------------------------------------

    def test_next_meeting_gives_a_clock_time_and_a_lead(self, run):
        upcoming = event("the design review", 25, provider="teams")
        run.collector.section = {
            "calendar": {
                "available": True,
                "events": [upcoming],
                "current": None,
                "next": upcoming,
                "minutes_until_next": 25.0,
            }
        }
        speech = run("query.next_meeting").speech
        assert "the design review" in speech
        assert "on Teams" in speech
        assert "25 minutes away" in speech

    def test_a_meeting_in_progress_is_mentioned_first(self, run):
        current = event("the standup", -5)
        upcoming = event("the review", 55)
        run.collector.section = {
            "calendar": {
                "available": True,
                "events": [current, upcoming],
                "current": current,
                "next": upcoming,
                "minutes_until_next": 55.0,
            }
        }
        speech = run("query.next_meeting").speech
        assert speech.startswith("You're in the standup now")
        assert "Then the review" in speech

    def test_a_clear_calendar_names_the_window_it_checked(self, run):
        run.collector.section = {
            "calendar": {"available": True, "events": [], "current": None, "next": None}
        }
        assert "next 12 hours" in run("query.next_meeting").speech

    def test_agenda_lists_meetings_in_order(self, run):
        run.collector.section = {
            "calendar": {
                "available": True,
                "events": [event("standup", 30), event("review", 120), event("one to one", 200)],
            }
        }
        speech = run("query.agenda", hours=8).speech
        assert speech.startswith("3 meetings in the next 8 hours")
        assert speech.index("standup") < speech.index("review") < speech.index("one to one")

    def test_agenda_respects_the_window(self, run):
        run.collector.section = {
            "calendar": {"available": True, "events": [event("tomorrow", 600)]}
        }
        assert "Nothing on your calendar in the next 2 hours" in run("query.agenda", hours=2).speech

    def test_agenda_caps_what_it_reads_aloud(self, run):
        run.collector.section = {
            "calendar": {
                "available": True,
                "events": [event(f"meeting {i}", 20 * (i + 1)) for i in range(8)],
            }
        }
        speech = run("query.agenda", hours=8).speech
        assert "8 meetings" in speech
        assert "3 more after that" in speech

    def test_a_past_meeting_is_left_off_the_agenda(self, run):
        run.collector.section = {
            "calendar": {"available": True, "events": [event("finished", -90), event("next", 20)]}
        }
        speech = run("query.agenda").speech
        assert "1 meeting" in speech
        assert "finished" not in speech


class TestJoining:
    @pytest.fixture
    def joined(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            "arnold.commands.web._open_url",
            lambda url, browser: calls.append(url),
        )
        return calls

    @pytest.fixture
    def run(self):
        registry = build_registry()

        class FakeCollector:
            section: dict = {}

            def snapshot(self, include_window=True):
                return dict(self.section)

        collector = FakeCollector()
        ctx = CommandContext(config=Config(), collector=collector, alerts=None, jarvis=None)

        def go(command, **args):
            return registry.dispatch(command, args, ctx)

        go.collector = collector
        return go

    def _calendar(self, current=None, upcoming=None):
        return {
            "calendar": {
                "available": True,
                "events": [e for e in (current, upcoming) if e],
                "current": current,
                "next": upcoming,
                "minutes_until_next": 10.0 if upcoming else None,
            }
        }

    def test_opens_the_join_link_for_the_next_meeting(self, run, joined):
        upcoming = event("review", 10, provider="teams", join_url="https://teams.microsoft.com/l/meetup-join/x")
        run.collector.section = self._calendar(upcoming=upcoming)
        result = run("web.join_meeting")
        assert result.ok
        assert joined == ["https://teams.microsoft.com/l/meetup-join/x"]

    def test_a_running_meeting_wins_over_the_next_one(self, run, joined):
        current = event("standup", -5, join_url="https://teams.microsoft.com/l/meetup-join/now")
        upcoming = event("review", 55, join_url="https://teams.microsoft.com/l/meetup-join/later")
        run.collector.section = self._calendar(current=current, upcoming=upcoming)
        assert run("web.join_meeting").ok
        assert joined == ["https://teams.microsoft.com/l/meetup-join/now"]

    def test_next_can_be_asked_for_explicitly(self, run, joined):
        current = event("standup", -5, join_url="https://teams.microsoft.com/l/meetup-join/now")
        upcoming = event("review", 55, join_url="https://teams.microsoft.com/l/meetup-join/later")
        run.collector.section = self._calendar(current=current, upcoming=upcoming)
        assert run("web.join_meeting", which="next").ok
        assert joined == ["https://teams.microsoft.com/l/meetup-join/later"]

    def test_no_meeting_is_a_plain_refusal(self, run, joined):
        run.collector.section = self._calendar()
        result = run("web.join_meeting")
        assert not result.ok
        assert "don't have a meeting" in result.speech
        assert joined == []

    def test_a_meeting_with_no_link_says_where_it_is(self, run, joined):
        run.collector.section = self._calendar(
            upcoming=event("all hands", 10, location="Boardroom")
        )
        result = run("web.join_meeting")
        assert not result.ok
        assert "Boardroom" in result.speech
        assert joined == []
