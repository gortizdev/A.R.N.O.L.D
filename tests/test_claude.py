"""Watching Claude Code sessions, and speaking into one.

The transcripts are real files with a known shape, so the tests write small
ones and check what the watch makes of them: which turn is open, what it is
doing, what was said, and which events fall out as the file grows. The pipe
and the process list are faked; what is tested is what gets decided.
"""

import json
import time
from pathlib import Path

import pytest

from arnold import claude_hooks, claude_link
from arnold.commands import build_registry
from arnold.commands.registry import CommandContext
from arnold.config import ClaudeConfig, Config
from arnold.monitors.claude import (
    ClaudeWatch,
    ListeningApp,
    Session,
    attribute_apps,
    match_session,
    project_name,
    tool_summary,
)

SID = "11111111-2222-3333-4444-555555555555"


def stamp(offset=0.0):
    from datetime import datetime, timezone

    return datetime.fromtimestamp(time.time() + offset, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def user(text, offset=0.0, origin="human", **extra):
    row = {
        "type": "user", "sessionId": SID, "timestamp": stamp(offset), "cwd": "C:\\work\\Ellipse-Hub",
        "entrypoint": "claude-desktop", "gitBranch": "main", "promptId": "p1",
        "permissionMode": "bypassPermissions",
        "message": {"role": "user", "content": text},
    }
    if origin:
        row["origin"] = {"kind": origin}
    row.update(extra)
    return row


def tool_result(tool_id, text="ok", offset=0.0):
    return {
        "type": "user", "sessionId": SID, "timestamp": stamp(offset), "cwd": "C:\\work\\Ellipse-Hub",
        "entrypoint": "claude-desktop",
        "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": text}]},
    }


def assistant(blocks, stop, msg="msg_1", offset=0.0):
    return {
        "type": "assistant", "sessionId": SID, "timestamp": stamp(offset), "cwd": "C:\\work\\Ellipse-Hub",
        "entrypoint": "claude-desktop",
        "message": {"id": msg, "model": "claude-opus-5", "role": "assistant", "content": blocks, "stop_reason": stop},
    }


def tool_use(tool_id, name, inputs):
    return {"type": "tool_use", "id": tool_id, "name": name, "input": inputs}


def text(value):
    return {"type": "text", "text": value}


