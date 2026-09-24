"""Teams and Outlook, read from the notification centre.

WinRT cannot run under the test runner, so the listener is a fake handing over
raw toasts. What is tested is everything that decides what gets said: which
toasts count, what is skipped, that the backlog at startup is not news, and
the sentences built from all of it.
"""

import time

import pytest

from arnold.commands import build_registry
from arnold.commands.registry import CommandContext
from arnold.config import Config, NotificationsConfig
from arnold.monitors.notifications import (
    DEFAULT_APPS,
    NotificationWatch,
    NotificationsUnavailable,
    WorkNotification,
    classify,
    ignored,
    parse,
)

TEAMS = "MSTeams_8wekyb3d8bbwe!MSTeams"
OUTLOOK = "Microsoft.OutlookForWindows_8wekyb3d8bbwe!Microsoft.OutlookforWindows"
NVIDIA = "com.nvidia.nvapp"


def toast(tid, aumid, texts, age=0.0, name=""):
    return (tid, aumid, name, texts, time.time() - age)


class FakeListener:
    def __init__(self, toasts=None, fail=None):
        self.toasts = list(toasts or [])
        self.fail = fail
        self.closed = False

    def read(self):
        if self.fail:
            raise self.fail
        return list(self.toasts)

    def close(self):
        self.closed = True


def watch(listener, **overrides):
    cfg = NotificationsConfig(enabled=True, **overrides)
    return NotificationWatch(cfg, listener_factory=lambda: listener)


class TestParsing:
    def test_only_the_configured_apps_count(self):
        assert classify(TEAMS, "Microsoft Teams", DEFAULT_APPS) == "Teams"
        assert classify(OUTLOOK, "Outlook", DEFAULT_APPS) == "Outlook"
        assert classify(NVIDIA, "NVIDIA App", DEFAULT_APPS) is None
        assert parse(toast(1, NVIDIA, ["Driver", "Update"]), DEFAULT_APPS) is None

    def test_lines_become_who_what_and_the_rest(self):
        note = parse(toast(7, OUTLOOK, ["Jane Doe", "Quarterly  numbers", "Hi, attached is..."]), DEFAULT_APPS)
        assert (note.app, note.title, note.body) == ("Outlook", "Jane Doe", "Quarterly numbers")
        assert note.extra == ["Hi, attached is..."]

    def test_a_custom_app_map_replaces_the_defaults(self):
        apps = {"Slack": "slack"}
        assert classify("com.squirrel.slack.slack", "Slack", apps) == "Slack"
        assert classify(TEAMS, "Microsoft Teams", apps) is None

    def test_speech_shapes(self):
        teams = WorkNotification(1, "Teams", "Jane Doe", "can you look at the deck")
        mail = WorkNotification(2, "Outlook", "Bob", "Invoice 42")
        assert teams.speech() == "Teams message from Jane Doe: can you look at the deck"
        assert mail.speech() == "Mail from Bob, Invoice 42"
        assert teams.speech(include_text=False) == "Teams message from Jane Doe"

    def test_ignore_lists_match_sender_or_text(self):
        cfg = NotificationsConfig(ignore_senders=["no-reply"], ignore_subjects=["jira"])
        assert ignored(WorkNotification(1, "Outlook", "No-Reply Bot", "hello"), cfg)
        assert ignored(WorkNotification(2, "Teams", "Jane", "JIRA ticket moved"), cfg)
        assert not ignored(WorkNotification(3, "Teams", "Jane", "lunch?"), cfg)


