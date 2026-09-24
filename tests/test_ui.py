"""The dashboard's server.

The page can run every command in the registry, so most of what matters here
is what gets refused: a request from another origin, a missing token, a
destructive command with the gate shut.
"""

from __future__ import annotations

import json
import pathlib
import threading
import time
import urllib.error
import urllib.request

import pytest

from arnold.config import Config, UiConfig, is_loopback
from arnold.ui.server import Dashboard, UiServer, _coerce


@pytest.fixture
def config(tmp_path):
    cfg = Config()
    cfg.ui = UiConfig(host="127.0.0.1", port=0, open_browser=False)
    cfg.mqtt.enabled = False
    cfg.jarvis.speech_route = "none"
    cfg.state_file = str(tmp_path / "state.json")
    cfg.log_file = str(tmp_path / "agent.log")
    cfg.source_path = tmp_path / "config.yaml"
    return cfg


@pytest.fixture
def server(config):
    """A real server on an ephemeral port, so the guards are exercised for real."""
    srv = UiServer(config)
    # Port 0 asked the OS to choose; ask it back so the URLs below are right.
    port = srv.server_address[1]
    srv.token = ""
    srv.allowed_origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
    thread = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    srv.base = f"http://127.0.0.1:{port}"
    try:
        yield srv
    finally:
        srv.shutdown()
        srv.dashboard.stop()
        srv.server_close()
        thread.join(timeout=5)


