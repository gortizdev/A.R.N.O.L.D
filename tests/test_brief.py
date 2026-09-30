"""printer.brief: questions that turn a short request into a precise one.

The model is stubbed at the HTTP call; what is tested is what is sent (the
answers so far, "ask nothing more"), and how its reply is held to shape.
"""

import io
import json

import pytest

from arnold import brief
from arnold.commands import build_registry
from arnold.commands.registry import CommandContext
from arnold.config import Config


def reply(content):
    body = json.dumps({"choices": [{"message": {"content": json.dumps(content)}}]}).encode()
    return io.BytesIO(body)


@pytest.fixture
def model(monkeypatch):
    """The chat endpoint, answering with whatever `model.reply` is set to."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    sent = []

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def urlopen(request, timeout=None):
        sent.append(json.loads(request.data.decode()))
        return Response(reply(stub.reply).getvalue())

    class Stub:
        reply: dict = {}

    stub = Stub()
    stub.sent = sent
    monkeypatch.setattr(brief.urllib.request, "urlopen", urlopen)
    return stub


def test_a_short_request_gets_questions(model):
    model.reply = {"title": "Owl", "description": "an owl", "height_mm": None, "questions": [
        {"question": "Cartoon or realistic?", "options": ["Cartoon", "Realistic"]},
        {"question": "What is it standing on?", "options": ["A stump", "A book", "Nothing"]},
    ]}
    found = brief.ask("an owl", model="gpt-5.4-mini")
    assert found.title == "Owl"
    assert [q["question"] for q in found.questions] == ["Cartoon or realistic?", "What is it standing on?"]
    assert model.sent[0]["response_format"] == {"type": "json_object"}
    assert "Request: an owl" in model.sent[0]["messages"][1]["content"]


def test_answers_are_sent_and_never_asked_again(model):
    model.reply = {"description": "a cartoon owl on a stump", "questions": [
        {"question": "Cartoon or realistic?", "options": ["Cartoon"]},
        {"question": "Wings open or folded?", "options": ["Folded", "Open"]},
    ]}
    answers = [{"question": "Cartoon or realistic?", "answer": "Cartoon"},
               {"question": "Base?", "answer": ""}]
    found = brief.ask("an owl", answers, model="gpt-5.4-mini")
    sent = model.sent[0]["messages"][1]["content"]
    assert "Cartoon or realistic? Cartoon" in sent
    assert "Base?" not in sent  # skipped, so not an answer
    assert [q["question"] for q in found.questions] == ["Wings open or folded?"]


def test_zero_questions_asks_for_the_description_only(model):
    model.reply = {"description": "a cartoon owl", "questions": [{"question": "More?", "options": []}]}
    found = brief.ask("an owl", [{"question": "Style?", "answer": "cartoon"}], model="m", questions=0)
    assert found.questions == []
    assert "Ask nothing more" in model.sent[0]["messages"][1]["content"]


def test_a_malformed_reply_is_held_to_shape():
    found = brief.read({"description": "", "height_mm": "tall", "questions": [
        "not a dict", {"question": ""}, {"question": "Pose?", "options": ["a", "", "b", "c", "d", "e"]},
    ]}, "an owl", [], 4)
    assert found.description == "an owl"
    assert found.height_mm is None
    assert found.questions == [{"question": "Pose?", "options": ["a", "b", "c", "d"]}]
    assert brief.read({"height_mm": 80}, "x", [], 4).height_mm == 80
    assert brief.read({"height_mm": 2}, "x", [], 4).height_mm is None


def test_no_key_no_questions(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(brief.BriefError, match="OPENAI_API_KEY"):
        brief.ask("an owl", model="m")


class TestCommand:
    def go(self, **args):
        ctx = CommandContext(config=Config(), collector=None, alerts=None, jarvis=None)
        return build_registry().dispatch("printer.brief", args, ctx)

    def test_questions_are_spoken_and_returned(self, model):
        model.reply = {"title": "Owl", "description": "an owl",
                       "questions": [{"question": "Cartoon or realistic?", "options": ["Cartoon"]}]}
        result = self.go(prompt="an owl")
        assert result.ok, result.speech
        assert result.speech == "A few questions first. Cartoon or realistic?"
        assert result.result["questions"][0]["options"] == ["Cartoon"]

    def test_answers_come_as_json_from_the_command_line(self, model):
        model.reply = {"title": "Owl", "description": "a cartoon owl", "questions": []}
        result = self.go(prompt="an owl", answers='[{"question":"Style?","answer":"cartoon"}]', questions=0)
        assert result.ok, result.speech
        assert result.speech == "I'd sculpt a cartoon owl"
        assert "Style? cartoon" in model.sent[0]["messages"][1]["content"]

    def test_bad_answers_are_refused(self, model):
        assert not self.go(prompt="an owl", answers="not json").ok
        assert not self.go(prompt="an owl", answers=["a string"]).ok
        assert not self.go(prompt="").ok
