"""Commands that need the logged-on desktop.

Jarvis reaches this machine over SSH, which lands in session 0 - a session
with no visible desktop. A browser launched there opens where nobody can see
it and the call still reports success, which is the worst possible failure:
silent. `exec` detects that and hands the work to the agent already running in
the logon session.
"""

import argparse

import pytest

from arnold import cli
from arnold.commands import build_registry
from arnold.config import Config


@pytest.fixture
def registry():
    return build_registry()


def args(**kw):
    return argparse.Namespace(speak=False, **kw)


class TestMarking:
    @pytest.mark.parametrize(
        "name",
        [
            "web.open",
            "web.search",
            "control.launch",
            "desktop.notify",
            "desktop.screenshot",
            "desktop.clipboard_get",
            "desktop.clipboard_set",
            "artifact.create",
        ],
    )
    def test_screen_and_session_bound_commands_are_marked(self, registry, name):
        assert registry.get(name).needs_desktop, f"{name} would fail silently over SSH"

    @pytest.mark.parametrize(
        "name", ["query.cpu", "query.disk", "system.info", "control.lock", "web.sites"]
    )
    def test_headless_safe_commands_are_not(self, registry, name):
        """Marking these would add a pointless MQTT round-trip to every call."""
        assert not registry.get(name).needs_desktop

    def test_the_flag_is_reported(self, registry):
        entry = next(c for c in registry.describe() if c["name"] == "web.open")
        assert entry["needs_desktop"] is True


class TestDelegation:
    def test_runs_locally_when_the_desktop_is_reachable(self, registry, monkeypatch):
        monkeypatch.setattr(
            "arnold.platform_win.session.has_interactive_desktop",
            lambda: True,
        )
        assert cli._delegate_if_headless(
            Config(), registry, "web.open", {}, args()
        ) is None

    def test_headless_commands_are_left_alone(self, registry, monkeypatch):
        monkeypatch.setattr(
            "arnold.platform_win.session.has_interactive_desktop",
            lambda: False,
        )
        assert cli._delegate_if_headless(
            Config(), registry, "query.cpu", {}, args()
        ) is None

    def test_an_unknown_command_falls_through(self, registry, monkeypatch):
        monkeypatch.setattr(
            "arnold.platform_win.session.has_interactive_desktop",
            lambda: False,
        )
        assert cli._delegate_if_headless(
            Config(), registry, "not.a.command", {}, args()
        ) is None

    def test_forwarded_to_the_agent_when_headless(self, registry, monkeypatch):
        monkeypatch.setattr(
            "arnold.platform_win.session.has_interactive_desktop",
            lambda: False,
        )
        sent = {}

        def fake_round_trip(config, command, command_args, *, speak, timeout):
            sent.update(command=command, args=command_args)
            return {"ok": True, "speech": "done"}

        monkeypatch.setattr(cli, "_round_trip", fake_round_trip)
        config = Config()
        config.mqtt.enabled = True

        reply = cli._delegate_if_headless(
            config, registry, "web.open", {"site": "youtube"}, args()
        )
        assert reply["ok"] and reply["via"] == "agent"
        assert sent == {"command": "web.open", "args": {"site": "youtube"}}

    def test_speech_is_not_said_twice(self, registry, monkeypatch):
        """The agent speaks it; the forwarding process must not repeat it."""
        monkeypatch.setattr(
            "arnold.platform_win.session.has_interactive_desktop",
            lambda: False,
        )
        monkeypatch.setattr(
            cli, "_round_trip", lambda *a, **k: {"ok": True, "speech": "done"}
        )
        config = Config()
        config.mqtt.enabled = True
        namespace = args()
        namespace.speak = True

        cli._delegate_if_headless(config, registry, "web.open", {}, namespace)
        assert namespace.speak is False

    def test_a_silent_agent_is_reported_not_swallowed(self, registry, monkeypatch):
        monkeypatch.setattr(
            "arnold.platform_win.session.has_interactive_desktop",
            lambda: False,
        )

        def boom(*a, **k):
            raise RuntimeError("no reply within timeout")

        monkeypatch.setattr(cli, "_round_trip", boom)
        config = Config()
        config.mqtt.enabled = True

        reply = cli._delegate_if_headless(config, registry, "web.open", {}, args())
        assert not reply["ok"]
        assert "did not answer" in reply["error"]

    def test_without_mqtt_it_explains_rather_than_pretending(self, registry, monkeypatch):
        monkeypatch.setattr(
            "arnold.platform_win.session.has_interactive_desktop",
            lambda: False,
        )
        config = Config()
        config.mqtt.enabled = False

        reply = cli._delegate_if_headless(config, registry, "web.open", {}, args())
        assert not reply["ok"]
        assert "desktop" in reply["error"]


def test_session_probe_is_self_consistent():
    """Whatever the machine reports, the answers must agree with each other."""
    from arnold.platform_win import session

    facts = session.describe()
    if facts["interactive"]:
        assert facts["session"] == facts["console_session"] > 0