class Home:
    """A ~/.claude with one project folder and a registry."""

    def __init__(self, root: Path):
        self.root = root
        self.project = root / "projects" / "C--work-Ellipse-Hub"
        self.project.mkdir(parents=True)
        (root / "sessions").mkdir()
        self.transcript = self.project / f"{SID}.jsonl"

    def write(self, *rows):
        with open(self.transcript, "a", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")

    def register(self, pid=4242, status="busy", socket=r"\\.\pipe\LOCAL\cc-msg-" + "ab" * 16, token="cd" * 16):
        (self.root / "sessions" / f"{pid}.json").write_text(json.dumps({
            "pid": pid, "sessionId": SID, "cwd": "C:\\work\\Ellipse-Hub", "status": status,
            "entrypoint": "claude-desktop", "kind": "interactive", "name": "ellipse-hub-7d",
            "messagingSocketPath": socket,
        }))
        (self.root / "sessions" / f"{pid}.{'e' * 64}.key").write_text(json.dumps({"peerToken": token}))


@pytest.fixture
def home(tmp_path):
    return Home(tmp_path / "claude")


def watch(home, **overrides):
    cfg = ClaudeConfig(enabled=True, poll_seconds=1, scan_seconds=2, apps_seconds=3, **overrides)
    return ClaudeWatch(
        cfg, home=home.root, events_file=home.root / "events.jsonl",
        apps_reader=lambda: [], prober=lambda port, host="127.0.0.1": ("unknown", 0, ""),
        registry_reader=lambda: [], desktop_titles=lambda: {},
    )


# -- reading a transcript ---------------------------------------------------


class TestTurns:
    def test_a_prompt_opens_a_turn_and_a_tool_call_is_the_activity(self, home):
        home.write(
            {"type": "custom-title", "customTitle": "Ellipse-Hub Development", "sessionId": SID},
            user("run the tests", -30),
            assistant([tool_use("t1", "Bash", {"command": "pytest", "description": "Run the tests"})], "tool_use", offset=-25),
        )
        w = watch(home)
        w.poll()
        [s] = w.listed()
        assert s.title == "Ellipse-Hub Development"
        assert s.turn == "busy"
        assert s.activity == "Run the tests"
        assert s.activity_tool == "Bash"
        assert s.last_prompt == "run the tests"
        assert s.where == "Desktop"
        assert s.current.tool_calls == 1

    def test_the_turn_closes_when_the_reply_is_complete(self, home):
        home.write(user("run the tests", -60), assistant([tool_use("t1", "Bash", {"command": "pytest"})], "tool_use", offset=-55))
        w = watch(home)
        w.poll()
        w.drain_new()
        home.write(
            tool_result("t1", "3 passed", -50),
            # The reply lands as two rows of the same message, both stamped end_turn.
            assistant([{"type": "thinking", "thinking": ""}], "end_turn", msg="msg_2", offset=-40),
        )
        w.poll()
        [s] = w.listed()
        assert s.turn == "idle"
        assert s.current.finished == 0.0, "not closed on the first block"
        home.write(assistant([text("All three tests pass.")], "end_turn", msg="msg_2", offset=-38))
        w.poll()
        assert w.drain_new() == []  # still within the grace period
        s._closing_wall -= 10
        w.poll()
        events = w.drain_new()
        assert [e["kind"] for e in events] == ["turn_done"]
        assert events[0]["reply"] == "All three tests pass."
        assert events[0]["name"] == "Ellipse Hub"
        assert 15 < events[0]["seconds"] < 30
        assert s.last_reply == "All three tests pass."
        assert s.turns[-1].prompt == "run the tests"

    def test_a_new_prompt_closes_the_previous_reply_at_once(self, home):
        home.write(user("first", -60), assistant([text("done one")], "end_turn", offset=-50))
        w = watch(home)
        w.poll()
        w.drain_new()
        home.write(user("second", -10))
        w.poll()
        kinds = [e["kind"] for e in w.drain_new()]
        assert kinds == ["turn_started"]  # the first turn finished before we watched
        [s] = w.listed()
        assert s.turn == "busy" and s.last_prompt == "second"
        assert s.turns[-1].reply == "done one"

    def test_a_question_to_the_user_is_waiting(self, home):
        home.write(user("ship it", -30), assistant([tool_use("q1", "AskUserQuestion", {"questions": [{"question": "Which branch?"}]})], "tool_use", offset=-20))
        w = watch(home)
        w.poll()
        [s] = w.listed()
        assert s.turn == "waiting"
        assert "Which branch?" in s.waiting_for
        home.write(tool_result("q1", "main", -5))
        w.poll()
        assert s.turn == "busy"

    def test_seeding_reports_nothing_that_already_happened(self, home):
        home.write(user("do a thing", -100), assistant([text("did it")], "end_turn", offset=-90))
        w = watch(home)
        w.poll()
        [s] = w.listed()
        s._closing_wall -= 10
        w.poll()
        assert w.drain_new() == []

    def test_system_reminders_and_meta_rows_are_not_prompts(self, home):
        home.write(
            user("real ask", -30),
            user("<system-reminder>\nsomething\n</system-reminder>", -20, origin=None),
            user("<command-name>/clear</command-name>", -19, origin=None, isMeta=True),
        )
        w = watch(home)
        w.poll()
        [s] = w.listed()
        assert s.last_prompt == "real ask"

    def test_a_task_notification_wakes_the_session_without_a_prompt(self, home):
        home.write(user("go", -60), assistant([text("ok")], "end_turn", offset=-50), user("agent finished", -10, origin="task-notification"))
        w = watch(home)
        w.poll()
        [s] = w.listed()
        assert s.turn == "busy"
        assert s.last_prompt == "go"
        assert s.current.prompt == "[task-notification]"

    def test_last_prompt_rows_fill_in_a_prompt_read_past(self, home):
        home.write({"type": "last-prompt", "lastPrompt": "the opening ask", "sessionId": SID})
        w = watch(home)
        w.poll()
        [s] = w.listed()
        assert s.last_prompt == "the opening ask"

    def test_only_the_tail_of_a_long_transcript_is_read(self, home):
        rows = [user("prompt 0", -1000)] + [tool_result("t%d" % i, "x" * 40, -900 + i) for i in range(60)]
        home.write(*rows)
        w = watch(home)
        w.SEED_BYTES = 600
        w.poll()
        [s] = w.listed()
        assert s._offset == home.transcript.stat().st_size
        # The tail never saw a prompt row, but the head did: the permission
        # mode - which only prompt rows carry - is still known.
        assert s.permission_mode == "bypassPermissions"
        assert s.mode_class == "bypass"
        assert Session(id="x", path=Path("x")).mode_class == ""

    def test_a_half_written_line_waits_for_the_rest(self, home):
        home.write(user("one", -30))
        w = watch(home)
        w.poll()
        with open(home.transcript, "a", encoding="utf-8") as fh:
            fh.write('{"type":"user","message":{"role":"user","content":"tw')
        w.poll()
        [s] = w.listed()
        assert s.last_prompt == "one"
        with open(home.transcript, "a", encoding="utf-8") as fh:
            fh.write('o"},"timestamp":"%s","origin":{"kind":"human"}}\n' % stamp(-5))
        w.poll()
        assert s.last_prompt == "two"

    def test_old_transcripts_are_not_sessions(self, home, tmp_path):
        import os

        home.write(user("ancient", -30))
        old = time.time() - 10 * 3600
        os.utime(home.transcript, (old, old))
        w = watch(home, recent_minutes=60)
        w.poll()
        assert w.listed() == []


class TestRegistry:
    def test_a_registered_session_is_live_and_messageable(self, home):
        home.write(user("go", -30), assistant([text("ok")], "end_turn", offset=-20))
        home.register(status="busy")
        from arnold.monitors.claude import read_registry

        w = ClaudeWatch(
            ClaudeConfig(enabled=True), home=home.root, events_file=home.root / "events.jsonl", apps_reader=lambda: [],
            registry_reader=lambda: read_registry(home.root, alive=lambda pid: pid == 4242),
            desktop_titles=lambda: {}, prober=lambda port, host="": ("unknown", 0, ""),
        )
        w.poll()
        [s] = w.listed()
        assert s.live and s.pid == 4242 and s.socket.startswith("\\\\.\\pipe")
        assert s.to_dict()["can_message"]
        # The process says busy, the transcript said idle a while ago: busy.
        assert s.turn == "busy"

    def test_a_dead_pid_is_not_live(self, home):
        home.write(user("go", -30))
        home.register(pid=99999)
        from arnold.monitors.claude import read_registry

        assert read_registry(home.root, alive=lambda pid: False) == []

    def test_desktop_titles_fill_in_where_the_transcript_has_none(self, home):
        home.write(user("go", -30))
        w = watch(home)
        w._desktop_titles = lambda: {SID: "Ellipse-Hub Development"}
        w.poll()
        [s] = w.listed()
        assert s.name == "Ellipse-Hub Development"

    def test_a_transcript_the_desktop_moved_on_from_is_hidden(self, home):
        """The desktop forks a new CLI session per turn; one conversation, one entry."""
        newer = "22222222-2222-3333-4444-555555555555"
        home.write(user("old turn", -300), assistant([text("done")], "end_turn", offset=-290))
        home.transcript = home.project / f"{newer}.jsonl"
        home.write(user("new turn", -30))
        w = watch(home)
        w._desktop_titles = lambda: {
            SID: {"title": "Ellipse-Hub Development", "desktop": "local_1", "current": newer, "superseded": True},
            newer: {"title": "Ellipse-Hub Development", "desktop": "local_1", "current": newer, "superseded": False},
        }
        w.poll()
        [s] = w.listed()
        assert s.id == newer and s.desktop_id == "local_1"
        assert w.get(SID).superseded

    def test_desktop_records_are_read_from_the_app_folder(self, tmp_path):
        from arnold.monitors.claude import read_desktop_records

        folder = tmp_path / "claude-code-sessions" / "acct" / "org"
        folder.mkdir(parents=True)
        (folder / "local_abc.json").write_text(json.dumps({
            "sessionId": "local_abc", "title": "Hub", "cliSessionId": "new-id", "priorCliSessionIds": ["old-1", "old-2"],
        }))
        records = read_desktop_records(tmp_path / "claude-code-sessions")
        assert records["new-id"] == {"title": "Hub", "desktop": "local_abc", "current": "new-id", "superseded": False}
        assert records["old-1"]["superseded"] and records["old-2"]["superseded"]


class TestHooks:
    def test_a_permission_prompt_makes_the_session_waiting(self, home, tmp_path):
        home.write(user("push it", -30), assistant([tool_use("t1", "Bash", {"command": "git push"})], "tool_use", offset=-20))
        events = tmp_path / "events.jsonl"
        w = watch(home)
        w.events_file = events
        w.poll()
        w.drain_new()
        events.write_text(json.dumps({
            "ts": time.time(), "event": "Notification", "session_id": SID, "transcript_path": str(home.transcript),
            "notification_type": "permission_prompt", "message": "Claude needs your permission to use Bash",
        }) + "\n")
        w.poll()
        [s] = w.listed()
        assert s.turn == "waiting"
        assert "permission" in s.waiting_for
        [event] = w.drain_new()
        assert event["kind"] == "needs_input" and event["what"] == "permission"
        # Granted: the tool result arrives and the session moves on.
        home.write(tool_result("t1", "pushed", -2))
        w.poll()
        assert s.turn == "busy"

    def test_old_hook_events_are_not_announced_again_at_start(self, home, tmp_path):
        # The hook file keeps days of history; a restart must not replay a
        # permission prompt from last week as if it were on screen now.
        home.write(user("push it", -30), assistant([tool_use("t1", "Bash", {"command": "git push"})], "tool_use", offset=-20))
        events = tmp_path / "events.jsonl"
        events.write_text(json.dumps({
            "ts": time.time() - 3 * 86400, "event": "Notification", "session_id": SID,
            "transcript_path": str(home.transcript),
            "notification_type": "permission_prompt", "message": "Claude needs your permission to use Bash",
        }) + "\n")
        w = watch(home)
        w.events_file = events
        w.poll()
        assert not [e for e in w.drain_new() if e["kind"] == "needs_input"]

    def test_the_hook_script_appends_one_line(self, tmp_path, monkeypatch, capsys):
        import io

        from arnold import claude_hook

        target = tmp_path / "ev.jsonl"
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({
            "hook_event_name": "Stop", "session_id": SID, "transcript_path": "x", "prompt": "secret words",
        })))
        assert claude_hook.main([str(target)]) == 0
        [line] = target.read_text().splitlines()
        row = json.loads(line)
        assert row["event"] == "Stop" and row["session_id"] == SID
        assert "prompt" not in row

    def test_install_and_remove_leave_other_hooks_alone(self, tmp_path):
        settings = tmp_path / "settings.json"
        settings.write_text(json.dumps({
            "model": "x",
            "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo mine"}]}]},
        }))
        out = claude_hooks.install(tmp_path / "ev.jsonl", path=settings, python="C:\\py\\python.exe")
        assert out["changed"]
        data = json.loads(settings.read_text())
        assert data["model"] == "x"
        assert len(data["hooks"]["Stop"]) == 2
        assert data["hooks"]["Stop"][0]["hooks"][0]["command"] == "echo mine"
        assert claude_hooks.MARKER in data["hooks"]["Stop"][1]["hooks"][0]["command"]
        assert set(claude_hooks.EVENTS) <= set(data["hooks"])
        assert claude_hooks.status(settings)["installed"]
        # Twice is idempotent.
        assert not claude_hooks.install(tmp_path / "ev.jsonl", path=settings, python="C:\\py\\python.exe")["changed"]
        assert len(json.loads(settings.read_text())["hooks"]["Stop"]) == 2

        out = claude_hooks.remove(path=settings)
        data = json.loads(settings.read_text())
        assert data["hooks"] == {"Stop": [{"hooks": [{"type": "command", "command": "echo mine"}]}]}
        assert not claude_hooks.status(settings)["installed"]
        assert any(p.name.endswith(".bak") for p in tmp_path.iterdir())


