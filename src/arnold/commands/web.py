"""Sending the browser somewhere.

`control.launch` starts an allowlisted application; this is the other half -
pointing a browser at something specific. Spoken URLs are hopeless ("aitch tee
tee pee colon slash slash..."), so the assistant passes a site name and a
query and the URL is built here from a table, rather than dictated and
mis-transcribed.

Only http and https are ever opened. An assistant that will open whatever URL
it is handed is one mis-hearing away from a `file:` or `javascript:` URL, and
neither is ever what somebody asked for out loud.
"""

from __future__ import annotations

import logging
import os
import threading
import urllib.parse
from pathlib import Path

from .. import process
from ..humanize import join_speech
from ..platform_win import foreground
from .registry import (
    CommandContext,
    CommandError,
    CommandResult,
    Registry,
    arg_str,
)

log = logging.getLogger(__name__)

# Where "open youtube" should land. Spoken aliases share a destination.
SITES: dict[str, str] = {
    "google": "https://www.google.com/",
    "youtube": "https://www.youtube.com/",
    "claude": "https://claude.ai/new",
    "chatgpt": "https://chatgpt.com/",
    "gmail": "https://mail.google.com/",
    "email": "https://mail.google.com/",
    "google calendar": "https://calendar.google.com/",
    "google drive": "https://drive.google.com/",
    "google maps": "https://www.google.com/maps",
    "maps": "https://www.google.com/maps",
    "github": "https://github.com/",
    "reddit": "https://www.reddit.com/",
    "amazon": "https://www.amazon.com/",
    "netflix": "https://www.netflix.com/",
    "twitch": "https://www.twitch.tv/",
    "spotify": "https://open.spotify.com/",
    "wikipedia": "https://www.wikipedia.org/",
    "x": "https://x.com/",
    "twitter": "https://x.com/",
    "news": "https://news.google.com/",
    "weather": "https://www.google.com/search?q=weather",
}

# Query templates. {q} is replaced with the URL-encoded search text.
SEARCHES: dict[str, str] = {
    "google": "https://www.google.com/search?q={q}",
    "youtube": "https://www.youtube.com/results?search_query={q}",
    "images": "https://www.google.com/search?tbm=isch&q={q}",
    "news": "https://news.google.com/search?q={q}",
    "maps": "https://www.google.com/maps/search/{q}",
    "google maps": "https://www.google.com/maps/search/{q}",
    "amazon": "https://www.amazon.com/s?k={q}",
    "github": "https://github.com/search?q={q}&type=repositories",
    "reddit": "https://www.reddit.com/search/?q={q}",
    "wikipedia": "https://en.wikipedia.org/w/index.php?search={q}",
    "spotify": "https://open.spotify.com/search/{q}",
    "twitch": "https://www.twitch.tv/search?term={q}",
    "netflix": "https://www.netflix.com/search?q={q}",
    "stack overflow": "https://stackoverflow.com/search?q={q}",
}

# Usual install locations, so `browser: firefox` works without configuration.
_BROWSER_PATHS: dict[str, tuple[str, ...]] = {
    "firefox": (
        r"C:\Program Files\Mozilla Firefox\firefox.exe",
        r"C:\Program Files (x86)\Mozilla Firefox\firefox.exe",
    ),
    "chrome": (
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ),
    "edge": (
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ),
    "brave": (
        r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
        r"C:\Program Files (x86)\BraveSoftware\Brave-Browser\Application\brave.exe",
    ),
    "opera": (r"C:\Program Files\Opera\opera.exe",),
    "vivaldi": (r"C:\Program Files\Vivaldi\Application\vivaldi.exe",),
}

_ALLOWED_SCHEMES = ("http", "https")

# Spoken aloud, so "Youtube" and "Github" from .title() are not good enough.
_DISPLAY = {
    "youtube": "YouTube",
    "github": "GitHub",
    "chatgpt": "ChatGPT",
    "x": "X",
    "gmail": "Gmail",
    "gmaps": "Google Maps",
    "google maps": "Google Maps",
    "google drive": "Google Drive",
    "google calendar": "Google Calendar",
    "stack overflow": "Stack Overflow",
    "openai": "OpenAI",
    "netflix": "Netflix",
    "reddit": "Reddit",
}


def _pretty(name: str) -> str:
    key = (name or "").strip().lower()
    return _DISPLAY.get(key, key.title() if key else key)


