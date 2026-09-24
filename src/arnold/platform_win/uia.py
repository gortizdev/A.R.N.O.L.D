"""Reading the text of a window, rather than photographing it.

"What am I looking at" and "read this to me" have been answered until now by
screenshotting every monitor and sending the picture to a model. That works,
but it is the expensive way round: it costs an image upload per question, it
loses the text as text, and it cannot run at all when `voice.vision` is off.

Windows already knows what is on the screen. UI Automation is the accessibility
layer screen readers use, and it reaches inside Chrome, Electron, WPF and UWP
alike - which plain `WM_GETTEXT` cannot, since those draw their own controls
and tell Win32 nothing.

Three tiers, best first, so something useful comes back even on a bad day:

1. **TextPattern** - a browser page or an editor buffer as one string. This is
   the one that answers "read this article to me".
2. **The control tree** - names and values, breadth-first, capped. What a form
   or a settings page gives instead of a document.
3. **WM_GETTEXT** - the window's own text and its direct children, through
   plain ctypes. Blind to modern apps, but it needs no dependency at all, so
   it is what a machine without comtypes still gets.

Everything runs on a short-lived worker thread with a deadline: a UIA call is
a cross-process call, and an application that has stopped answering will
otherwise hang whoever asked.
"""

from __future__ import annotations

import ctypes
import gc
import logging
import queue
import threading
from ctypes import wintypes
from typing import Any

from . import IS_WINDOWS

log = logging.getLogger(__name__)

INSTALL_HINT = (
    'reading modern windows needs comtypes - uv pip install -e ".[uia]"'
)

# UIA constants, so nothing here depends on a generated wrapper's names.
UIA_TEXT_PATTERN_ID = 10014
UIA_VALUE_PATTERN_ID = 10002
UIA_IS_TEXT_PATTERN_AVAILABLE_ID = 30040
TREE_SCOPE_SUBTREE = 7

WM_GETTEXT = 0x000D
WM_GETTEXTLENGTH = 0x000E

# "Cannot change thread mode after it is set" - COM is already up on this
# thread in the other apartment, which is survivable.
RPC_E_CHANGED_MODE = -2147417850

# Give up on a window that is not pumping its messages rather than blocking.
SMTO_ABORTIFHUNG = 0x0002
WM_TIMEOUT_MS = 500

# A control tree can be enormous - a spreadsheet is tens of thousands of
# cells - and reading all of it would be slower than a screenshot and less
# useful than the first page of it.
MAX_NODES = 400
MAX_DEPTH = 12
# Text-bearing elements to compare before settling for the best so far. A
# page has a handful; a spreadsheet has thousands, and reading all of them
# would cost more than the screenshot this replaces.
MAX_TEXT_CANDIDATES = 40


class UiaError(RuntimeError):
    """The screen could not be read, with a reason worth speaking aloud."""


def _import_comtypes():
    """Import comtypes with COM set to multi-threaded.

    comtypes initialises COM for a thread the first time it is used, and its
    default is apartment-threaded. An STA client has to pump messages, and
    this worker thread has no message loop, so a cross-process UIA call from
    one can wedge. The flag has to be set before comtypes is first imported
    anywhere in the process, hence the check rather than a plain assignment.
    """
    import sys

    if "comtypes" not in sys.modules:
        # 0 is COINIT_MULTITHREADED and 2 is COINIT_APARTMENTTHREADED - the
        # opposite way round from what the names suggest. Getting this
        # backwards puts the importing thread into a pump-less apartment for
        # the life of the process, which is the wedge this exists to avoid.
        sys.coinit_flags = 0
    import comtypes

    return comtypes


def available() -> bool:
    """Whether the good tiers can run at all. Never raises."""
    if not IS_WINDOWS:
        return False
    try:
        _import_comtypes()
    except Exception:
        return False
    return True


_warmed = False


def _warm_up() -> None:
    """Generate the type-library wrapper once, on the caller's thread.

    It happens once per machine and is far slower than a read, so leaving it
    inside the worker's deadline makes the first read after install report
    that the window is not answering, which it is. Once per process, and
    never fatal.
    """
    global _warmed
    if _warmed or not available():
        return
    _warmed = True
    try:
        _uia_module()
    except Exception as exc:
        log.debug("could not prepare the UI Automation wrapper: %s", exc)


