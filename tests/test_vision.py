"""Letting the assistant actually see the screen.

A function_call_output is a plain string, so the screenshot cannot travel in
the tool result. It is handed back under `__screen_b64__` and the realtime
layer turns it into a separate `input_image` message - the same contract
Jarvis uses on the Pi. These pin that handoff, since a silent failure looks
exactly like the model choosing not to mention what it saw.
"""

import json

import pytest

from arnold.config import Config
from arnold.voice.realtime import RealtimeConversation
from arnold.voice.tools import ToolDispatcher, build_tools


@pytest.fixture
def config():
    cfg = Config()
    cfg.jarvis.home_assistant.token = ""  # keep the tool list to the basics
    return cfg


def tool_names(cfg):
    return [t["name"] for t in build_tools(cfg)]


class TestToolSurface:
    def test_view_screen_is_offered(self, config):
        assert "view_screen" in tool_names(config)

    def test_vision_off_removes_the_tool(self, config):
        config.voice.vision = False
        assert "view_screen" not in tool_names(config)
        # ...and the rest of the surface is untouched.
        assert "pc_agent" in tool_names(config)
        assert "end_conversation" in tool_names(config)

    def test_screen_argument_is_optional(self, config):
        tool = next(t for t in build_tools(config) if t["name"] == "view_screen")
        assert "required" not in tool["parameters"]

    def test_pc_agent_steers_away_from_the_saving_command(self, config):
        """The model must not reach for desktop.screenshot to *see* anything."""
        tool = next(t for t in build_tools(config) if t["name"] == "pc_agent")
        assert "view_screen" in tool["description"]


class TestDispatcher:
    def test_capture_comes_back_under_the_agreed_key(self, config, monkeypatch):
        monkeypatch.setattr(
            "arnold.voice.tools.screen.capture_for_vision",
            lambda **kw: {
                "jpeg_base64": "AAAA", "bytes": 4, "width": 1600,
                "height": 900, "screen": kw.get("screen"),
            },
        )
        result = ToolDispatcher(config, None)("view_screen", {})
        assert result["__screen_b64__"] == "AAAA"
        assert result["screen"] == "active"  # the user's own monitor by default

    def test_an_explicit_monitor_is_passed_through(self, config, monkeypatch):
        seen = {}

        def fake(**kw):
            seen.update(kw)
            return {"jpeg_base64": "x", "bytes": 1, "width": 1, "height": 1, "screen": "2"}

        monkeypatch.setattr(
            "arnold.voice.tools.screen.capture_for_vision", fake
        )
        ToolDispatcher(config, None)("view_screen", {"screen": "2"})
        assert seen["screen"] == "2"
        assert seen["max_width"] == config.voice.vision_max_width

    def test_disabled_vision_refuses_rather_than_captures(self, config, monkeypatch):
        config.voice.vision = False

        def explode(**kw):
            raise AssertionError("must not capture when vision is off")

        monkeypatch.setattr(
            "arnold.voice.tools.screen.capture_for_vision", explode
        )
        assert "error" in ToolDispatcher(config, None)("view_screen", {})

    def test_capture_failure_is_reported_not_raised(self, config, monkeypatch):
        def explode(**kw):
            raise RuntimeError("screen locked")

        monkeypatch.setattr(
            "arnold.voice.tools.screen.capture_for_vision", explode
        )
        result = ToolDispatcher(config, None)("view_screen", {})
        assert "screen locked" in result["error"]


class TestRealtimeInjection:
    """_run_tool is where the image becomes a conversation item."""

    def conversation(self, sent, result):
        rt = RealtimeConversation("key", object(), [], lambda name, args: result)
        rt._send = sent.append
        return rt

    def test_image_becomes_an_input_image_message(self):
        sent = []
        self.conversation(sent, {"status": "captured", "__screen_b64__": "SGk="})._run_tool(
            "call-1", "view_screen", "{}"
        )

        image_items = [
            e for e in sent
            if e.get("item", {}).get("type") == "message"
        ]
        assert len(image_items) == 1
        content = image_items[0]["item"]["content"][0]
        assert image_items[0]["item"]["role"] == "user"
        assert content["type"] == "input_image"
        assert content["image_url"] == "data:image/jpeg;base64,SGk="
        assert content["detail"] == "high"

    def test_the_image_is_stripped_from_the_tool_output(self):
        """Base64 in the function output would burn context for nothing."""
        sent = []
        self.conversation(
            sent, {"status": "captured", "__screen_b64__": "A" * 5000}
        )._run_tool("call-1", "view_screen", "{}")

        output = next(
            e for e in sent if e.get("item", {}).get("type") == "function_call_output"
        )
        assert "__screen_b64__" not in output["item"]["output"]
        assert json.loads(output["item"]["output"])["status"] == "captured"

    def test_a_response_is_requested_after_the_image(self):
        sent = []
        self.conversation(sent, {"__screen_b64__": "x"})._run_tool("c", "view_screen", "{}")
        kinds = [e.get("type") for e in sent]
        assert kinds[-1] == "response.create"
        # The image has to be in the conversation before the model replies.
        assert kinds.index("conversation.item.create") < len(kinds) - 1

    def test_tools_without_an_image_send_no_message_item(self):
        sent = []
        self.conversation(sent, {"speech": "42% used"})._run_tool("c", "pc_agent", "{}")
        assert not [e for e in sent if e.get("item", {}).get("type") == "message"]

    def test_a_failing_tool_still_answers_the_call(self):
        def boom(name, args):
            raise RuntimeError("nope")

        sent = []
        rt = RealtimeConversation("key", object(), [], boom)
        rt._send = sent.append
        rt._run_tool("call-9", "view_screen", "{}")
        output = next(
            e for e in sent if e.get("item", {}).get("type") == "function_call_output"
        )
        assert output["item"]["call_id"] == "call-9"
        assert "nope" in output["item"]["output"]