def register_all(registry: Registry) -> None:
    registry.register(
        "web.open",
        _open,
        "Open a website in the browser.",
        {
            "site": "a known name like 'youtube' or 'claude'",
            "url": "or an explicit http/https URL",
            "browser": "optional: firefox, chrome, edge... default is the system browser",
        },
        needs_desktop=True,
    )
    registry.register(
        "web.search",
        _search,
        "Search a website and open the results.",
        {
            "query": "what to search for",
            "site": "google (default), youtube, amazon, maps, github...",
            "browser": "optional browser name",
        },
        needs_desktop=True,
    )
    registry.register("web.sites", _sites, "List the site names that can be opened.")
    # Here rather than under control.*: the join link is a URL, and this module
    # already owns handing one to a browser and raising the window afterwards.
    registry.register(
        "web.join_meeting",
        _join_meeting,
        "Open the join link for the meeting in progress, or the next one.",
        {"which": "'current', 'next', or 'auto' (default)"},
        needs_desktop=True,
    )


# -- resolution --------------------------------------------------------------


def _known_sites(ctx: CommandContext) -> dict[str, str]:
    """Built-in destinations, with the user's own entries taking precedence."""
    sites = dict(SITES)
    for name, url in (ctx.config.web.sites or {}).items():
        sites[str(name).strip().lower()] = str(url)
    return sites


def _clean_url(url: str) -> str:
    """Accept only a real http(s) URL, tolerating a missing scheme."""
    text = (url or "").strip().strip("<>\"'")
    if not text:
        raise CommandError("I need a site name or a URL to open.")

    parsed = urllib.parse.urlsplit(text)
    if not parsed.scheme:
        # "youtube.com/feed" - spoken URLs never include the scheme.
        if "." not in text.split("/")[0]:
            raise CommandError(f"I don't know a site called {text}.")
        text = "https://" + text
        parsed = urllib.parse.urlsplit(text)

    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        raise CommandError(
            f"I only open web pages, and {parsed.scheme}: isn't one."
        )
    if not parsed.netloc:
        raise CommandError(f"{url} doesn't look like a web address.")
    return text


def _browser_path(ctx: CommandContext, name: str) -> Path | None:
    """Executable for a named browser, or None to use the system default."""
    wanted = (name or ctx.config.web.default_browser or "").strip().lower()
    if not wanted or wanted in ("default", "system"):
        return None

    configured = {k.lower(): v for k, v in (ctx.config.web.browsers or {}).items()}
    candidates = [configured[wanted]] if wanted in configured else _BROWSER_PATHS.get(wanted, ())
    for candidate in candidates:
        path = Path(candidate)
        if path.exists():
            return path

    if wanted not in configured and wanted not in _BROWSER_PATHS:
        raise CommandError(
            f"I don't know a browser called {name}. I know "
            f"{join_speech(sorted(_BROWSER_PATHS), 'and')}."
        )
    raise CommandError(f"{wanted.title()} doesn't seem to be installed on this PC.")


def _default_browser_exe() -> str:
    """Executable name Windows uses for https, e.g. 'firefox.exe'.

    Needed only so the right window can be raised afterwards; a failure here
    costs the focus, not the navigation.
    """
    try:
        import winreg

        key = (
            r"SOFTWARE\Microsoft\Windows\Shell\Associations\UrlAssociations"
            r"\https\UserChoice"
        )
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as handle:
            prog_id = winreg.QueryValueEx(handle, "ProgId")[0]
        with winreg.OpenKey(
            winreg.HKEY_CLASSES_ROOT, rf"{prog_id}\shell\open\command"
        ) as handle:
            command = winreg.QueryValueEx(handle, "")[0]
        return Path(command.strip('"').split('"')[0]).name.lower()
    except Exception as exc:
        log.debug("could not read the default browser: %s", exc)
        return ""


def _open_url(url: str, browser: Path | None) -> None:
    """Hand the URL to the browser and bring it forward.

    Separated out so tests can intercept the whole hand-off.
    """
    # Given up front, so a browser that is starting can raise itself. An
    # already-running one ignores this and is raised explicitly below.
    foreground.allow_foreground()

    if browser is not None:
        process.launch([str(browser), url])
        target = browser.name.lower()
    else:
        target = _default_browser_exe()
        try:
            os.startfile(url)  # the shell's own https association
        except AttributeError:  # not Windows
            import webbrowser

            webbrowser.open(url)

    if target:
        # Off the caller's thread: the page is already on its way, and waiting
        # several seconds for focus would stall the spoken reply.
        threading.Thread(
            target=foreground.raise_process_windows,
            args=({target},),
            daemon=True,
            name="raise-browser",
        ).start()