# -- dev servers --------------------------------------------------------------


class TestApps:
    def test_listeners_are_matched_to_sessions_by_folder(self):
        hub = Session(id="a", path=Path("a.jsonl"), cwd="C:\\work\\Ellipse-Hub")
        roi = Session(id="b", path=Path("b.jsonl"), cwd="C:\\work\\ROI Calculator")
        apps = [
            ListeningApp(5173, 1, "node.exe", "C:\\work\\Ellipse-Hub\\frontend", "vite", True),
            ListeningApp(5174, 2, "node.exe", "C:\\work\\ROI Calculator", "vite", True),
            ListeningApp(3000, 3, "node.exe", "", "node server.mjs", True),
            ListeningApp(11434, 4, "ollama.exe", "C:\\elsewhere", "ollama", False),
        ]
        hub.note_ports("Local: http://localhost:3000/")
        owned = attribute_apps(apps, [hub, roi])
        assert [a.port for a in owned["a"]] == [3000, 5173]
        assert [a.port for a in owned["b"]] == [5174]
        assert "" not in owned  # ollama is neither hosted nor mentioned

    def test_the_most_recent_session_in_a_folder_gets_the_app(self):
        old = Session(id="old", path=Path("o.jsonl"), cwd="C:\\work\\Ellipse-Hub", last_event_at=100)
        new = Session(id="new", path=Path("n.jsonl"), cwd="C:\\work\\Ellipse-Hub", last_event_at=200)
        apps = [ListeningApp(5173, 1, "node.exe", "C:\\work\\Ellipse-Hub", "vite", True)]
        assert list(attribute_apps(apps, [old, new])) == ["new"]

    def test_apps_appear_in_the_snapshot_and_as_events(self, home):
        home.write(user("serve", -30))
        apps = [ListeningApp(5173, 1, "node.exe", "C:\\work\\Ellipse-Hub", "vite", True)]
        w = ClaudeWatch(
            ClaudeConfig(enabled=True, apps_seconds=3), home=home.root, events_file=home.root / "events.jsonl", apps_reader=lambda: apps,
            prober=lambda port, host="127.0.0.1": ("up", 200, "Vite App"),
            registry_reader=lambda: [], desktop_titles=lambda: {},
        )
        w.poll()
        section = w.snapshot()["claude_sessions"]
        [row] = section["sessions"]
        assert row["apps"][0]["url"] == "http://localhost:5173/"
        assert row["apps"][0]["status"] == "up" and row["apps"][0]["title"] == "Vite App"
        # Already up when the watch started: shown, not announced.
        assert [e for e in w.drain_new() if e["kind"] == "app_up"] == []
        apps.append(ListeningApp(3000, 2, "node.exe", "C:\\work\\Ellipse-Hub", "node server", True))
        w._apps_at = 0
        w.poll()
        [event] = [e for e in w.drain_new() if e["kind"] == "app_up"]
        assert event["port"] == 3000 and event["name"] == "Ellipse Hub"
        apps.clear()
        w._apps_at = 0
        w.poll()
        assert sorted(e["kind"] for e in w.drain_new()) == ["app_down", "app_down"]


