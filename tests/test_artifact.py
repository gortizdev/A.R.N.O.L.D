"""Pages the assistant makes and puts on screen.

Artifacts open as `file:` URLs, which web.open deliberately refuses. That is
safe only because the file is one this process just wrote to a directory it
chose - so the tests that matter are the ones pinning that a caller cannot
steer either the path or the scheme.
"""

import pytest

from arnold.commands import build_registry
from arnold.commands.artifact import artifacts_dir
from arnold.commands.registry import CommandContext
from arnold.config import Config


@pytest.fixture
def shown(monkeypatch):
    """Intercept the browser hand-off; record what would have been opened."""
    opened = []
    monkeypatch.setattr(
        "arnold.commands.artifact._show",
        lambda ctx, path: opened.append(path),
    )
    return opened


@pytest.fixture
def run(tmp_path):
    config = Config()
    # source_path is the config file; artifacts land beside it.
    config.source_path = tmp_path / "config.yaml"
    config.source_path.write_text("")
    registry = build_registry()
    ctx = CommandContext(config=config, collector=None, alerts=None, jarvis=None)

    def go(command, **args):
        return registry.dispatch(command, args, ctx)

    go.ctx = ctx
    go.dir = tmp_path / "artifacts"
    return go


class TestIdentity:
    def test_the_page_is_signed_by_the_assistant(self, run, shown):
        run.ctx.config.assistant.name = "Mycroft"
        run.ctx.config.device.friendly_name = "Study PC"
        result = run("artifact.create", title="t", html="<p>x</p>")
        page = (run.dir / result.result["name"]).read_text(encoding="utf-8")
        assert 'data-palette="steel"' in page
        assert "Mycroft · Study PC" in page

    def test_mirroring_jarvis_puts_up_his_hud(self, run, shown):
        run.ctx.config.assistant.mirror_jarvis = True
        result = run("artifact.create", title="t", html="<p>x</p>")
        page = (run.dir / result.result["name"]).read_text(encoding="utf-8")
        assert 'data-palette="gold"' in page
        assert "<footer><span>Jarvis" in page and "Mycroft \u00b7" not in page

    def test_the_model_facing_parts_survive_both_looks(self, run, shown):
        """A fragment written against these names renders in either identity."""
        result = run("artifact.create", title="t", html="<p>x</p>")
        page = (run.dir / result.result["name"]).read_text(encoding="utf-8")
        for token in (".grid", ".card", ".stat", ".label", ".value", ".bar", ".badge",
                      ".ok", ".warn", ".crit", ".dim", ".mono",
                      "--accent", "--ink", "--dim", "--ok", "--warn", "--crit"):
            assert token in page, token


