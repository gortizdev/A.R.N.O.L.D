"""Desktop interaction: toasts, clipboard, screen capture."""

from __future__ import annotations

import logging

from ..humanize import bytes_speech
from ..platform_win import clipboard as clip
from ..platform_win import screenshot as screen
from ..platform_win import toast
from .registry import (
    CommandContext,
    CommandError,
    CommandResult,
    Registry,
    arg_bool,
    arg_int,
    arg_str,
)

log = logging.getLogger(__name__)

# A clipboard is a common place for passwords to sit. Reading it aloud is a
# deliberate act, so the spoken reply summarises and the full text goes only in
# the structured result.
SPEAK_PREVIEW_CHARS = 200


def register_all(registry: Registry) -> None:
    # All of these are meaningless outside the logged-on session: a toast is
    # never shown, the clipboard is a different clipboard, and the screen is
    # blank. Marked so `exec` forwards them to the agent on the desktop.
    registry.register(
        "desktop.notify",
        _notify,
        "Show a Windows toast notification.",
        {"message": "body text", "title": "optional heading"},
        needs_desktop=True,
    )
    registry.register(
        "desktop.clipboard_get", _clipboard_get, "Read the clipboard.", needs_desktop=True
    )
    registry.register(
        "desktop.clipboard_set", _clipboard_set, "Replace the clipboard.",
        {"text": "new contents"}, needs_desktop=True,
    )
    registry.register(
        "desktop.screenshot",
        _screenshot,
        "Capture the screen to a PNG.",
        {"inline": "true to also return base64", "all_screens": "false for primary only"},
        needs_desktop=True,
    )
    registry.register(
        "desktop.dashboard",
        _dashboard,
        "Put the dashboard on screen: vitals, alerts and every command.",
        {"browser": "optional: firefox, chrome, edge... default is the system browser"},
        needs_desktop=True,
    )
    registry.register(
        "desktop.read_window",
        _read_window,
        "Read the text of the window in front, without taking a picture of it.",
        {
            "scope": "'window' (default), or 'focus' for just the box the caret is in",
            "max_chars": "cap on how much comes back",
        },
        needs_desktop=True,
    )


def _read_window(ctx: CommandContext, args: dict) -> CommandResult:
    """What is on screen, as text.

    Narrower than a screenshot - one window rather than every monitor - and it
    never leaves an image anywhere. The text still goes to the model when a
    voice session asks for it, so the deny list is checked before anything at
    all is read, and the text itself is never written to the log.
    """
    from ..platform_win import uia

    cfg = ctx.config.voice
    if not cfg.screen_text:
        raise CommandError("Reading the screen is switched off on this PC.")

    from ..platform_win.window import active_window

    def refuse_if_denied(window: dict) -> None:
        haystack = " ".join(
            str(window.get(key) or "") for key in ("process", "title")
        ).lower()
        for denied in cfg.screen_text_deny:
            if denied and str(denied).lower() in haystack:
                log.info("refused to read a window matching %r", denied)
                raise CommandError("I would rather not read that window.")

    refuse_if_denied(active_window())

    if not uia.available():
        log.info("reading the screen without comtypes; only classic windows will answer")

    try:
        result = uia.read_active_window(
            scope=str(args.get("scope") or "window"),
            max_chars=arg_int(
                args, "max_chars", cfg.screen_text_max_chars, minimum=100, maximum=20000
            ),
            timeout=cfg.screen_text_timeout_seconds,
        )
    except uia.UiaError as exc:
        raise CommandError(str(exc)) from exc

    # Again, against what was actually read. The window can change between
    # the check and the read, and `scope=focus` need not even be the window
    # that was vetted.
    refuse_if_denied(result)

    title = result.get("title") or "that window"
    # The count, never the text: this is somebody's screen.
    log.info("read %d characters from %s (%s)", result["chars"], title, result["how"])

    if not (result["text"] or "").strip():
        hint = ""
        if not uia.available():
            hint = f" {uia.INSTALL_HINT}"
        return CommandResult(
            speech=f"There's no text I can read in {title}.{hint}",
            result=result,
        )

    preview = result["text"][:300].replace("\n", " ").strip()
    return CommandResult(speech=f"{title} - {preview}", result=result)


