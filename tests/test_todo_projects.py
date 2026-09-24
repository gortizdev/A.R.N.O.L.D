"""Groups on the list are the document's projects; this ties them to the
Claude Code projects on the PC, by guess or by hand, and hands tasks over."""

from __future__ import annotations

import pytest

from arnold.commands import CommandContext, build_registry
from arnold.config import Config
from arnold.todos import (
    TodoItem,
    TodoList,
    claude_projects,
    compose_prompt,
    match_project,
)

PROJECTS = [
    {"project": "ROI Calculator Development", "cwd": "C:/Users/geo/Projects/ROI-Calculator Development",
     "live": True, "turn": "idle", "session_id": "s1"},
    {"project": "Ellipse Hub", "cwd": "C:/Users/geo/Projects/Ellipse-Hub", "live": True, "turn": "busy",
     "session_id": "s2"},
    {"project": "Ellipse Data", "cwd": "C:/Users/geo/Projects/Ellipse-Data", "live": False, "turn": "",
     "session_id": "s3"},
    {"project": "Arnold", "cwd": "C:/Users/geo/Projects/ComputerAssistant", "live": True,
     "turn": "waiting", "session_id": "s4"},
]


class TestGuessing:
    def test_obvious_matches(self):
        assert match_project("ROI Calculator", PROJECTS)["project"] == "ROI Calculator Development"
        assert match_project("Ellipse Hub Rollout", PROJECTS)["project"] == "Ellipse Hub"

    def test_one_shared_word_is_not_enough(self):
        assert match_project("Data Migration / Management", PROJECTS) is None
        assert match_project("Certification Evaluation", PROJECTS) is None
        assert match_project("", PROJECTS) is None

    def test_stop_words_do_not_count(self):
        assert match_project("New Tool Evaluation", PROJECTS) is None


class TestCandidates:
    def test_one_per_folder_with_the_liveliest_session(self):
        cfg = Config()
        cfg.code.projects = {"assistant": "C:/Users/geo/Projects/ComputerAssistant", "scratch": "C:/x/scratch"}
        section = {"sessions": [
            {"id": "a", "name": "Old chat", "project": "Ellipse Hub", "cwd": "C:/Users/geo/Projects/Ellipse-Hub",
             "live": False, "turn": "idle"},
            {"id": "b", "name": "Live chat", "project": "Ellipse Hub", "cwd": "C:\\Users\\geo\\Projects\\Ellipse-Hub",
             "live": True, "turn": "busy"},
            {"id": "c", "name": "Me", "project": "Arnold", "cwd": "C:/Users/geo/Projects/ComputerAssistant",
             "live": True, "turn": "idle"},
        ]}
        projects = claude_projects(cfg, section)
        names = [p["project"] for p in projects]
        assert names == ["Ellipse Hub", "Arnold", "scratch"]
        hub = projects[0]
        assert hub["session_id"] == "b" and hub["sessions"] == 2 and hub["turn"] == "busy"
        assert projects[2]["cwd"] == "C:/x/scratch" and not projects[2]["live"]

    def test_nothing_known(self):
        assert claude_projects(Config(), None) == []


class TestLinks:
    def test_explicit_beats_guess_and_nothing_is_nothing(self, tmp_path):
        todos = TodoList(tmp_path / "todos.json")
        todos.add("Fix the totals", section="ROI Calculator")
        todos.add("Ship the migration", section="Data Migration / Management")
        todos.add("Write the runbook", section="Lab Scheduling")

        resolved = todos.resolve_links(PROJECTS)
        assert resolved["ROI Calculator"]["auto"] and resolved["ROI Calculator"]["session_id"] == "s1"
        assert "Data Migration / Management" not in resolved

        todos.link("Data Migration / Management", "Ellipse Data", "C:/Users/geo/Projects/Ellipse-Data")
        resolved = todos.resolve_links(PROJECTS)
        assert resolved["Data Migration / Management"] == {
            "project": "Ellipse Data", "cwd": "C:/Users/geo/Projects/Ellipse-Data", "auto": False,
            "live": False, "turn": "", "session_id": "s3",
        }

        todos.link("ROI Calculator", "", "")  # explicitly nothing
        assert "ROI Calculator" not in todos.resolve_links(PROJECTS)
        assert todos.unlink("ROI Calculator")
        assert todos.resolve_links(PROJECTS)["ROI Calculator"]["auto"]
        assert not todos.unlink("ROI Calculator")
        assert todos.sections() == ["ROI Calculator", "Data Migration / Management", "Lab Scheduling"]

    def test_links_survive_an_import(self, tmp_path):
        todos = TodoList(tmp_path / "todos.json")
        todos.link("Lab Scheduling", "Ellipse Hub", "C:/Users/geo/Projects/Ellipse-Hub")
        assert todos.links()["lab scheduling"]["project"] == "Ellipse Hub"
        assert todos.describe()["links"]["lab scheduling"]["cwd"].endswith("Ellipse-Hub")


class TestPrompt:
    def test_reads_like_a_person(self):
        item = TodoItem(id="x", text="Ryan to Test", section="Bracketing Evaluation", source="geo update")
        assert compose_prompt(item, "geo update") == (
            "From this week's geo update, under Bracketing Evaluation: Ryan to Test. "
            "Please take a look and tell me what you'd do about it."
        )
        manual = TodoItem(id="y", text="Call the dentist", source="manual")
        assert compose_prompt(manual, "geo update").startswith("From my to-do list, on the list: Call the dentist")


class TestCommands:
    def _ctx(self, tmp_path) -> CommandContext:
        cfg = Config()
        cfg.state_file = str(tmp_path / "state.json")
        cfg.todo.watch_folders = [str(tmp_path / "inbox")]
        cfg.code.projects = {"roi": "C:/Users/geo/Projects/ROI-Calculator"}
        return CommandContext(config=cfg, collector=None, alerts=None)  # type: ignore[arg-type]

    def test_projects_link_and_send(self, tmp_path):
        ctx = self._ctx(tmp_path)
        registry = build_registry()
        registry.dispatch("todo.add", {"text": "Fix the totals", "section": "ROI Calculator"}, ctx)
        registry.dispatch("todo.add", {"text": "Book the room", "section": "Lab Scheduling"}, ctx)

        listed = registry.dispatch("todo.projects", {}, ctx)
        assert listed.ok and "ROI Calculator is roi (a guess)" in listed.speech
        assert "Lab Scheduling has no project" in listed.speech

        assert not registry.dispatch("todo.link", {"section": "Nowhere", "project": "roi"}, ctx).ok
        assert not registry.dispatch("todo.link", {"section": "Lab", "project": "unknown thing"}, ctx).ok
        linked = registry.dispatch("todo.link", {"section": "lab", "project": "roi"}, ctx)
        assert linked.ok and linked.speech == "Lab Scheduling is now roi."
        again = registry.dispatch("todo.projects", {}, ctx)
        assert "Lab Scheduling is roi." in again.speech or "Lab Scheduling is roi;" in again.speech

        untied = registry.dispatch("todo.link", {"section": "Lab Scheduling", "project": "none"}, ctx)
        assert untied.ok and "no longer" in untied.speech
        assert "Lab Scheduling has no project" in registry.dispatch("todo.projects", {}, ctx).speech

        # Sending needs a project and, with no sessions watched, says so.
        no_project = registry.dispatch("todo.send", {"which": "room"}, ctx)
        assert not no_project.ok and "isn't tied" in no_project.error
        no_session = registry.dispatch("todo.send", {"which": "totals"}, ctx)
        assert not no_session.ok
        assert "switched off" in no_session.error or "no Claude Code sessions" in no_session.error