# -- naming -------------------------------------------------------------------


class TestNaming:
    def sessions(self):
        return [
            Session(id="1", path=Path("1"), cwd="C:\\p\\Ellipse-Hub", title="Ellipse-Hub Development"),
            Session(id="2", path=Path("2"), cwd="C:\\p\\Ellipse-Data", title="Web UI for LIMS server"),
            Session(id="3", path=Path("3"), cwd="C:\\p\\ROI Certification Calculator", title="ROI-Calculator Development"),
        ]

    def test_words_pick_a_session(self):
        s = self.sessions()
        assert match_session("ellipse hub", s).id == "1"
        assert match_session("the ellipse data one", s).id == "2"
        assert match_session("roi", s).id == "3"
        assert match_session("lims", s).id == "2"
        assert match_session("the calculator session", s).id == "3"
        assert match_session("banana", s) is None

    def test_nothing_named_means_the_obvious_one(self):
        s = self.sessions()
        assert match_session("", s) is None
        s[1].turn = "busy"
        assert match_session("", s).id == "2"
        assert match_session("the session", s).id == "2"
        assert match_session("", s[:1]).id == "1"

    def test_project_names_and_tool_lines(self):
        assert project_name("C:\\p\\Ellipse-Hub") == "Ellipse Hub"
        assert project_name("C:\\p\\ROI Certification Calculator") == "ROI Certification Calculator"
        assert tool_summary("Bash", {"command": "pytest -q", "description": "Run tests"}) == "Run tests"
        assert tool_summary("Edit", {"file_path": "C:\\a\\b\\server.py"}) == "edit server.py"
        assert tool_summary("mcp__Claude_Browser__browser_batch", {}) == "browser batch"