def read_active_window(
    scope: str = "window",
    max_chars: int = 4000,
    timeout: float = 4.0,
) -> dict[str, Any]:
    """The foreground window's text.

    `scope` is "window" for the whole thing or "focus" for just the control
    the caret is in. The result carries `how` - "text_pattern", "tree" or
    "wm_gettext" - so a disappointing answer can be explained rather than
    guessed at.
    """
    if not IS_WINDOWS:
        raise UiaError("reading the screen only works on Windows.")

    from .window import active_window, foreground_hwnd

    hwnd = foreground_hwnd()
    if not hwnd:
        raise UiaError("There's no window in the foreground.")
    window = active_window()

    _warm_up()

    answer: queue.Queue = queue.Queue(maxsize=1)

    def work() -> None:
        try:
            answer.put(_read(hwnd, scope, max_chars))
        except BaseException as exc:  # carried across, never raised in here
            answer.put(exc)

    # A daemon thread with a deadline: if the window never answers we leak one
    # thread rather than freezing the agent, and the leak is logged.
    worker = threading.Thread(target=work, name="uia-read", daemon=True)
    worker.start()
    try:
        result = answer.get(timeout=max(0.5, float(timeout)))
    except queue.Empty:
        log.warning("gave up reading %r after %.1fs", window.get("title"), timeout)
        raise UiaError("That window isn't answering.") from None
    if isinstance(result, BaseException):
        raise UiaError(f"I couldn't read that window: {result}") from result

    text, how = result
    truncated = len(text) > max_chars
    if truncated:
        text = text[:max_chars] + " ... (truncated)"
    return {
        "title": window.get("title"),
        "process": window.get("process"),
        "pid": window.get("pid"),
        "text": text,
        "chars": len(text),
        "truncated": truncated,
        "how": how,
    }


def _read(hwnd: int, scope: str, max_chars: int) -> tuple[str, str]:
    """The three tiers, on the worker thread."""
    if available():
        try:
            return _read_with_uia(hwnd, scope, max_chars)
        except Exception as exc:
            log.debug("UI Automation could not read the window: %s", exc)
    return _text_via_wm_gettext(hwnd), "wm_gettext"


def _read_with_uia(hwnd: int, scope: str, max_chars: int) -> tuple[str, str]:
    comtypes = _import_comtypes()
    import comtypes.client

    # COM has to be initialised on this thread before anything is created.
    # If something else on this thread already chose an apartment, that is a
    # RPC_E_CHANGED_MODE we can carry on through - COM is initialised either
    # way, which is all these synchronous calls need.
    try:
        comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
    except OSError as exc:
        if getattr(exc, "winerror", 0) != RPC_E_CHANGED_MODE:
            raise
        # Something already chose an apartment on this thread. COM is up
        # either way, which is all these synchronous calls need - but if it
        # is an STA with no message pump a hung application can wedge us, so
        # say so rather than hiding it.
        log.warning(
            "COM was already initialised on this thread in another apartment; "
            "a window that stops answering may take the full timeout"
        )
    try:
        automation = comtypes.client.CreateObject(
            "{ff48dba4-60ef-4201-aa87-54103eef594e}",  # CUIAutomation
            interface=_uia_interface(),
        )
        element = (
            automation.GetFocusedElement()
            if scope == "focus"
            else automation.ElementFromHandle(hwnd)
        )
        if element is None:
            raise UiaError("that window has nothing to read.")

        text = _text_via_pattern(element, max_chars)
        how = "text_pattern"
        if not text.strip():
            # A browser or an editor keeps its content in a document element
            # well inside the window, and the frame itself exposes no text at
            # all. This is the difference between reading the article and
            # reading the menu bar.
            text = _best_inner_text(automation, element, max_chars)
        if not text.strip():
            text = _text_via_tree(element, max_chars, automation.ControlViewWalker)
            how = "tree"
        return text, how
    finally:
        # Deliberately no CoUninitialize.
        #
        # comtypes releases interface pointers when they are collected, and
        # anything collected after CoUninitialize is a release into an
        # apartment that no longer exists - which segfaults the whole process,
        # agent and all. Dropping the references and collecting here means the
        # releases happen while COM is still up; the thread then ends, and
        # Windows tears its apartment down with it.
        automation = element = None
        gc.collect()


def _uia_module():
    """The generated wrapper for UIAutomationCore, built once and cached by
    comtypes itself."""
    import comtypes.client

    return comtypes.client.GetModule("UIAutomationCore.dll")


def _uia_interface():
    return _uia_module().IUIAutomation


