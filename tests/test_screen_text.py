"""Reading the window in front, as text rather than as a picture.

The COM plumbing is not what these test - that needs a real desktop with a
real window on it. What they pin down are the guard rails, because this
command reads whatever the user happens to be looking at: the switch that
turns it off, the list of windows it refuses outright, the cap on how much
comes back, and the promise that the text itself never reaches the log.
"""

from __future__ import annotations

import logging

import pytest

from arnold.alerts import AlertEngine
from arnold.commands import CommandContext, build_registry
from arnold.config import Config
from arnold.monitors.collector import Collector
from arnold.platform_win import uia


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


@pytest.fixture
def screen(monkeypatch):
    """Stands in for the desktop: one window, whatever text we choose."""

    state = {
        "window": {"title": "Notes - Notepad", "process": "notepad.exe", "pid": 42},
        "text": "the quick brown fox",
        "calls": [],
        "raises": None,
        "available": True,
    }

    def fake_active_window():
        return dict(state["window"])

    def fake_read(scope="window", max_chars=4000, timeout=4.0):
        state["calls"].append((scope, max_chars))
        if state["raises"] is not None:
            raise state["raises"]
        text = state["text"]
        truncated = len(text) > max_chars
        if truncated:
            text = text[:max_chars] + " ... (truncated)"
        return {
            **state["window"],
            "text": text,
            "chars": len(text),
            "truncated": truncated,
            "how": "text_pattern",
        }

    monkeypatch.setattr(
        "arnold.platform_win.window.active_window", fake_active_window
    )
    monkeypatch.setattr(uia, "read_active_window", fake_read)
    monkeypatch.setattr(uia, "available", lambda: state["available"])
    return state


def read(registry, ctx, **args):
    return registry.dispatch("desktop.read_window", args, ctx)


class TestReachable:
    def test_it_is_marked_as_needing_the_desktop(self, registry):
        """Over SSH it must be handed to the agent, not run in session 0."""
        assert registry.get("desktop.read_window").needs_desktop

    def test_it_is_offered_to_the_voice_session(self):
        from arnold.voice.tools import PC_COMMANDS

        assert "desktop.read_window" in PC_COMMANDS

    def test_the_model_is_told_when_to_use_each(self):
        """Text goes to read_window, pixels go to view_screen."""
        from arnold.voice.tools import build_tools

        cfg = Config()
        pc_agent = next(t for t in build_tools(cfg) if t["name"] == "pc_agent")
        assert "desktop.read_window" in pc_agent["description"]
        assert "view_screen" in pc_agent["description"]

    def test_it_works_with_vision_switched_off(self, registry, ctx, config, screen):
        """The point of it: no picture is taken, so vision being off is
        irrelevant to whether the assistant can read the screen."""
        from arnold.voice.tools import build_tools

        config.voice.vision = False
        assert not any(t["name"] == "view_screen" for t in build_tools(config))
        assert read(registry, ctx).ok


class TestGuards:
    def test_off_in_config_is_refused_with_a_sentence(self, registry, ctx, config, screen):
        config.voice.screen_text = False
        result = read(registry, ctx)
        assert not result.ok
        assert "switched off" in result.error
        assert screen["calls"] == []

    @pytest.mark.parametrize(
        "process,title",
        [
            ("KeePassXC.exe", "Passwords"),
            ("chrome.exe", "1Password vault"),
            ("firefox.exe", "Private Browsing"),
            ("msedge.exe", "InPrivate - Edge"),
        ],
    )
    def test_a_password_manager_is_refused_before_anything_is_read(
        self, registry, ctx, screen, process, title
    ):
        screen["window"] = {"title": title, "process": process, "pid": 1}
        result = read(registry, ctx)
        assert not result.ok
        assert "rather not" in result.error
        # The refusal has to come first, or the guard is decoration.
        assert screen["calls"] == []

    def test_the_deny_list_is_configurable(self, registry, ctx, config, screen):
        config.voice.screen_text_deny = ["notepad"]
        assert not read(registry, ctx).ok

    def test_an_ordinary_window_is_allowed(self, registry, ctx, screen):
        assert read(registry, ctx).ok


class TestResults:
    def test_the_text_comes_back_whole_in_the_result(self, registry, ctx, screen):
        result = read(registry, ctx)
        assert result.result["text"] == "the quick brown fox"
        assert "the quick brown fox" in result.speech

    def test_text_is_capped_and_marked_truncated(self, registry, ctx, screen):
        screen["text"] = "x" * 10000
        result = read(registry, ctx, max_chars=4000)
        assert result.result["truncated"] is True
        assert result.result["text"].endswith("(truncated)")
        assert len(result.result["text"]) < 4100

    def test_the_cap_comes_from_config_by_default(self, registry, ctx, config, screen):
        config.voice.screen_text_max_chars = 1234
        read(registry, ctx)
        assert screen["calls"][0][1] == 1234

    def test_the_scope_is_passed_through(self, registry, ctx, screen):
        read(registry, ctx, scope="focus")
        assert screen["calls"][0][0] == "focus"

    def test_an_empty_window_says_so_rather_than_failing(self, registry, ctx, screen):
        screen["text"] = ""
        result = read(registry, ctx)
        assert result.ok
        assert "no text I can read" in result.speech

    def test_an_empty_window_without_comtypes_explains_the_install(
        self, registry, ctx, screen
    ):
        screen["text"] = ""
        screen["available"] = False
        assert ".[uia]" in read(registry, ctx).speech

    def test_a_hung_window_is_an_error_not_a_hang(self, registry, ctx, screen):
        screen["raises"] = uia.UiaError("That window isn't answering.")
        result = read(registry, ctx)
        assert not result.ok
        assert "isn't answering" in result.error

    def test_it_does_not_log_the_text(self, registry, ctx, screen, caplog):
        """This is somebody's screen. The count is useful; the content is not
        ours to write into a file that gets read back later."""
        screen["text"] = "hunter2 is the password"
        with caplog.at_level(logging.DEBUG, logger="arnold.commands.desktop"):
            read(registry, ctx)
        assert "hunter2" not in caplog.text
        assert "read 23 characters" in caplog.text


class TestTheModuleItself:
    def test_the_install_hint_names_the_extra(self):
        assert ".[uia]" in uia.INSTALL_HINT

    def test_available_is_a_bool_and_never_raises(self):
        assert isinstance(uia.available(), bool)

    def test_reading_with_no_window_in_front_is_an_error(self, monkeypatch):
        monkeypatch.setattr(
            "arnold.platform_win.window.foreground_hwnd", lambda: 0
        )
        with pytest.raises(uia.UiaError):
            uia.read_active_window()

    def test_a_worker_that_never_answers_times_out(self, monkeypatch):
        """A window that has stopped responding must not stop the agent."""
        import time

        monkeypatch.setattr(
            "arnold.platform_win.window.foreground_hwnd", lambda: 1
        )
        monkeypatch.setattr(
            "arnold.platform_win.window.active_window",
            lambda: {"title": "Stuck", "process": "stuck.exe", "pid": 1},
        )
        monkeypatch.setattr(uia, "_read", lambda *a, **k: time.sleep(30))
        started = time.monotonic()
        with pytest.raises(uia.UiaError, match="answering"):
            uia.read_active_window(timeout=0.5)
        assert time.monotonic() - started < 5