def request(server, path, *, method="GET", body=None, headers=None):
    """Returns (status, payload). Never raises on an HTTP error status."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(server.base + path, data=data, method=method)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return exc.code, {"raw": raw.decode("utf-8", "replace")}


UI = {"X-CA-UI": "1"}


class TestLoopback:
    @pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "127.1.2.3", ""])
    def test_loopback_spellings(self, host):
        assert is_loopback(host)

    @pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.103", "::"])
    def test_routable_addresses_are_not(self, host):
        assert not is_loopback(host)

    def test_a_lan_bind_without_a_token_is_a_config_problem(self):
        cfg = Config()
        cfg.ui = UiConfig(host="0.0.0.0")
        assert any("ui.token" in problem for problem in cfg.validate())

    def test_a_lan_bind_with_a_token_is_fine(self):
        cfg = Config()
        cfg.ui = UiConfig(host="0.0.0.0", token="x" * 32)
        assert not any("ui." in problem for problem in cfg.validate())


class TestRoutes:
    def test_the_page_is_served(self, server):
        with urllib.request.urlopen(server.base + "/", timeout=10) as response:
            page = response.read().decode()
        assert response.headers["Content-Type"].startswith("text/html")
        assert "<title>Console</title>" in page  # the script retitles it with the name

    def test_the_boot_placeholder_is_replaced(self, server):
        """A page still holding __BOOT__ is a page whose JavaScript never runs."""
        with urllib.request.urlopen(server.base + "/", timeout=10) as response:
            page = response.read().decode()
        assert "__BOOT__" not in page
        assert '"token":' in page

    def test_state_reports_the_machine(self, server):
        status, payload = request(server, "/api/state")
        assert status == 200
        assert payload["snapshot"]["cpu"]["percent"] is not None
        assert payload["device"]["id"]
        # No agent has written a state file, so it must not claim one is running.
        assert payload["agent"]["running"] is False

    def test_commands_are_listed(self, server):
        status, payload = request(server, "/api/commands")
        assert status == 200
        assert any(cmd["name"] == "query.system" for cmd in payload["commands"])

    def test_log_reports_a_missing_file_rather_than_failing(self, server):
        status, payload = request(server, "/api/log")
        assert status == 200
        assert payload["lines"] == [] or isinstance(payload["lines"], list)

    def test_unknown_routes_are_404(self, server):
        assert request(server, "/api/nope")[0] == 404


class TestSnapshotSharing:
    """Telemetry is collected on a clock, not per request.

    A snapshot walks every process and takes a second or more, and CPU percent
    is a delta against the previous call for the whole process - so two readers
    collecting in turn would split one reading between them and hand the second
    of them 0%.
    """

    def test_back_to_back_readers_share_one_snapshot(self, config):
        dash = Dashboard(config)
        first, second = dash.snapshot(), dash.snapshot()
        assert first["ts"] == second["ts"]

    def test_two_state_calls_both_report_the_same_cpu_figure(self, server):
        """The bug this guards: the second reader used to get 0.0%."""
        readings = [request(server, "/api/state")[1]["snapshot"]["cpu"]["percent"]
                    for _ in range(2)]
        assert readings[0] == readings[1]
        assert all(value is not None for value in readings)

    def test_a_collection_replaces_what_readers_see(self, config):
        dash = Dashboard(config)
        first = dash.snapshot()
        dash._collect_once()
        assert dash.snapshot()["ts"] != first["ts"]

    def test_reading_marks_the_dashboard_as_watched(self, config):
        dash = Dashboard(config)
        assert not dash._watched()
        dash.snapshot()
        assert dash._watched()

    def test_it_stops_collecting_once_nobody_is_looking(self, config):
        dash = Dashboard(config)
        dash.snapshot()
        dash._asked_at = time.monotonic() - 3600
        assert not dash._watched()


class TestGuards:
    def test_a_write_without_the_header_is_refused(self, server):
        status, payload = request(server, "/api/exec", method="POST", body={"cmd": "system.ping"})
        assert status == 403
        assert "X-CA-UI" in payload["error"]

    def test_a_foreign_origin_is_refused(self, server):
        status, _ = request(
            server, "/api/exec", method="POST", body={"cmd": "system.ping"},
            headers={**UI, "Origin": "http://evil.example"},
        )
        assert status == 403

    def test_our_own_origin_is_allowed(self, server):
        status, _ = request(
            server, "/api/exec", method="POST", body={"cmd": "system.ping"},
            headers={**UI, "Origin": server.base},
        )
        assert status == 200

    def test_an_unexpected_host_header_is_refused(self, server):
        """DNS rebinding: a hostile name resolved to 127.0.0.1 arrives with its own Host."""
        status, _ = request(server, "/api/state", headers={"Host": "attacker.example"})
        assert status == 403

    def test_a_token_is_required_when_set(self, server):
        server.token = "s3cret-token-value"
        assert request(server, "/api/state")[0] == 401
        assert request(server, "/api/state", headers={"X-CA-Token": "wrong"})[0] == 401
        assert request(server, "/api/state", headers={"X-CA-Token": server.token})[0] == 200

    def test_the_token_may_come_from_the_url_so_the_page_can_load(self, server):
        server.token = "s3cret-token-value"
        assert request(server, "/api/state?token=" + server.token)[0] == 200

    def test_an_oversized_body_is_refused(self, server):
        status, _ = request(
            server, "/api/say", method="POST",
            body={"text": "x" * 70_000}, headers=UI,
        )
        assert status == 413

    def test_a_non_json_body_is_refused(self, server):
        req = urllib.request.Request(
            server.base + "/api/exec", data=b"not json", method="POST",
            headers={**UI, "Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(req, timeout=10)
            raised = None
        except urllib.error.HTTPError as exc:
            raised = exc.code
        assert raised == 400


class TestRunningCommands:
    def test_a_command_runs_and_answers(self, server):
        status, payload = request(
            server, "/api/exec", method="POST", body={"cmd": "system.ping"}, headers=UI
        )
        assert status == 200
        assert payload["ok"] is True
        assert payload["cmd"] == "system.ping"

    def test_an_unknown_command_is_named_in_the_error(self, server):
        _, payload = request(
            server, "/api/exec", method="POST", body={"cmd": "query.nonsense"}, headers=UI
        )
        assert payload["ok"] is False
        assert "query.nonsense" in payload["error"]

    def test_destructive_commands_are_gated(self, config):
        config.security.allow_destructive = False
        dash = Dashboard(config)
        reply = dash.run_command("control.shutdown", {}, speak=False)
        assert reply["ok"] is False
        assert "allow_destructive" in reply["error"]

    def test_the_gate_is_checked_before_the_handler(self, config, monkeypatch):
        """The refusal must not depend on the handler happening to fail."""
        config.security.allow_destructive = False
        dash = Dashboard(config)

        def explode(*args, **kwargs):
            raise AssertionError("the handler must not be reached")

        monkeypatch.setattr(dash.registry, "dispatch", explode)
        assert dash.run_command("control.shutdown", {}, speak=False)["ok"] is False

    def test_a_background_command_without_mqtt_says_so(self, config):
        """Some commands only work in the agent; without MQTT there is no handover."""
        config.mqtt.enabled = False
        dash = Dashboard(config)
        agent_only = [
            cmd for cmd in dash.registry.describe()
            if cmd["needs_agent"] and not cmd["destructive"]
        ]
        if not agent_only:
            pytest.skip("no non-destructive agent-only commands registered")
        reply = dash.run_command(agent_only[0]["name"], {}, speak=False)
        assert reply["ok"] is False
        assert "agent" in reply["error"]


class TestArgumentCoercion:
    @pytest.mark.parametrize(
        "given,expected",
        [("true", True), ("false", False), ("40", 40), ("-3", -3),
         ("C", "C"), ("  spaced  ", "spaced"), ("3.5", "3.5")],
    )
    def test_obvious_scalars_become_scalars(self, given, expected):
        assert _coerce(given) == expected

    def test_non_strings_pass_through(self):
        assert _coerce(True) is True
        assert _coerce(7) == 7

    def test_blank_arguments_are_dropped_rather_than_sent_empty(self, server):
        """An untouched form field must not become `drive=""`."""
        _, payload = request(
            server, "/api/exec", method="POST",
            body={"cmd": "query.disk", "args": {"drive": ""}}, headers=UI,
        )
        # No drive given means "the busiest one", not a failure about a blank name.
        assert payload["ok"] is True


class TestSharingTheAgentsParts:
    """Inside the agent the dashboard borrows its collector and alert engine."""

    def test_injected_parts_are_used_rather_than_new_ones(self, config):
        from arnold.alerts import AlertEngine
        from arnold.monitors.collector import Collector

        collector = Collector(config.monitors)
        alerts = AlertEngine(config.alerts, device_name="x")
        dash = Dashboard(config, collector=collector, alerts=alerts)
        assert dash.collector is collector
        assert dash.alerts is alerts
        assert dash.context.collector is collector

    def test_it_still_builds_its_own_when_nothing_is_handed_in(self, config):
        dash = Dashboard(config)
        assert dash.collector is not None
        assert dash.registry.get("system.ping") is not None

    def test_a_taken_port_does_not_take_the_agent_down(self, config, server):
        """The agent must keep publishing telemetry even if the port is busy."""
        from arnold.ui.server import serve_in_background

        config.ui.port = server.server_address[1]  # already listening
        assert serve_in_background(config) is None

    def test_a_lan_bind_without_a_token_is_refused_in_the_agent_too(self, config):
        from arnold.ui.server import serve_in_background

        config.ui.host, config.ui.token = "0.0.0.0", ""
        assert serve_in_background(config) is None


class TestOpeningItOnCommand:
    """`desktop.dashboard` - "show me the dashboard", from voice or a shell."""

    def test_it_is_registered_and_needs_the_desktop(self, config):
        command = Dashboard(config).registry.get("desktop.dashboard")
        assert command is not None
        assert command.needs_desktop

    def test_an_already_running_server_is_reused(self, config, server, monkeypatch):
        """Starting a second one would take the port from the first."""
        from arnold.ui import server as ui_server

        config.ui.port = server.server_address[1]
        monkeypatch.setattr(
            ui_server, "serve_in_background",
            lambda *a, **k: pytest.fail("must not start a second server"),
        )
        url, started = ui_server.ensure_serving(config)
        assert started is False
        assert str(config.ui.port) in url

    def test_it_starts_one_when_nothing_is_serving(self, config):
        from arnold.ui.server import ensure_serving

        config.ui.port = 0  # the OS picks a free one, so nothing is on it yet
        url, started = ensure_serving(config)
        assert started is True
        assert url.startswith("http://localhost:")

    def test_it_says_why_rather_than_opening_a_broken_page(self, config, monkeypatch):
        from arnold.ui import server as ui_server

        monkeypatch.setattr(ui_server, "already_serving", lambda *a, **k: False)
        monkeypatch.setattr(ui_server, "serve_in_background", lambda *a, **k: None)
        with pytest.raises(OSError, match="arnold ui"):
            ui_server.ensure_serving(config)

    def test_the_browser_gets_the_dashboard_url(self, config, server, monkeypatch):
        from arnold.commands import web

        config.ui.port = server.server_address[1]
        opened = []
        monkeypatch.setattr(web, "_open_url", lambda url, browser: opened.append(url))

        dash = Dashboard(config)
        result = dash.run_command("desktop.dashboard", {}, speak=False)
        assert result["ok"] is True
        assert opened and opened[0].startswith(f"http://localhost:{config.ui.port}")

    def test_a_dying_process_refuses_rather_than_serving_briefly(self, config, monkeypatch):
        """`exec` from a shell exits at once; a server it started would too."""
        from arnold import runtime
        from arnold.commands import web
        from arnold.ui import server as ui_server

        monkeypatch.setattr(runtime, "IS_RESIDENT", False)
        monkeypatch.setattr(ui_server, "already_serving", lambda *a, **k: False)
        monkeypatch.setattr(web, "_open_url", lambda url, browser: pytest.fail("opened it"))

        reply = Dashboard(config).run_command("desktop.dashboard", {}, speak=False)
        assert reply["ok"] is False
        assert "computer" in reply["error"]

    def test_a_resident_process_takes_it_on(self, config, monkeypatch):
        from arnold import runtime
        from arnold.commands import web
        from arnold.ui import server as ui_server

        monkeypatch.setattr(runtime, "IS_RESIDENT", True)
        monkeypatch.setattr(ui_server, "already_serving", lambda *a, **k: False)
        monkeypatch.setattr(web, "_open_url", lambda url, browser: None)
        config.ui.port = 0

        assert Dashboard(config).run_command("desktop.dashboard", {}, speak=False)["ok"]

    def test_a_disabled_dashboard_is_not_opened(self, config, monkeypatch):
        from arnold.commands import web

        monkeypatch.setattr(web, "_open_url", lambda url, browser: pytest.fail("opened it"))
        config.ui.enabled = False
        reply = Dashboard(config).run_command("desktop.dashboard", {}, speak=False)
        assert reply["ok"] is False
        assert "switched off" in reply["error"]

    def test_the_voice_session_may_call_it(self):
        from arnold.voice.tools import PC_COMMANDS

        assert "desktop.dashboard" in PC_COMMANDS

    @pytest.mark.parametrize(
        "said",
        [
            "open the dashboard",
            "show me the dashboard",
            "bring up the control panel",
            "pull up the console",
            "put the dashboard on screen",
        ],
    )
    def test_spoken_phrasings_match_locally(self, config, said):
        from arnold.commands import CommandContext
        from arnold.monitors.collector import Collector
        from arnold.alerts import AlertEngine
        from arnold.voice.brain import LocalBrain

        ctx = CommandContext(
            config=config,
            collector=Collector(config.monitors),
            alerts=AlertEngine(config.alerts, config.device.friendly_name),
        )
        matched = LocalBrain(ctx).match(said)
        assert matched is not None and matched[0] == "desktop.dashboard"

    def test_it_does_not_swallow_unrelated_talk(self, config):
        from arnold.commands import CommandContext
        from arnold.monitors.collector import Collector
        from arnold.alerts import AlertEngine
        from arnold.voice.brain import LocalBrain

        ctx = CommandContext(
            config=config,
            collector=Collector(config.monitors),
            alerts=AlertEngine(config.alerts, config.device.friendly_name),
        )
        brain = LocalBrain(ctx)
        for said in ["put the dashboard camera footage on the telly", "how much disk is left"]:
            matched = brain.match(said)
            assert matched is None or matched[0] != "desktop.dashboard"


class TestAsking:
    """The page's line to Jarvis, mirroring the voice session's ask_jarvis."""

    def test_empty_text_is_refused(self, config):
        assert Dashboard(config).ask("   ")["ok"] is False

    def test_without_the_relay_it_says_why(self, config):
        config.jarvis.home_assistant.token = ""
        reply = Dashboard(config).ask("hello")
        assert reply["ok"] is False
        assert "token" in reply["error"]

    def test_when_this_pc_is_jarvis_there_is_nobody_to_ask(self, config):
        config.jarvis.home_assistant.token = "tok"
        config.assistant.mirror_jarvis = True
        reply = Dashboard(config).ask("hello")
        assert reply["ok"] is False
        assert "mirror_jarvis" in reply["error"]

    def test_the_reply_comes_back_attributed(self, config, monkeypatch):
        from arnold.voice import brain

        config.jarvis.home_assistant.token = "tok"
        monkeypatch.setattr(brain.JarvisBrain, "ask", lambda self, text: "Milk and eggs.")
        reply = Dashboard(config).ask("what's on the list?")
        assert reply["ok"] is True
        assert reply["reply"] == "Milk and eggs."
        assert reply["asked"] == "what's on the list?"

    def test_a_dead_pi_is_reported_not_raised(self, config, monkeypatch):
        from arnold.voice import brain

        config.jarvis.home_assistant.token = "tok"

        def down(self, text):
            raise brain.BrainError("I couldn't reach Jarvis on the Pi.")

        monkeypatch.setattr(brain.JarvisBrain, "ask", down)
        reply = Dashboard(config).ask("hello")
        assert reply["ok"] is False
        assert "reach" in reply["error"]

    def test_the_state_says_who_lives_here(self, config):
        state = Dashboard(config).state()
        me = state["assistant"]
        assert me["name"] == "Arnold"
        assert me["wake_word"] == config.voice.wake_word
        assert me["palette"] == "steel"
        assert me["can_ask"] is False  # the fixture has no HA token
        assert "voice_state" not in state["jarvis"]

    def test_the_page_boots_with_the_name(self, server):
        with urllib.request.urlopen(server.base + "/", timeout=10) as response:
            page = response.read().decode("utf-8")
        assert '"assistant": "Arnold"' in page
        assert "__BOOT__" not in page


class TestSaying:
    def test_empty_text_is_refused(self, config):
        assert Dashboard(config).say("   ")["ok"] is False

    def test_speech_failure_is_reported_not_raised(self, config):
        """speech_route: none means the client refuses; the page must hear about it."""
        reply = Dashboard(config).say("hello")
        assert reply["ok"] in (True, False)
        if not reply["ok"]:
            assert reply["error"]


class TestImportingADocument:
    """The one route that takes a file: a document picked in the browser."""

    @staticmethod
    def _docx(*lines: str) -> bytes:
        import io
        import zipfile

        w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
        body = "".join(
            '<w:p><w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr></w:pPr>'
            f"<w:r><w:t>{line}</w:t></w:r></w:p>" for line in lines
        )
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as bundle:
            bundle.writestr("word/document.xml", f"<w:document {w}><w:body>{body}</w:body></w:document>")
        return buffer.getvalue()

    @staticmethod
    def _upload(server, name, data, headers=None):
        req = urllib.request.Request(server.base + "/api/import", data=data, method="POST")
        for key, value in {"X-CA-UI": "1", "X-File-Name": name, **(headers or {})}.items():
            req.add_header(key, value)
        req.add_header("Content-Type", "application/octet-stream")
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def test_a_document_becomes_the_weeks_list(self, server, config):
        status, reply = self._upload(server, "Geo Update 9-23.docx", self._docx("Ship it", "Call Sam"))
        assert status == 200 and reply["ok"], reply
        assert reply["result"]["total"] == 2
        saved = pathlib.Path(config.state_file).with_name("mail") / "uploads" / "Geo Update 9-23.docx"
        assert saved.exists()
        _, state = request(server, "/api/state")
        assert [i["text"] for i in state["todos"]["items"]] == ["Ship it", "Call Sam"]
        assert state["todos"]["week"]["state"] == "imported"

    def test_only_documents_and_only_by_basename(self, server, config):
        status, reply = self._upload(server, "..\..\evil.exe", b"MZ")
        assert status == 200 and not reply["ok"]
        assert "not .exe" in reply["error"]
        status, reply = self._upload(server, "../escape.txt", b"- fine\n")
        assert reply["ok"]
        assert (pathlib.Path(config.state_file).with_name("mail") / "uploads" / "escape.txt").exists()

    def test_needs_the_ui_header_like_every_write(self, server):
        req = urllib.request.Request(server.base + "/api/import", data=b"x", method="POST")
        req.add_header("X-File-Name", "a.txt")
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(req, timeout=10)
        assert caught.value.code == 403

    def test_an_empty_or_huge_body_is_refused(self, server):
        status, reply = self._upload(server, "a.txt", b"")
        assert status == 400 and not reply["ok"]