def _best_inner_text(automation, element, max_chars: int) -> str:
    """The richest text in the window.

    Not the *first* element that can hand over text: in a browser that is the
    address bar, which is a URL where the user wanted the page. So the
    candidates are gathered and the longest wins, which is the document
    wherever a document exists.
    """
    try:
        condition = automation.CreatePropertyCondition(
            UIA_IS_TEXT_PATTERN_AVAILABLE_ID, True
        )
        found = element.FindAll(TREE_SCOPE_SUBTREE, condition)
    except Exception as exc:
        log.debug("no element with a text pattern: %s", exc)
        return ""

    best = ""
    try:
        count = min(int(found.Length), MAX_TEXT_CANDIDATES)
    except Exception:
        return ""
    for index in range(count):
        try:
            text = _text_via_pattern(found.GetElement(index), max_chars)
        except Exception:
            continue
        if len(text) > len(best):
            best = text
            if len(best) >= max_chars:
                break  # already as much as the caller asked for
    return best


def _text_via_pattern(element, max_chars: int) -> str:
    """A document as one string: the browser page, the editor buffer."""
    try:
        pattern = element.GetCurrentPattern(UIA_TEXT_PATTERN_ID)
        if not pattern:
            return ""
        text_pattern = pattern.QueryInterface(_uia_module().IUIAutomationTextPattern)
        return text_pattern.DocumentRange.GetText(max_chars) or ""
    except Exception as exc:
        log.debug("no text pattern: %s", exc)
        return ""


def _text_via_tree(element, max_chars: int, walker=None) -> str:
    """Names and values, breadth-first. What a form gives instead of prose."""
    if walker is None:
        import comtypes.client

        automation = comtypes.client.CreateObject(
            "{ff48dba4-60ef-4201-aa87-54103eef594e}", interface=_uia_interface()
        )
        walker = automation.ControlViewWalker

    seen: list[str] = []
    unique: set[str] = set()
    total = 0
    frontier = [(element, 0)]
    visited = 0

    while frontier and visited < MAX_NODES and total < max_chars:
        node, depth = frontier.pop(0)
        visited += 1
        for value in (_name_of(node), _value_of(node)):
            value = (value or "").strip()
            if value and value not in unique:
                unique.add(value)
                seen.append(value)
                total += len(value) + 1
        if depth >= MAX_DEPTH:
            continue
        try:
            child = walker.GetFirstChildElement(node)
        except Exception:
            child = None
        while child is not None and len(frontier) + visited < MAX_NODES:
            frontier.append((child, depth + 1))
            try:
                child = walker.GetNextSiblingElement(child)
            except Exception:
                break

    return "\n".join(seen)


def _name_of(element) -> str:
    try:
        return element.CurrentName or ""
    except Exception:
        return ""


def _value_of(element) -> str:
    try:
        pattern = element.GetCurrentPattern(UIA_VALUE_PATTERN_ID)
        if not pattern:
            return ""
        value_pattern = pattern.QueryInterface(_uia_module().IUIAutomationValuePattern)
        return value_pattern.CurrentValue or ""
    except Exception:
        return ""


def _text_via_wm_gettext(hwnd: int) -> str:
    """The last tier: the window's own text and its direct children.

    Enough for Notepad, dialogs and anything else drawn with real Win32
    controls, and completely blind to Chrome and friends - which is why it is
    the fallback and not the plan.
    """
    user32 = ctypes.windll.user32
    parts: list[str] = []

    def text_of(handle: int) -> str:
        # SendMessageTimeoutW, never SendMessageW: a plain send does not
        # return while the target window is hung, and this is the tier that
        # runs on a machine with no comtypes - so it is the likeliest path,
        # and a permanently blocked thread per call is not acceptable.
        try:
            result = ctypes.c_ulong()
            if not user32.SendMessageTimeoutW(
                wintypes.HWND(handle),
                WM_GETTEXTLENGTH,
                0,
                0,
                SMTO_ABORTIFHUNG,
                WM_TIMEOUT_MS,
                ctypes.byref(result),
            ):
                return ""
            length = int(result.value)
            if not length:
                return ""
            buf = ctypes.create_unicode_buffer(length + 1)
            if not user32.SendMessageTimeoutW(
                wintypes.HWND(handle),
                WM_GETTEXT,
                length + 1,
                ctypes.byref(buf),
                SMTO_ABORTIFHUNG,
                WM_TIMEOUT_MS,
                ctypes.byref(result),
            ):
                return ""
            return buf.value or ""
        except Exception:
            return ""

    parts.append(text_of(hwnd))

    proto = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def each(child, _param):
        if len(parts) >= MAX_NODES:
            return False
        value = text_of(child).strip()
        if value and value not in parts:
            parts.append(value)
        return True

    try:
        user32.EnumChildWindows(hwnd, proto(each), 0)
    except Exception as exc:
        log.debug("could not walk the child windows: %s", exc)

    return "\n".join(part for part in parts if part.strip())