# -- the commands -------------------------------------------------------------


@pytest.fixture
def run(home, monkeypatch):
    config = Config()
    config.claude = ClaudeConfig(enabled=True)
    config.claude.home = str(home.root)
    config.state_file = str(home.root / "state.json")

    class Coll:
        def __init__(self):
            self.claude = ClaudeWatch(
                config.claude, home=home.root, events_file=home.root / "events.jsonl",
                apps_reader=lambda: [], registry_reader=lambda: [],
                desktop_titles=lambda: {}, prober=lambda port, host="": ("unknown", 0, ""),
            )

    registry = build_registry()
    ctx = CommandContext(config=config, collector=Coll(), alerts=None, jarvis=None)

    def go(command, **args):
        return registry.dispatch(command, args, ctx)

    go.ctx = ctx
    go.watch = ctx.collector.claude
    return go


class TestCommands:
    def test_list_and_status_speak_the_state(self, home, run):
        home.write(
            {"type": "custom-title", "customTitle": "Ellipse-Hub Development", "sessionId": SID},
            user("run the tests", -90),
            assistant([tool_use("t1", "Bash", {"command": "pytest", "description": "Run the tests"})], "tool_use", offset=-80),
        )
        result = run("claude.list")
        assert result.ok
        assert "1 Claude Code session" in result.speech
        assert "Ellipse-Hub Development in Desktop is on Run the tests" in result.speech
        status = run("claude.status", which="ellipse")
        assert status.ok
        assert "is on Run the tests" in status.speech
        assert "run the tests" in status.speech

    def test_nothing_open(self, run):
        result = run("claude.list")
        assert result.ok and "No Claude Code sessions" in result.speech
        assert not run("claude.status", which="ellipse").ok

    def test_prompt_goes_down_the_pipe_when_live(self, home, run, monkeypatch):
        home.write(user("go", -30), assistant([text("ok")], "end_turn", offset=-20))
        home.register(status="idle")
        from arnold.monitors.claude import read_registry

        run.watch._registry_reader = lambda: read_registry(home.root, alive=lambda pid: True)
        sent = {}

        def fake_send(path, token, text_, **kw):
            sent.update(path=path, token=token, text=text_)
            return {"delivered": True, "via": "pipe"}

        monkeypatch.setattr(claude_link, "send_over_pipe", fake_send)
        result = run("claude.prompt", which="ellipse hub", text="run the tests")
        assert result.ok, result.error
        assert sent["token"] == "cd" * 16
        assert sent["path"].endswith("cc-msg-" + "ab" * 16)
        # Wrapped as a peer message, attesting the session's own permission
        # class (the transcript said bypassPermissions) so it is delivered
        # rather than held for a review nobody is there to give.
        assert sent["text"] == (
            '<cross-session-message from="arnold" from-name="Arnold" from-mode="bypass">\n'
            "run the tests\n</cross-session-message>"
        )
        assert "Told the" in result.speech

    def test_prompt_falls_back_to_the_cli_when_not_running(self, home, run, monkeypatch):
        home.write(user("go", -30), assistant([text("ok")], "end_turn", offset=-20))
        started = {}
        monkeypatch.setattr(claude_link, "find_cli", lambda configured="": "C:\\claude.exe")

        def fake_resume(cli, session_id, cwd, text_, **kw):
            started.update(cli=cli, session_id=session_id, cwd=cwd, text=text_)
            return {"delivered": True, "via": "resume", "pid": 1}

        monkeypatch.setattr(claude_link, "resume_headless", fake_resume)
        run.ctx.config.claude.prompt_prefix = "(via Mycroft)"
        result = run("claude.prompt", which="ellipse", text="run the tests")
        assert result.ok, result.error
        assert started["session_id"] == SID and started["cwd"] == "C:\\work\\Ellipse-Hub"
        assert started["text"] == "(via Mycroft) run the tests"
        assert "isn't running" in result.speech

    def test_prompt_refuses_when_the_fallback_is_off(self, home, run):
        home.write(user("go", -30), assistant([text("ok")], "end_turn", offset=-20))
        run.ctx.config.claude.resume_fallback = False
        result = run("claude.prompt", which="ellipse", text="run the tests")
        assert not result.ok and "isn't running" in result.error

    def test_a_desktop_conversation_is_never_resumed_behind_the_apps_back(self, home, run, monkeypatch):
        """The app would fork a fresh transcript next turn and never show ours."""
        home.write(user("go", -30), assistant([text("ok")], "end_turn", offset=-20))
        run.watch._desktop_titles = lambda: {
            SID: {"title": "Ellipse-Hub Development", "desktop": "local_1", "current": SID, "superseded": False},
        }
        monkeypatch.setattr(claude_link, "resume_headless", lambda *a, **k: pytest.fail("resumed anyway"))
        result = run("claude.prompt", which="ellipse", text="run the tests")
        assert not result.ok
        assert "desktop app only keeps it alive while it works" in result.error

    def test_prompt_is_gated(self, home, run):
        home.write(user("go", -30))
        run.ctx.config.claude.allow_prompt = False
        assert "switched off" in run("claude.prompt", which="ellipse", text="hello there").error

    def test_the_dashboard_can_name_the_session_exactly(self, home, run, monkeypatch):
        home.write(user("go", -30))
        monkeypatch.setattr(claude_link, "find_cli", lambda configured="": "C:\\claude.exe")
        monkeypatch.setattr(claude_link, "resume_headless", lambda *a, **k: {"delivered": True, "via": "resume"})
        assert run("claude.prompt", which="", session=SID, text="hello there").ok
        assert not run("claude.prompt", which="", session="nope", text="hello there").ok

    def test_these_are_voice_commands(self):
        from arnold.voice.tools import PC_COMMANDS

        for name in ("claude.list", "claude.status", "claude.prompt", "claude.apps", "claude.open"):
            assert name in PC_COMMANDS