def _describe(browser: Path | None) -> str:
    return browser.stem.replace("msedge", "Edge").title() if browser else "the browser"


# -- handlers ----------------------------------------------------------------


def _open(ctx: CommandContext, args: dict) -> CommandResult:
    if not ctx.config.web.enabled:
        raise CommandError("Opening web pages is switched off on this PC.")

    sites = _known_sites(ctx)
    name = str(args.get("site") or "").strip().lower()
    label = _pretty(name)

    if name and name in sites:
        url = sites[name]
    elif args.get("url"):
        url = _clean_url(arg_str(args, "url"))
        label = urllib.parse.urlsplit(url).netloc.removeprefix("www.")
    elif name:
        # A name that is really a domain ("bbc.co.uk") still works.
        url = _clean_url(name)
        label = urllib.parse.urlsplit(url).netloc.removeprefix("www.")
    else:
        raise CommandError("Tell me which site to open.")

    browser = _browser_path(ctx, str(args.get("browser") or ""))
    try:
        _open_url(url, browser)
    except OSError as exc:
        raise CommandError(f"I couldn't open {label}: {exc}") from exc

    log.info("opened %s in %s", url, _describe(browser))
    return CommandResult(
        speech=f"Opening {label} on {ctx.device_name}.",
        result={"url": url, "browser": browser.name if browser else "default"},
    )


def _join_meeting(ctx: CommandContext, args: dict) -> CommandResult:
    """Open the join link off the Outlook calendar.

    No Teams API is involved: the invitation carries its own join URL, and
    handing that to the browser is what the Teams client is registered to catch.
    """
    from .query import mailbox_section

    if not ctx.config.web.enabled:
        raise CommandError("Opening web pages is switched off on this PC.")

    which = str(args.get("which") or "auto").strip().lower()
    if which not in ("auto", "current", "next"):
        raise CommandError("Tell me whether to join the current meeting or the next one.")

    calendar = mailbox_section(ctx, "calendar")
    current, upcoming = calendar.get("current"), calendar.get("next")
    # A meeting already running wins: "join my meeting" during one is never a
    # request for the one after it.
    event = current if which in ("auto", "current") and current else None
    if event is None and which in ("auto", "next"):
        event = upcoming

    if event is None:
        raise CommandError("You don't have a meeting to join right now.")

    subject = event.get("subject") or "your meeting"
    url = event.get("join_url")
    if not url:
        where = f" It's in {event['location']}." if event.get("location") else ""
        raise CommandError(f"I can't find a join link for {subject}.{where}")

    browser = _browser_path(ctx, str(args.get("browser") or ""))
    try:
        _open_url(_clean_url(url), browser)
    except OSError as exc:
        raise CommandError(f"I couldn't open the link for {subject}: {exc}") from exc

    log.info("opened the join link for %r", subject)
    return CommandResult(
        speech=f"Joining {subject}.",
        result={
            "subject": subject,
            "provider": event.get("provider") or "",
            "start_ts": event.get("start_ts"),
            "url": url,
        },
    )


def _search(ctx: CommandContext, args: dict) -> CommandResult:
    if not ctx.config.web.enabled:
        raise CommandError("Opening web pages is switched off on this PC.")

    query = arg_str(args, "query").strip()
    if not query:
        raise CommandError("Tell me what to search for.")

    site = str(args.get("site") or "google").strip().lower()
    template = SEARCHES.get(site)
    if template is None:
        raise CommandError(
            f"I can't search {site}. I can search "
            f"{join_speech(sorted(SEARCHES), 'or')}."
        )

    url = template.format(q=urllib.parse.quote_plus(query))
    browser = _browser_path(ctx, str(args.get("browser") or ""))
    try:
        _open_url(url, browser)
    except OSError as exc:
        raise CommandError(f"I couldn't run that search: {exc}") from exc

    log.info("searched %s for %r", site, query)
    return CommandResult(
        speech=f"Searching {_pretty(site)} for {query} on {ctx.device_name}.",
        result={"site": site, "query": query, "url": url},
    )


def _sites(ctx: CommandContext, args: dict) -> CommandResult:
    names = sorted(_known_sites(ctx))
    return CommandResult(
        speech=f"I can open {join_speech(names[:8], 'and')}, and {len(names) - 8} more.",
        result={"sites": names, "searchable": sorted(SEARCHES)},
    )