class TestWatch:
    def test_the_backlog_at_startup_is_context_not_news(self):
        listener = FakeListener([toast(1, TEAMS, ["Jane", "morning"], age=3600)])
        w = watch(listener)
        w.poll()
        assert w.drain_new() == []
        # ...but it is still there to be asked about.
        recent = w.snapshot()["work_notifications"]["recent"]
        assert [n["title"] for n in recent] == ["Jane"]

    def test_a_new_toast_is_news_once(self):
        listener = FakeListener([toast(1, TEAMS, ["Jane", "morning"], age=3600)])
        w = watch(listener)
        w.poll()
        listener.toasts.append(toast(2, OUTLOOK, ["Bob", "Invoice", "please pay"]))
        w.poll()
        fresh = w.drain_new()
        assert [n.app for n in fresh] == ["Outlook"]
        w.poll()
        assert w.drain_new() == []

    def test_other_apps_and_ignored_senders_never_surface(self):
        listener = FakeListener([])
        w = watch(listener, ignore_senders=["bot"])
        w.poll()
        listener.toasts += [
            toast(1, NVIDIA, ["Driver", "Update"]),
            toast(2, TEAMS, ["Build Bot", "pipeline green"]),
            toast(3, TEAMS, ["Jane", "lunch?"]),
        ]
        w.poll()
        assert [n.title for n in w.drain_new()] == ["Jane"]

    def test_the_section_counts_the_last_hour_by_app(self):
        listener = FakeListener([
            toast(1, TEAMS, ["Jane", "a"], age=60),
            toast(2, TEAMS, ["Jane", "b"], age=120),
            toast(3, OUTLOOK, ["Bob", "c"], age=7200),
        ])
        w = watch(listener)
        w.poll()
        section = w.snapshot()["work_notifications"]
        assert section["available"] is True
        assert section["last_hour"] == 2 and section["by_app"] == {"Teams": 2}
        # newest first
        assert [n["body"] for n in section["recent"]] == ["a", "b", "c"]

    def test_unavailable_is_reported_not_raised(self):
        listener = FakeListener(fail=NotificationsUnavailable("Windows has not allowed notification access"))
        w = watch(listener)
        w.poll()
        section = w.snapshot()["work_notifications"]
        assert section["available"] is False
        assert "allowed" in section["error"]
        assert listener.closed  # a fresh adapter is tried next time

    def test_switched_off_means_no_section(self):
        w = NotificationWatch(NotificationsConfig(enabled=False), listener_factory=FakeListener)
        assert w.snapshot() == {}


class FakeCollector:
    def __init__(self, section):
        self.section = section

    def snapshot(self, include_window=True):
        snap = {"ts": time.time()}
        if self.section is not None:
            snap["work_notifications"] = self.section
        return snap


def run(section, **args):
    registry = build_registry()
    ctx = CommandContext(config=Config(), collector=FakeCollector(section), alerts=None, jarvis=None)
    return registry.dispatch("query.notifications", args, ctx)


class TestQuery:
    def test_switched_off_explains_itself(self):
        result = run(None)
        assert not result.ok and "notifications section" in result.error

    def test_nothing_recent(self):
        result = run({"available": True, "recent": []}, hours=2)
        assert result.ok and result.speech == "Nothing from Teams or Outlook in the last 2 hours."

    def test_reads_out_the_latest_with_a_count(self):
        now = time.time()
        section = {
            "available": True,
            "recent": [
                {"id": 2, "app": "Outlook", "title": "Bob", "body": "Invoice 42", "extra": [], "ts": now - 300},
                {"id": 1, "app": "Teams", "title": "Jane", "body": "lunch?", "extra": [], "ts": now - 900},
                {"id": 0, "app": "Teams", "title": "Old", "body": "x", "extra": [], "ts": now - 20000},
            ],
        }
        result = run(section, hours=4)
        assert result.ok
        assert result.speech.startswith("In the last 4 hours: 1 mail and 1 Teams message.")
        assert "mail from Bob, Invoice 42, 5 minutes ago" in result.speech
        assert "Jane on Teams, lunch?, 15 minutes ago" in result.speech
        assert result.result["count"] == 2

    def test_narrowed_to_one_app(self):
        now = time.time()
        section = {"available": True, "recent": [
            {"id": 2, "app": "Outlook", "title": "Bob", "body": "Invoice", "extra": [], "ts": now - 60},
        ]}
        result = run(section, app="teams")
        assert result.speech == "Nothing from Teams in the last 4 hours."

    def test_unavailable_passes_the_reason_on(self):
        result = run({"available": False, "error": "Windows has not allowed notification access"})
        assert not result.ok and "allowed" in result.error