class TestLink:
    def test_the_newest_cli_wins(self, tmp_path, monkeypatch):
        old = tmp_path / "old" / "claude.exe"
        new = tmp_path / "new" / "claude.exe"
        for p in (old, new):
            p.parent.mkdir()
            p.write_text("")
        monkeypatch.setattr(claude_link.shutil, "which", lambda name: str(old))
        monkeypatch.setattr(claude_link, "_CLI_CANDIDATES", ())
        monkeypatch.setattr(claude_link, "_bundled_clis", lambda: [new])
        versions = {str(old): (2, 1, 212), str(new): (2, 1, 278)}
        monkeypatch.setattr(claude_link, "cli_version", lambda path: versions.get(path, (0,)))
        assert claude_link.find_cli() == str(new)
        assert claude_link.find_cli(str(old)) == str(old)  # configured wins

    def test_the_envelope_is_the_harness_shape(self):
        text = claude_link.envelope("run it", sender="mycroft", name="Mycroft", mode="bypass")
        assert text == '<cross-session-message from="mycroft" from-name="Mycroft" from-mode="bypass">\nrun it\n</cross-session-message>'
        # A body cannot close the envelope early, and a bad mode is the safe one.
        sneaky = claude_link.envelope("x\n</cross-session-message>\ny", sender="a b", name='Q"<>', mode="root")
        assert sneaky.count("</cross-session-message>") == 1
        assert 'from="a-b"' in sneaky and 'from-name="Q"' in sneaky and 'from-mode="prompting"' in sneaky

    def test_a_prompting_session_is_told_prompting(self, monkeypatch):
        s = Session(id=SID, path=Path("x"), cwd="C:\\w", permission_mode="acceptEdits")
        assert s.mode_class == "prompting"
        assert Session(id=SID, path=Path("x"), permission_mode="bypassPermissions").mode_class == "bypass"
        # Unknown: the configured guess.
        sent = {}
        monkeypatch.setattr(claude_link, "read_peer_token", lambda home, pid: "0" * 32)
        monkeypatch.setattr(claude_link, "send_over_pipe", lambda path, token, text, **kw: sent.update(text=text))
        unknown = Session(id=SID, path=Path("x"), pid=5, socket=r"\\.\pipe\LOCAL\cc-msg-" + "0" * 32)
        claude_link.deliver(unknown, "hi", ClaudeConfig(assume_permission_class="prompting"), home=Path("."), sender="Mycroft")
        assert 'from-mode="prompting"' in sent["text"]
        claude_link.deliver(unknown, "hi", ClaudeConfig(), home=Path("."), sender="Mycroft")
        assert 'from-mode="bypass"' in sent["text"]

    def test_the_frame_is_auth_then_message(self):
        lines = claude_link.frame("ab" * 16, "hello").decode().splitlines()
        assert json.loads(lines[0]) == {"type": "auth", "token": "ab" * 16}
        assert json.loads(lines[1]) == {"type": "user", "message": {"role": "user", "content": "hello"}}
        assert claude_link.frame("", "hi").decode().count("\n") == 1

    def test_the_peer_token_is_read_for_the_pid(self, home):
        home.register(pid=77, token="ef" * 16)
        assert claude_link.read_peer_token(home.root, 77) == "ef" * 16
        assert claude_link.read_peer_token(home.root, 78) == ""

    def test_a_missing_pipe_is_a_spoken_error(self, tmp_path):
        with pytest.raises(claude_link.LinkError):
            claude_link.send_over_pipe(r"\\.\pipe\LOCAL\cc-msg-" + "0" * 32, "0" * 32, "hi")

    def test_a_live_session_without_an_inbox_is_refused(self):
        s = Session(id=SID, path=Path("x"), cwd="C:\\w", pid=5, socket="")
        with pytest.raises(claude_link.LinkError, match="not taking messages"):
            claude_link.deliver(s, "hi", ClaudeConfig(), home=Path("."))