def _notify(ctx: CommandContext, args: dict) -> CommandResult:
    message = arg_str(args, "message")
    title = args.get("title") or ctx.config.assistant_name()
    try:
        toast.notify(str(title), message)
    except Exception as exc:
        raise CommandError(f"I couldn't show the notification: {exc}") from exc
    return CommandResult(
        speech=f"Notification shown on {ctx.device_name}.",
        result={"title": str(title), "message": message},
    )


def _clipboard_get(ctx: CommandContext, args: dict) -> CommandResult:
    text, truncated = clip.read_clipboard()
    if not text:
        return CommandResult(
            speech=f"The clipboard on {ctx.device_name} is empty, or holds something that isn't text.",
            result={"text": "", "empty": True},
        )

    preview = text[:SPEAK_PREVIEW_CHARS]
    speech = f"The clipboard says: {preview}"
    if len(text) > SPEAK_PREVIEW_CHARS:
        speech = (
            f"The clipboard holds {len(text)} characters. It starts: {preview}"
        )
    return CommandResult(
        speech=speech,
        result={"text": text, "length": len(text), "truncated": truncated},
    )


def _clipboard_set(ctx: CommandContext, args: dict) -> CommandResult:
    text = args.get("text")
    if text is None:
        raise CommandError("Tell me what to put on the clipboard.")
    text = str(text)
    clip.write_clipboard(text)
    return CommandResult(
        speech=f"Copied {len(text)} characters to the clipboard on {ctx.device_name}.",
        result={"length": len(text)},
    )


def _screenshot(ctx: CommandContext, args: dict) -> CommandResult:
    inline = bool(arg_bool(args, "inline", False))
    all_screens = arg_bool(args, "all_screens", True)
    try:
        info = screen.capture(all_screens=bool(all_screens), as_base64=inline)
    except Exception as exc:
        raise CommandError(f"I couldn't capture the screen: {exc}") from exc

    encoded = info.get("png_base64")
    size_note = f" It's {bytes_speech(len(encoded) * 3 // 4)}." if isinstance(encoded, str) else ""
    return CommandResult(
        speech=f"Screenshot saved on {ctx.device_name}.{size_note}",
        result=info,
    )


def _dashboard(ctx: CommandContext, args: dict) -> CommandResult:
    """Open the dashboard, serving it first if nothing else already is.

    "Show me the dashboard" has to work whoever is asked - the voice session,
    Jarvis over SSH, or a shell - so the page is started on demand rather than
    being something the user has to have remembered to run.
    """
    from .. import runtime
    from ..ui.server import already_serving, ensure_serving
    from .web import _browser_path, _open_url

    if not ctx.config.ui.enabled:
        raise CommandError("The dashboard is switched off in the config on this PC.")

    # Serving it outlives the call. Usually something already is - the agent
    # runs at logon - and then this is only opening a browser. When nothing is,
    # only a process that will still be here can take it on.
    if not already_serving(ctx.config) and not runtime.IS_RESIDENT:
        raise CommandError(
            "Nothing is serving the dashboard, and I'm about to exit, so anything "
            "I started would go with me. Leave the agent running, or start it "
            "with computer dash assistant U I."
        )

    url, started = ensure_serving(ctx.config)
    try:
        _open_url(url, _browser_path(ctx, str(args.get("browser") or "")))
    except OSError as exc:
        raise CommandError(f"I couldn't open the dashboard: {exc}") from exc

    log.info("opened the dashboard at %s (started=%s)", url, started)
    return CommandResult(
        speech=f"The dashboard for {ctx.device_name} is on your screen.",
        result={"url": url, "started": started},
    )
