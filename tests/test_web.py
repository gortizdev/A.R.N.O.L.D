"""Opening web pages by voice.

The URL is built here from a site name and a query rather than dictated,
because spoken URLs do not survive transcription. That makes the scheme check
the important part: whatever comes back must still be an ordinary web page.
"""

import pytest

from arnold.commands import build_registry
from arnold.commands.registry import CommandContext
from arnold.config import Config


@pytest.fixture
def opened(monkeypatch):
    """Intercept the browser hand-off and record what would have opened."""
    calls = []
    monkeypatch.setattr(
        "arnold.commands.web._open_url",
        lambda url, browser: calls.append((url, browser)),
    )
    return calls


@pytest.fixture
def run():
    registry = build_registry()
    ctx = CommandContext(config=Config(), collector=None, alerts=None, jarvis=None)

    def go(command, **args):
        return registry.dispatch(command, args, ctx)

    go.ctx = ctx
    return go


class TestOpen:
    def test_known_site(self, run, opened):
        result = run("web.open", site="youtube")
        assert result.ok
        assert opened[0][0] == "https://www.youtube.com/"

    def test_claude_opens_a_new_chat(self, run, opened):
        assert run("web.open", site="claude").ok
        assert opened[0][0] == "https://claude.ai/new"

    def test_explicit_url(self, run, opened):
        assert run("web.open", url="https://example.com/x").ok
        assert opened[0][0] == "https://example.com/x"

    def test_a_bare_domain_gets_a_scheme(self, run, opened):
        """Nobody says "aitch tee tee pee ess colon"."""
        assert run("web.open", url="bbc.co.uk/news").ok
        assert opened[0][0] == "https://bbc.co.uk/news"

    def test_a_domain_in_the_site_slot_still_works(self, run, opened):
        assert run("web.open", site="example.org").ok
        assert opened[0][0] == "https://example.org"

    def test_user_defined_sites_win(self, run, opened):
        run.ctx.config.web.sites = {"youtube": "https://my.tube/"}
        assert run("web.open", site="youtube").ok
        assert opened[0][0] == "https://my.tube/"

    def test_nothing_to_open_is_refused(self, run, opened):
        assert not run("web.open").ok
        assert not opened

    def test_unknown_name_is_refused(self, run, opened):
        result = run("web.open", site="the thing with the videos")
        assert not result.ok
        assert not opened

    def test_disabled_refuses(self, run, opened):
        run.ctx.config.web.enabled = False
        assert not run("web.open", site="google").ok
        assert not opened


class TestSchemes:
    """A mis-heard command must not be able to open anything but a web page."""

    @pytest.mark.parametrize(
        "url",
        [
            "file:///C:/Users/geogo/secrets.txt",
            "javascript:alert(1)",
            "data:text/html,<script>x</script>",
            "vbscript:msgbox",
            "ms-settings:privacy",
        ],
    )
    def test_non_web_schemes_are_refused(self, run, opened, url):
        result = run("web.open", url=url)
        assert not result.ok
        assert not opened, f"{url} was opened"

    def test_the_error_names_the_scheme(self, run, opened):
        assert "file:" in run("web.open", url="file:///c:/x").error

    def test_a_scheme_only_string_is_refused(self, run, opened):
        assert not run("web.open", url="https://").ok
        assert not opened


class TestSearch:
    def test_google_is_the_default(self, run, opened):
        result = run("web.search", query="how tall is everest")
        assert result.ok
        assert opened[0][0] == (
            "https://www.google.com/search?q=how+tall+is+everest"
        )

    def test_youtube(self, run, opened):
        assert run("web.search", query="lofi beats", site="youtube").ok
        assert opened[0][0] == (
            "https://www.youtube.com/results?search_query=lofi+beats"
        )

    def test_queries_are_encoded(self, run, opened):
        """A spoken query can contain anything; it must not break the URL."""
        assert run("web.search", query="c++ & rust <tips>").ok
        assert " " not in opened[0][0]
        assert "&" not in opened[0][0].split("q=", 1)[1]

    def test_unknown_site_lists_the_alternatives(self, run, opened):
        result = run("web.search", query="x", site="altavista")
        assert not result.ok
        assert "youtube" in result.error
        assert not opened

    def test_empty_query_is_refused(self, run, opened):
        assert not run("web.search", query="   ").ok
        assert not opened

    def test_the_spoken_reply_names_the_site(self, run, opened):
        assert "YouTube" in run("web.search", query="x", site="youtube").speech.replace(
            "Youtube", "YouTube"
        )


class TestBrowsers:
    def test_default_is_the_system_browser(self, run, opened):
        run("web.open", site="google")
        assert opened[0][1] is None

    def test_a_named_browser_is_resolved_to_a_path(self, run, opened, tmp_path):
        exe = tmp_path / "firefox.exe"
        exe.write_text("")
        run.ctx.config.web.browsers = {"firefox": str(exe)}
        run("web.open", site="google", browser="firefox")
        assert opened[0][1] == exe

    def test_an_unknown_browser_is_refused(self, run, opened):
        result = run("web.open", site="google", browser="mosaic")
        assert not result.ok
        assert "firefox" in result.error
        assert not opened

    def test_a_known_but_absent_browser_says_so(self, run, opened):
        run.ctx.config.web.browsers = {"firefox": "C:/nope/firefox.exe"}
        result = run("web.open", site="google", browser="firefox")
        assert not result.ok
        assert "installed" in result.error

    def test_the_configured_default_is_used(self, run, opened, tmp_path):
        exe = tmp_path / "chrome.exe"
        exe.write_text("")
        run.ctx.config.web.browsers = {"chrome": str(exe)}
        run.ctx.config.web.default_browser = "chrome"
        run("web.open", site="google")
        assert opened[0][1] == exe


def test_sites_command_lists_destinations(run):
    result = run("web.sites")
    assert result.ok
    assert "youtube" in result.result["sites"]
    assert "google" in result.result["searchable"]