class TestCreate:
    def test_html_lands_in_a_styled_page(self, run, shown):
        result = run("artifact.create", title="Disk usage", html="<h2>Hi</h2>")
        assert result.ok
        page = (run.dir / result.result["name"]).read_text(encoding="utf-8")
        assert "<h2>Hi</h2>" in page
        assert "<!doctype html>" in page.lower()
        assert "Disk usage" in page  # the header, and the <title>

    def test_markdown_is_rendered(self, run, shown):
        result = run("artifact.create", title="Notes", markdown="# Big\n\n- one\n- two")
        page = (run.dir / result.result["name"]).read_text(encoding="utf-8")
        assert "<h1>Big</h1>" in page
        assert "<li>one</li>" in page

    def test_markdown_tables_and_code(self, run, shown):
        result = run(
            "artifact.create",
            title="t",
            markdown="| a | b |\n|---|---|\n| 1 | 2 |\n\n```py\nx=1\n```",
        )
        page = (run.dir / result.result["name"]).read_text(encoding="utf-8")
        assert "<table>" in page and "<code" in page

    def test_a_full_document_is_folded_into_the_styling(self, run, shown):
        """Otherwise a model-written page lands looking like nothing else here."""
        doc = (
            "<!DOCTYPE html><html><head><title>Mine</title>"
            "<style>body{background:#fff;color:#111} .hint{color:#555}</style>"
            "</head><body><p>x</p><script>go()</script></body></html>"
        )
        result = run("artifact.create", title="Readout", html=doc)
        page = (run.dir / result.result["name"]).read_text(encoding="utf-8")

        assert "<p>x</p>" in page and "go()" in page  # its content survives
        assert page.count("<html") == 1 and page.count("<body") == 1
        assert "--accent" in page  # the theme is there
        # Its body rule now dresses the panel, not the page.
        assert "body{background:#fff" not in page
        assert ".frag{background:#fff" in page
        assert ".hint{color:#555}" in page  # everything else is left alone

    def test_raw_skips_the_styling(self, run, shown):
        """The escape hatch, for when a page really does want the whole window."""
        doc = "<!DOCTYPE html><html><head><title>Mine</title></head><body>x</body></html>"
        result = run("artifact.create", title="ignored", html=doc, raw=True)
        assert (run.dir / result.result["name"]).read_text(encoding="utf-8") == doc

    def test_a_head_style_lands_before_the_content(self, run, shown):
        """Later in the cascade than the theme, so what it wrote still wins."""
        doc = "<html><head><style>p{color:red}</style></head><body><p>hi</p></body></html>"
        result = run("artifact.create", title="t", html=doc)
        page = (run.dir / result.result["name"]).read_text(encoding="utf-8")
        assert page.index("p{color:red}") < page.index("<p>hi</p>")
        assert page.index("--accent") < page.index("p{color:red}")

    def test_it_is_opened_by_default(self, run, shown):
        run("artifact.create", title="x", html="<p>y</p>")
        assert len(shown) == 1

    def test_open_false_only_writes_it(self, run, shown):
        result = run("artifact.create", title="x", html="<p>y</p>", open=False)
        assert result.ok
        assert not shown
        assert (run.dir / result.result["name"]).exists()

    def test_empty_content_is_refused(self, run, shown):
        assert not run("artifact.create", title="x").ok
        assert not shown

    def test_the_title_becomes_the_file_name(self, run, shown):
        result = run("artifact.create", title="Weekly F1 Standings!", html="<p>x</p>")
        assert result.result["name"].endswith("-weekly-f1-standings.html")

    def test_a_hostile_title_cannot_escape_the_directory(self, run, shown):
        result = run("artifact.create", title="../../etc/passwd", html="<p>x</p>")
        written = (run.dir / result.result["name"]).resolve()
        assert written.parent == run.dir.resolve()

    def test_titles_are_escaped_into_the_page(self, run, shown):
        """The title is placed in <title> and a heading, so it must be escaped."""
        result = run("artifact.create", title="<script>bad()</script>", html="<p>x</p>")
        page = (run.dir / result.result["name"]).read_text(encoding="utf-8")
        assert "<script>bad()</script>" not in page
        assert "&lt;script&gt;" in page

    def test_an_oversized_page_is_refused(self, run, shown):
        assert not run("artifact.create", title="x", html="y" * 3_000_000).ok


class TestOpenAndList:
    def test_open_without_a_name_takes_the_newest(self, run, shown):
        run("artifact.create", title="first", html="<p>1</p>", open=False)
        second = run("artifact.create", title="second", html="<p>2</p>", open=False)
        assert run("artifact.open").ok
        assert shown[-1].name == second.result["name"]

    def test_open_by_name(self, run, shown):
        made = run("artifact.create", title="thing", html="<p>1</p>", open=False)
        assert run("artifact.open", name=made.result["name"]).ok
        assert shown[-1].name == made.result["name"]

    def test_a_traversing_name_is_refused(self, run, shown):
        """`name` comes from a model; it must not be able to open any file."""
        result = run("artifact.open", name="../../../Windows/System32/drivers/etc/hosts")
        assert not result.ok
        assert not shown

    def test_an_unknown_name_is_refused(self, run, shown):
        assert not run("artifact.open", name="nope.html").ok
        assert not shown

    def test_open_with_nothing_made_yet(self, run, shown):
        assert not run("artifact.open").ok

    def test_list_is_newest_first(self, run, shown):
        run("artifact.create", title="older", html="<p>1</p>", open=False)
        run("artifact.create", title="newer", html="<p>2</p>", open=False)
        result = run("artifact.list")
        assert result.result["count"] == 2
        assert result.result["artifacts"][0]["title"] == "newer"

    def test_list_when_empty(self, run):
        assert run("artifact.list").ok


def test_artifacts_sit_beside_the_config(run, shown):
    """Not the working directory: the agent is launched by Task Scheduler."""
    assert artifacts_dir(run.ctx) == (run.ctx.config.source_path.parent / "artifacts")
    assert artifacts_dir(run.ctx).is_absolute()


def test_these_commands_need_the_desktop():
    """Otherwise Jarvis over SSH writes a page nobody ever sees."""
    registry = build_registry()
    assert registry.get("artifact.create").needs_desktop
    assert registry.get("artifact.open").needs_desktop
    # Listing is just data, so it can answer from session 0.
    assert not registry.get("artifact.list").needs_desktop
