"""jarvis.enabled: false - the Pi is off and nothing on this PC tries it."""

from __future__ import annotations

import pytest

from arnold.config import Config
from arnold.jarvis import JarvisClient
from arnold.voice import session_config
from arnold.voice.brain import LocalBrain, build_brain
from arnold.voice.tools import build_tools


@pytest.fixture
def config():
    cfg = Config()
    cfg.jarvis.home_assistant.token = "t"
    cfg.jarvis.enabled = False
    return cfg


def _tool_names(config) -> set[str]:
    return {tool.get("name") for tool in build_tools(config)}


class TestSpeechRoute:
    @pytest.mark.parametrize("chosen", ["", "auto", "jarvis", "both", "local"])
    def test_everything_that_would_try_the_pi_speaks_here(self, config, chosen):
        config.speech.route = chosen
        assert config.speech_route() == "local"

    def test_none_still_means_silence(self, config):
        config.speech.route = "none"
        assert config.speech_route() == "none"

    def test_on_by_default(self):
        cfg = Config()
        cfg.speech.route = "auto"
        assert cfg.jarvis.enabled is True
        assert cfg.speech_route() == "auto"


class TestClient:
    def test_does_not_speak_and_does_not_raise(self, config):
        client = JarvisClient(config.jarvis)
        assert not client.enabled
        assert client.say("hello")["spoken"] is False

    def test_check_says_it_is_switched_off_without_reaching_out(self, config):
        probe = JarvisClient(config.jarvis).check()
        assert probe["ok"] is False
        assert "switched off" in probe["detail"]


class TestTools:
    def test_no_jarvis_or_home_assistant_tools(self, config):
        names = _tool_names(config)
        assert not names & {"ask_jarvis", "tell_jarvis", "ha_get_state",
                            "ha_call_service", "ha_list_entities"}

    def test_offered_again_when_the_pi_is_back(self, config):
        config.jarvis.enabled = True
        assert {"ask_jarvis", "ha_get_state"} <= _tool_names(config)

    def test_the_brain_is_local(self, config):
        assert isinstance(build_brain(config, context=None), LocalBrain)


class TestSessionConfig:
    def test_never_fetches_over_ssh(self, config, monkeypatch):
        def boom(*a, **k):
            raise AssertionError("tried to reach the Pi")

        monkeypatch.setattr(session_config, "_fetch_from_pi", boom)
        monkeypatch.setattr(session_config, "_read_cache", lambda: None)
        session = session_config._load_session_config(config, refresh=True, mirror=False)
        assert session.source == session_config.SessionConfig().source
