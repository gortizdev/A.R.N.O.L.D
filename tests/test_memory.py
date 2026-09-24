"""What the assistant keeps between conversations.

The tests that matter are the ones about forgetting and about drift: a fact
said twice must not become two facts, a conversation from yesterday must not
be carried into today as though it just happened, and nothing here may ever
raise into the middle of a live session.
"""

import json
import time

import pytest

from arnold import memory
from arnold.commands import build_registry
from arnold.commands.registry import CommandContext
from arnold.config import Config


@pytest.fixture
def config(tmp_path):
    config = Config()
    config.source_path = tmp_path / "config.yaml"
    config.source_path.write_text("")
    config.memory.file = "memory.json"
    config.memory.conversation_file = "conversations.jsonl"
    return config


@pytest.fixture
def run(config):
    registry = build_registry()
    ctx = CommandContext(config=config, collector=None, alerts=None, jarvis=None)

    def go(command, **args):
        return registry.dispatch(command, args, ctx)

    go.config = config
    go.store = memory.store_for(config)
    return go


class TestFacts:
    def test_a_fact_survives_the_process(self, run):
        assert run("memory.remember", text="The good coffee is in the left cupboard").ok
        assert memory.store_for(run.config).recall("coffee")[0].text.startswith("The good coffee")

    def test_recall_finds_by_word_not_by_prefix(self, run):
        run("memory.remember", text="Ellipse Analytics is where I work")
        found = run("memory.recall", query="where do I work")
        assert "Ellipse Analytics" in found.speech

    def test_recall_of_something_unknown_says_so(self, run):
        run("memory.remember", text="the dog is called Bandit")
        result = run("memory.recall", query="my sister's birthday")
        assert result.ok
        assert result.result["facts"] == []
        assert "birthday" in result.speech

    def test_the_same_thing_twice_is_one_fact(self, run):
        run("memory.remember", text="I'm allergic to walnuts")
        run("memory.remember", text="I am allergic to walnuts.")
        assert run("memory.list").result["count"] == 1

    def test_a_rephrasing_that_adds_meaning_is_a_new_fact(self, run):
        run("memory.remember", text="I'm allergic to walnuts")
        run("memory.remember", text="I'm allergic to walnuts and pecans")
        assert run("memory.list").result["count"] == 2

    def test_forget_takes_the_best_match(self, run):
        run("memory.remember", text="the dog is called Bandit")
        run("memory.remember", text="the car is a blue Volvo")
        assert run("memory.forget", query="the dog").ok
        remaining = [f["text"] for f in run("memory.list").result["facts"]]
        assert remaining == ["the car is a blue Volvo"]

    def test_forget_something_never_stored(self, run):
        run("memory.remember", text="the dog is called Bandit")
        assert not run("memory.forget", query="the boat").ok
        assert run("memory.list").result["count"] == 1

    def test_forget_everything(self, run):
        run("memory.remember", text="one thing")
        run("memory.remember", text="another thing entirely")
        assert run("memory.forget", query="everything").ok
        assert run("memory.list").result["facts"] == []

    def test_forget_by_id(self, run):
        made = run("memory.remember", text="the dog is called Bandit")
        assert run("memory.forget", id=made.result["remembered"]["id"]).ok
        assert run("memory.list").result["facts"] == []

    def test_nothing_to_remember_is_refused(self, run):
        assert not run("memory.remember", text="   ").ok

    def test_a_monologue_is_refused_rather_than_stored(self, run):
        assert not run("memory.remember", text="x " * 300).ok

    def test_tags_are_searchable(self, run):
        run("memory.remember", text="the spare key is under the third pot", tags="house, keys")
        assert run("memory.recall", query="keys").result["facts"]

    def test_the_store_is_capped(self, config):
        config.memory.max_facts = 3
        store = memory.store_for(config)
        for i in range(6):
            store.remember(f"fact number {i}")
        assert store.count() == 3
        assert "5" in store.facts()[0].text  # the newest survived

    def test_a_corrupt_file_is_not_fatal(self, run):
        memory.resolve(run.config, run.config.memory.file).write_text("{not json")
        assert run("memory.list").ok
        assert run("memory.remember", text="starting over").ok

    def test_memory_can_be_switched_off(self, run):
        run.config.memory.enabled = False
        assert not run("memory.remember", text="anything").ok


