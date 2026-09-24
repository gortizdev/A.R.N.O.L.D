"""Handing work to Claude Code.

This is the only command that edits source, so the tests that matter are the
ones pinning the boundary: it is off until switched on, and it can only work
inside a directory named in the config.
"""

import time
import types

import pytest

from arnold.commands import build_registry, code
from arnold.commands.registry import CommandContext
from arnold.config import Config


@pytest.fixture(autouse=True)
def clean_tasks():
    with code._lock:
        code._tasks.clear()
        code._order.clear()
    yield


@pytest.fixture
def run(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    cli = tmp_path / "claude.exe"
    cli.write_text("")

    config = Config()
    config.code.enabled = True
    config.code.cli_path = str(cli)
    config.code.projects = {"demo": str(project)}
    config.code.speak_when_done = False

    registry = build_registry()
    ctx = CommandContext(config=config, collector=None, alerts=None, jarvis=None)

    def go(command, **args):
        return registry.dispatch(command, args, ctx)

    go.ctx = ctx
    go.project = project
    return go


def finished(stdout, code_=0):
    return types.SimpleNamespace(stdout=stdout, stderr="", returncode=code_)


SUCCESS = (
    '{"type":"result","is_error":false,"result":"Fixed the thing.",'
    '"session_id":"abc-123","total_cost_usd":0.04}'
)


class TestBoundary:
    def test_disabled_by_default(self):
        assert Config().code.enabled is False

    def test_refuses_when_disabled(self, run, monkeypatch):
        run.ctx.config.code.enabled = False
        monkeypatch.setattr(code.process, "run", lambda *a, **k: pytest.fail("ran anyway"))
        result = run("code.task", project="demo", prompt="do a thing")
        assert not result.ok
        assert "switched on" in result.error

    def test_an_unlisted_project_is_refused(self, run, monkeypatch):
        monkeypatch.setattr(code.process, "run", lambda *a, **k: pytest.fail("ran anyway"))
        result = run("code.task", project="/etc", prompt="do a thing")
        assert not result.ok
        assert "demo" in result.error

    def test_a_path_is_not_a_project_name(self, run, monkeypatch):
        """The allowlist is by name; a directory must not be passable directly."""
        monkeypatch.setattr(code.process, "run", lambda *a, **k: pytest.fail("ran anyway"))
        assert not run("code.task", project=str(run.project), prompt="x").ok

    def test_no_projects_configured(self, run, monkeypatch):
        run.ctx.config.code.projects = {}
        monkeypatch.setattr(code.process, "run", lambda *a, **k: pytest.fail("ran anyway"))
        result = run("code.task", project="demo", prompt="fix the failing test")
        assert "allowlisted" in result.error

    def test_an_empty_prompt_is_refused(self, run):
        assert not run("code.task", project="demo", prompt="hi").ok

    def test_these_commands_run_in_the_agent(self):
        """Otherwise a one-shot exec exits and abandons the background thread."""
        registry = build_registry()
        assert registry.get("code.task").needs_agent
        assert registry.get("code.status").needs_agent
        assert not registry.get("code.projects").needs_agent


class TestRunning:
    def wait_for(self, predicate, timeout=3.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return False

    def test_it_runs_in_the_project_directory(self, run, monkeypatch):
        seen = {}

        def fake(argv, **kwargs):
            seen["argv"] = argv
            seen["cwd"] = kwargs.get("cwd")
            return finished(SUCCESS)

        monkeypatch.setattr(code.process, "run", fake)
        result = run("code.task", project="demo", prompt="fix the bug please")
        assert result.ok
        assert self.wait_for(lambda: "cwd" in seen)
        assert seen["cwd"] == str(run.project)
        assert "-p" in seen["argv"] and "fix the bug please" in seen["argv"]
        assert "--permission-mode" in seen["argv"]

    def test_it_returns_before_the_job_finishes(self, run, monkeypatch):
        monkeypatch.setattr(
            code.process, "run", lambda *a, **k: (time.sleep(0.4), finished(SUCCESS))[1]
        )
        started = time.monotonic()
        result = run("code.task", project="demo", prompt="something slow")
        assert time.monotonic() - started < 0.2, "voice cannot wait for it"
        assert result.result["state"] == "running"

    def test_the_result_is_recorded(self, run, monkeypatch):
        monkeypatch.setattr(code.process, "run", lambda *a, **k: finished(SUCCESS))
        run("code.task", project="demo", prompt="fix the bug")
        assert self.wait_for(lambda: run("code.status").result["state"] == "done")
        status = run("code.status")
        assert status.result["summary"] == "Fixed the thing."
        assert status.result["session_id"] == "abc-123"
        assert "Fixed the thing." in status.speech

    def test_a_failure_is_reported_not_swallowed(self, run, monkeypatch):
        monkeypatch.setattr(
            code.process,
            "run",
            lambda *a, **k: finished('{"is_error":true,"result":"could not build"}'),
        )
        run("code.task", project="demo", prompt="break it")
        assert self.wait_for(lambda: run("code.status").result["state"] == "failed")
        assert "could not build" in run("code.status").result["summary"]

    def test_unreadable_output_is_a_failure_not_a_crash(self, run, monkeypatch):
        monkeypatch.setattr(code.process, "run", lambda *a, **k: finished("not json at all"))
        run("code.task", project="demo", prompt="do a thing")
        assert self.wait_for(lambda: run("code.status").result["state"] == "failed")

    def test_a_crash_in_the_cli_is_survivable(self, run, monkeypatch):
        def boom(*a, **k):
            raise OSError("cli exploded")

        monkeypatch.setattr(code.process, "run", boom)
        run("code.task", project="demo", prompt="do a thing")
        assert self.wait_for(lambda: run("code.status").result["state"] == "failed")

    def test_only_one_job_at_a_time(self, run, monkeypatch):
        monkeypatch.setattr(
            code.process, "run", lambda *a, **k: (time.sleep(0.5), finished(SUCCESS))[1]
        )
        assert run("code.task", project="demo", prompt="the first job").ok
        second = run("code.task", project="demo", prompt="the second job")
        assert not second.ok
        assert "one at a time" in second.error.lower()

    def test_a_single_project_needs_no_naming(self, run, monkeypatch):
        monkeypatch.setattr(code.process, "run", lambda *a, **k: finished(SUCCESS))
        assert run("code.task", prompt="just do it in the only project").ok

    def test_status_with_nothing_to_report(self, run):
        assert run("code.status").ok


class TestSubscription:
    """Work must draw on the signed-in Max account, not metered API billing."""

    def test_an_api_key_is_hidden_from_the_child(self, run, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-not-be-used")
        seen = {}

        def fake(argv, **kwargs):
            seen["env"] = kwargs.get("env")
            return finished(SUCCESS)

        monkeypatch.setattr(code.process, "run", fake)
        run("code.task", project="demo", prompt="fix the bug")
        deadline = time.time() + 3
        while "env" not in seen and time.time() < deadline:
            time.sleep(0.02)
        assert seen["env"] is not None
        assert "ANTHROPIC_API_KEY" not in seen["env"]
        # The rest of the environment still has to reach the child.
        assert "PATH" in seen["env"] or "Path" in seen["env"]

    def test_the_environment_is_inherited_when_there_is_no_key(self, run, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        seen = {}

        def fake(argv, **kwargs):
            seen["env"] = kwargs.get("env")
            return finished(SUCCESS)

        monkeypatch.setattr(code.process, "run", fake)
        run("code.task", project="demo", prompt="fix the bug")
        deadline = time.time() + 3
        while "env" not in seen and time.time() < deadline:
            time.sleep(0.02)
        assert seen["env"] is None, "no need to rebuild the environment"

    def test_opting_out_leaves_the_key_alone(self, run, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-deliberate")
        run.ctx.config.code.use_subscription = False
        seen = {}

        def fake(argv, **kwargs):
            seen["env"] = kwargs.get("env")
            return finished(SUCCESS)

        monkeypatch.setattr(code.process, "run", fake)
        run("code.task", project="demo", prompt="fix the bug")
        deadline = time.time() + 3
        while "env" not in seen and time.time() < deadline:
            time.sleep(0.02)
        assert seen["env"] is None

    def test_cost_is_reported_as_an_estimate_not_a_charge(self, run, monkeypatch):
        monkeypatch.setattr(code.process, "run", lambda *a, **k: finished(SUCCESS))
        run("code.task", project="demo", prompt="fix the bug")
        deadline = time.time() + 3
        while run("code.status").result["state"] == "running" and time.time() < deadline:
            time.sleep(0.02)
        result = run("code.status").result
        assert "equivalent_cost_usd" in result
        assert "cost_usd" not in result


class TestParsing:
    def test_plain_json(self):
        assert code._parse(SUCCESS)["session_id"] == "abc-123"

    def test_json_after_noise(self):
        """The CLI may print progress before the result object."""
        assert code._parse("loading...\n" + SUCCESS)["result"] == "Fixed the thing."

    def test_nothing_at_all(self):
        assert code._parse("") is None

    def test_garbage(self):
        assert code._parse("segmentation fault") is None