class TestSpokenReply:
    """Replies are read aloud, so markdown has to become sentences."""

    def speak(self, text, limit=300):
        from arnold.monitors.claude import _excerpt, _strip_markup

        return _excerpt(_strip_markup(text), limit)

    def test_a_link_whose_path_has_brackets_is_removed_whole(self):
        said = self.speak("Changed the header in [news.tsx](app/(tabs)/news.tsx). Done.")
        assert said == "Changed the header in news.tsx. Done."

    def test_snake_case_names_keep_their_underscores(self):
        said = self.speak("Updated `coa_template.html` and metals_analytes, which is _really_ it.")
        assert said == "Updated coa_template.html and metals_analytes, which is really it."

    def test_headings_and_list_items_become_sentences(self):
        said = self.speak(
            "In priority order:\n\n## 1. Bugs worth fixing now\n\n"
            "- **\"Suggested photos\"** only works in dev\n2. Second thing"
        )
        assert said == (
            'In priority order: Bugs worth fixing now. "Suggested photos" only works in dev. Second thing.'
        )

    def test_tables_rules_urls_and_arrows_are_not_recited(self):
        said = self.speak(
            "Two kinds:\n\n| Approach | Good for |\n|---|:---:|\n| Parametric (code → CAD) | Parts |\n\n"
            "---\n\nOpen http://localhost:8501 now."
        )
        assert said == "Two kinds: Approach, Good for. Parametric (code to CAD), Parts. Open localhost:8501 now."

    def test_a_long_run_is_cut_between_words(self):
        said = self.speak("word " * 80, limit=100)
        assert said.endswith("word…")