class TestConversations:
    def test_the_last_conversation_comes_back(self, config):
        log = memory.conversations_for(config)
        log.append([{"who": "user", "text": "put the kettle on"}, {"who": "assistant", "text": "done"}])
        assert log.last()["turns"][0]["text"] == "put the kettle on"

    def test_an_old_conversation_is_not_carried(self, config):
        log = memory.conversations_for(config)
        log.append([{"who": "user", "text": "yesterday"}])
        # Rewrite the timestamp rather than wait a day.
        path = memory.resolve(config, config.memory.conversation_file)
        record = json.loads(path.read_text().strip())
        record["ended"] = time.time() - 86400
        path.write_text(json.dumps(record) + "\n")

        assert log.last(max_age_seconds=3600) is None
        assert log.last() is not None  # still on disk, just not fresh

    def test_empty_turns_are_not_recorded(self, config):
        log = memory.conversations_for(config)
        log.append([{"who": "user", "text": "   "}])
        assert log.last() is None

    def test_the_log_is_trimmed(self, config):
        config.memory.max_conversations = 5
        log = memory.conversations_for(config)
        for i in range(12):
            log.append([{"who": "user", "text": f"exchange {i}"}])
        assert len(log.all()) == 5
        assert log.last()["turns"][0]["text"] == "exchange 11"


class TestInstructionBlock:
    def test_facts_and_the_last_conversation_are_carried_in(self, config):
        memory.store_for(config).remember("the dog is called Bandit")
        memory.conversations_for(config).append(
            [{"who": "user", "text": "open youtube"}, {"who": "assistant", "text": "Opening it."}]
        )
        block = memory.instruction_block(config)
        assert "Bandit" in block
        assert "open youtube" in block
        assert "the user: open youtube" in block and "you: Opening it." in block
        assert "memory.remember" in block  # it is told how to add to it

    def test_nothing_remembered_means_nothing_added(self, config):
        assert memory.instruction_block(config) == ""

    def test_disabled_means_nothing_added(self, config):
        memory.store_for(config).remember("the dog is called Bandit")
        config.memory.enabled = False
        assert memory.instruction_block(config) == ""

    def test_only_the_configured_number_of_facts_go_in(self, config):
        config.memory.inject_facts = 2
        store = memory.store_for(config)
        for i in range(5):
            store.remember(f"fact number {i}")
        block = memory.instruction_block(config)
        assert block.count("- fact number") == 2

    def test_a_stale_conversation_is_left_out(self, config):
        memory.store_for(config).remember("the dog is called Bandit")
        config.memory.carry_max_age_minutes = 0
        memory.conversations_for(config).append([{"who": "user", "text": "open youtube"}])
        assert "open youtube" not in memory.instruction_block(config)


class TestSpokenIntents:
    """The local brain has to catch these without a round trip to the Pi."""

    @pytest.fixture
    def brain(self, config):
        from arnold.voice.brain import LocalBrain

        return LocalBrain(CommandContext(config=config, collector=None, alerts=None, jarvis=None))

    def test_remember_that(self, brain):
        command, args = brain.match("remember that the bins go out on Tuesday")
        assert command == "memory.remember"
        assert args["text"] == "the bins go out on Tuesday"

    def test_capitals_are_kept(self, brain):
        _, args = brain.match("remember that Bandit is the dog")
        assert args["text"] == "Bandit is the dog"

    def test_what_do_you_know_about(self, brain):
        command, args = brain.match("what do you know about the dog?")
        assert command == "memory.recall"
        assert args["query"] == "the dog"

    def test_forget(self, brain):
        command, args = brain.match("forget about the dog")
        assert (command, args["query"]) == ("memory.forget", "the dog")

    def test_a_reminder_is_not_a_fact(self, brain):
        """'remember to' is a timer, which lives on the Pi."""
        assert brain.match("remember to take the bins out") is None

    def test_it_answers_end_to_end(self, brain):
        assert brain.ask("remember that the bins go out on Tuesday")
        assert "Tuesday" in brain.ask("what do you know about the bins")
