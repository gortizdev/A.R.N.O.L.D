"""Mail and calendar, read from the Outlook desktop client over COM.

Why COM and not Microsoft Graph: Graph wants an Azure AD app registration and,
on a work tenant, an administrator's consent for `Mail.Read` and
`Calendars.Read`. Outlook is already signed in as the user on this machine and
MAPI hands over the same mailbox with no token to store and nobody to ask. The
trade is that this only works on the logged-on desktop, with classic Outlook
installed - which is exactly where the agent already runs.

Teams needs no Teams API. An invitation lands in the Outlook calendar as an
appointment carrying its own join URL, so the calendar answers "what's coming
up" and the join link arrives with it.

Two things shape the design:

* **COM is too expensive for the tick.** Every call crosses a process boundary
  and needs an initialised apartment per thread, so one worker polls on its own
  cadence and readers take a cached copy. A stalled Outlook slows the poll, not
  telemetry.
* **The cache holds absolute timestamps, never "minutes until".** That figure
  is recomputed on every snapshot, so a meeting alert fires on the minute it is
  due rather than on whichever poll happened to notice.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime, timedelta
from typing import Any

from ..config import OutlookConfig

log = logging.getLogger(__name__)

# OlDefaultFolders. Numeric because a late-bound Dispatch never loads the type
# library that names them.
_FOLDER_INBOX = 6
_FOLDER_CALENDAR = 9

# OlMeetingStatus: cancelled as organiser, and cancelled as attendee.
_MEETING_CANCELLED = (5, 7)
# OlResponseStatus: declined.
_RESPONSE_DECLINED = 4
# OlImportance: high.
_IMPORTANCE_HIGH = 2

# Enough of the body to hold a join link and its surrounding block. Meeting
# bodies can run to whole email threads, and none of that is wanted here.
_BODY_SCAN_CHARS = 4000

# Join links, most specific first. Teams is the reason this exists; the others
# cost one regex each and mean "join my meeting" still works when the invite
# came from somebody else's organisation.
_JOIN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("teams", re.compile(r"https://teams\.microsoft\.com/l/meetup-join/[^\s<>\"'\]]+")),
    ("teams", re.compile(r"https://teams\.microsoft\.com/meet/[^\s<>\"'\]]+")),
    ("teams", re.compile(r"https://teams\.live\.com/meet/[^\s<>\"'\]]+")),
    ("zoom", re.compile(r"https://[\w.-]*zoom\.us/j/[^\s<>\"'\]]+")),
    ("meet", re.compile(r"https://meet\.google\.com/[a-z0-9-]+")),
    ("webex", re.compile(r"https://[\w.-]*webex\.com/[^\s<>\"'\]]+")),
)


class OutlookUnavailable(RuntimeError):
    """Outlook could not be reached. Carries a sentence fit to speak."""


def _to_ts(value: Any) -> float | None:
    """A COM date to a POSIX timestamp, read as local time.

    Rebuilt field by field rather than handed straight to `.timestamp()`:
    pywintypes returns its own datetime subclass carrying a `TimeZoneInfo`, and
    Outlook reports appointment times in the machine's local zone anyway. Taking
    the fields and letting the standard library apply the local offset is the
    one reading that is right on both counts.
    """
    if value is None:
        return None
    try:
        naive = datetime(
            value.year, value.month, value.day, value.hour, value.minute, value.second
        )
    except (AttributeError, ValueError, TypeError):
        return None
    try:
        return naive.timestamp()
    except (OverflowError, OSError, ValueError):
        return None


# .NET date tokens to strftime. Day and month *names* map to nothing: some
# locales put them in the short date, and Restrict does not need them to bound a
# range.
_DATE_TOKENS = {
    "yyyy": "%Y",
    "yy": "%y",
    "MMMM": "%m",
    "MMM": "%m",
    "MM": "%m",
    "M": "%m",
    "dddd": "",
    "ddd": "",
    "dd": "%d",
    "d": "%d",
}
# One pass, longest token first. Replacing them one at a time would have the
# later rules chew on the earlier rules' output: 'dd' -> '%d', and then the 'd'
# rule finds the 'd' it just wrote and turns it into '%%d'.
_DATE_TOKEN_RE = re.compile("|".join(sorted(_DATE_TOKENS, key=len, reverse=True)))


def date_format_from_pattern(pattern: str) -> str:
    """A .NET date pattern ('dd/MM/yyyy') as a strftime one ('%d/%m/%Y').

    Falls back to month/day/year if the result is not a usable date, which is
    what Outlook assumes when it cannot tell.
    """
    pattern = _DATE_TOKEN_RE.sub(lambda m: _DATE_TOKENS[m.group(0)], pattern)
    pattern = re.sub(r"[,\s]+", " ", pattern).strip(" -/.")

    if "%d" in pattern and "%m" in pattern and ("%Y" in pattern or "%y" in pattern):
        return pattern
    return "%m/%d/%Y"


def _short_date_format() -> str:
    """A strftime format matching this user's Windows short-date setting.

    Outlook's `Restrict` parses dates in the *user's* locale, so '03/04/2026' is
    March 4th on one machine and April 3rd on another. Guessing wrong does not
    raise - it silently reads the wrong window - so the order comes from the
    same registry value Windows itself uses.
    """
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Control Panel\International") as handle:
            pattern = str(winreg.QueryValueEx(handle, "sShortDate")[0])
    except Exception as exc:  # no registry, no such value, not Windows
        log.debug("could not read sShortDate (%s); assuming month/day/year", exc)
        return "%m/%d/%Y"
    return date_format_from_pattern(pattern)


def _find_join_url(text: str) -> tuple[str, str]:
    """(provider, url) for the first join link in `text`, or ('', '')."""
    for provider, pattern in _JOIN_PATTERNS:
        found = pattern.search(text)
        if found:
            # Bodies arrive HTML-escaped often enough to be worth undoing, and a
            # trailing '>' from a wrapped link is never part of the URL.
            return provider, found.group(0).replace("&amp;", "&").rstrip(">")
    return "", ""


class OutlookProbe:
    """Cached view of the inbox and calendar.

    `start()` runs a worker that polls; without it, `snapshot()` polls inline the
    first time it is asked and then no more often than `poll_seconds`. The agent
    starts the worker, a one-shot `exec` does not and pays one COM round trip.
    """

    def __init__(self, config: OutlookConfig) -> None:
        self._config = config
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        # Written by the poller, read by everyone else, always as a whole dict
        # so a reader never sees half an update.
        self._mail: dict[str, Any] = {"available": False, "error": "not checked yet"}
        self._calendar: dict[str, Any] = {"available": False, "error": "not checked yet"}
        self._last_poll = 0.0

        self._namespace: Any = None
        self._date_format = ""
        # So a machine without Outlook logs the reason once, not every poll.
        self._reported: str = ""

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        if not self._config.enabled or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="outlook-poll")
        self._thread.start()
        log.info(
            "polling Outlook every %.0fs (%s)",
            self._config.poll_seconds,
            ", ".join(
                part
                for part in (
                    "mail" if self._config.mail else "",
                    "calendar" if self._config.calendar else "",
                )
                if part
            )
            or "nothing enabled",
        )

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2.0)
        self._thread = None

    def _run(self) -> None:
        import pythoncom

        # Every thread touching COM needs its own apartment.
        pythoncom.CoInitialize()
        try:
            while not self._stop.is_set():
                started = time.monotonic()
                try:
                    self._poll()
                except Exception:
                    log.exception("Outlook poll failed; continuing")
                interval = max(10.0, self._config.poll_seconds)
                self._stop.wait(max(1.0, interval - (time.monotonic() - started)))
        finally:
            self._release()
            pythoncom.CoUninitialize()

    # -- reading ------------------------------------------------------------

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """The `mail` and `calendar` sections of a telemetry snapshot."""
        if not self._config.enabled:
            return {}

        if self._thread is None:
            self._poll_inline()

        with self._lock:
            mail = dict(self._mail)
            calendar = dict(self._calendar)

        now = time.time()
        # Derived here, not at poll time, so a rule watching for a meeting five
        # minutes out fires five minutes out.
        if calendar.get("available"):
            calendar = self._with_derived_times(calendar, now)
        if mail.get("available"):
            received = (mail.get("latest") or {}).get("received_ts")
            mail["seconds_since_latest"] = round(now - received, 1) if received else None

        section: dict[str, dict[str, Any]] = {}
        if self._config.mail:
            section["mail"] = mail
        if self._config.calendar:
            section["calendar"] = calendar
        return section

    def _poll_inline(self) -> None:
        """One COM round trip on the caller's thread, rate-limited.

        This is the `exec query.mail` path: no agent is running, so there is no
        worker and nobody else is going to fill the cache.
        """
        if time.monotonic() - self._last_poll < max(10.0, self._config.poll_seconds):
            return
        try:
            import pythoncom
        except ImportError:
            self._fail("pywin32 is not installed, so I can't read Outlook.")
            return

        # Something else in this process may already have initialised the
        # apartment (pycaw does, for absolute volume control). Initialising twice
        # is fine; uninitialising an apartment we did not open is not.
        opened = True
        try:
            pythoncom.CoInitialize()
        except Exception as exc:
            log.debug("CoInitialize declined (%s); using the existing apartment", exc)
            opened = False

        try:
            self._poll()
        except Exception as exc:
            log.debug("inline Outlook poll failed: %s", exc)
        finally:
            # Drop the cached namespace: it belongs to an apartment that may be
            # about to go away, and reusing it from another thread would fail.
            self._release()
            if opened:
                pythoncom.CoUninitialize()

    @staticmethod
    def _with_derived_times(calendar: dict[str, Any], now: float) -> dict[str, Any]:
        events = calendar.get("events") or []
        # Sorted here rather than trusting the poll to have done it: "the next
        # meeting" being whichever one happened to come back first is the kind of
        # bug that only shows up on the day it matters.
        upcoming = sorted(
            (e for e in events if (e.get("start_ts") or 0) > now),
            key=lambda e: e["start_ts"],
        )
        current = next(
            (
                e
                for e in events
                if (e.get("start_ts") or 0) <= now < (e.get("end_ts") or 0)
                and not e.get("all_day")
            ),
            None,
        )

        nxt = upcoming[0] if upcoming else None
        calendar["next"] = nxt
        calendar["current"] = current
        calendar["in_progress"] = current is not None
        calendar["minutes_until_next"] = (
            round((nxt["start_ts"] - now) / 60.0, 1) if nxt else None
        )
        calendar["upcoming_count"] = len(upcoming)
        return calendar

    # -- polling ------------------------------------------------------------

    def _release(self) -> None:
        self._namespace = None

    def _fail(self, reason: str) -> None:
        with self._lock:
            self._mail = {"available": False, "error": reason}
            self._calendar = {"available": False, "error": reason}
        if self._reported != reason:
            log.warning("%s", reason)
            self._reported = reason
        self._release()

    @staticmethod
    def outlook_is_running() -> bool:
        try:
            import psutil

            return any(
                (proc.info["name"] or "").lower() == "outlook.exe"
                for proc in psutil.process_iter(["name"])
            )
        except Exception:
            return False

    def _explain(self, exc: Any) -> str:
        """Turn a COM failure into a sentence that says what to do about it.

        Worth the trouble because the useful cases are indistinguishable from the
        error code alone. A running OUTLOOK.EXE that cannot be attached to is
        usually one that never finished starting - it registers itself for
        automation only once it has a mailbox open, so an Outlook sitting on its
        'Add Account' dialog looks exactly like an Outlook that is not there.
        """
        detail = str(exc.args[1]) if len(getattr(exc, "args", ())) > 1 else str(exc)

        if self._config.require_running and not self.outlook_is_running():
            return "Outlook isn't running on this PC, so I can't check mail or the calendar."
        if self._config.require_running:
            return (
                "Outlook is running but won't answer yet. If it's asking you to add an "
                "account, mail and the calendar stay unreadable until it has one."
            )
        return (
            f"I couldn't start Outlook on this PC ({detail}). If it has never been signed in, "
            f"open it once and add your account."
        )

    def _connect(self) -> Any:
        """A MAPI namespace, reusing the last one while it still answers."""
        if self._namespace is not None:
            return self._namespace

        try:
            import pythoncom
            import win32com.client
        except ImportError as exc:
            raise OutlookUnavailable(
                "pywin32 is not installed, so I can't read Outlook."
            ) from exc

        try:
            if self._config.require_running:
                # Attach to the Outlook the user already has open. Dispatch would
                # start one instead, and an agent launching Outlook by itself is a
                # surprise nobody asked for.
                app = win32com.client.GetActiveObject("Outlook.Application")
            else:
                app = win32com.client.Dispatch("Outlook.Application")
            namespace = app.GetNamespace("MAPI")
            # Cheap call that fails loudly if the connection is not really usable:
            # an Outlook with no account configured hands back an Application that
            # cannot actually reach a mailbox.
            namespace.GetDefaultFolder(_FOLDER_INBOX)
        except pythoncom.com_error as exc:
            raise OutlookUnavailable(self._explain(exc)) from exc
        except Exception as exc:
            raise OutlookUnavailable(f"I couldn't reach Outlook on this PC ({exc}).") from exc

        self._namespace = namespace
        return namespace

    def _poll(self) -> None:
        self._last_poll = time.monotonic()
        try:
            namespace = self._connect()
        except OutlookUnavailable as exc:
            self._fail(str(exc))
            return

        if self._reported:
            log.info("Outlook is reachable again")
            self._reported = ""

        mail = self._read_safe("mail", self._read_mail, namespace) if self._config.mail else {}
        calendar = (
            self._read_safe("calendar", self._read_calendar, namespace)
            if self._config.calendar
            else {}
        )

        with self._lock:
            if self._config.mail:
                self._mail = mail
            if self._config.calendar:
                self._calendar = calendar

    def _read_safe(self, what: str, probe: Any, namespace: Any) -> dict[str, Any]:
        """Run one half of the poll; a failure marks that half unavailable.

        The inbox and the calendar fail independently - a mailbox still syncing,
        a shared calendar that went away - and one of them working is worth more
        than both of them being reported as broken.
        """
        try:
            return probe(namespace)
        except Exception as exc:
            log.warning("reading the Outlook %s failed: %s", what, exc)
            # The namespace may be the thing that broke; take a fresh one next time.
            self._release()
            return {"available": False, "error": f"I couldn't read the Outlook {what} ({exc})."}

    # -- the inbox ----------------------------------------------------------

    def _ignored(self, sender: str, subject: str) -> bool:
        lowered_sender, lowered_subject = sender.lower(), subject.lower()
        if any(n.lower() in lowered_sender for n in self._config.ignore_senders if n):
            return True
        return any(n.lower() in lowered_subject for n in self._config.ignore_subjects if n)

    def _read_mail(self, namespace: Any) -> dict[str, Any]:
        inbox = namespace.GetDefaultFolder(_FOLDER_INBOX)
        items = inbox.Items

        # Boolean and numeric Restrict clauses carry no dates, so unlike the
        # calendar they need no locale handling.
        unread_items = items.Restrict("[Unread] = true")
        unread = int(unread_items.Count)

        important = 0
        if unread:
            try:
                important = int(
                    items.Restrict(f"[Unread] = true AND [Importance] = {_IMPORTANCE_HIGH}").Count
                )
            except Exception as exc:
                log.debug("counting high-importance unread failed: %s", exc)

        latest = self._newest_worth_mentioning(items)

        return {
            "available": True,
            "error": None,
            "unread": unread,
            "unread_important": important,
            "latest": latest,
            "folder": str(getattr(inbox, "Name", "Inbox")),
            "checked_ts": time.time(),
        }

    def _newest_worth_mentioning(self, items: Any) -> dict[str, Any] | None:
        """The most recent inbox item that is not on an ignore list.

        Sorting is left to Outlook, and only a handful of items are walked: the
        ignore lists exist to skip a newsletter, not to page through the mailbox.
        """
        ordered = items
        try:
            # Sort mutates the collection, so work on a copy of the reference.
            ordered.Sort("[ReceivedTime]", True)
        except Exception as exc:
            log.debug("sorting the inbox failed: %s", exc)

        item = ordered.GetFirst()
        for _ in range(max(1, self._config.scan_messages)):
            if item is None:
                break

            sender = str(getattr(item, "SenderName", "") or "")
            subject = str(getattr(item, "Subject", "") or "")
            if not self._ignored(sender, subject):
                received = _to_ts(getattr(item, "ReceivedTime", None))
                return {
                    # SenderName rather than SenderEmailAddress on purpose: the
                    # address is one of the properties Outlook's object model
                    # guard can put a consent prompt in front of, and a display
                    # name is what gets read aloud anyway.
                    "from": sender or "someone",
                    "subject": subject if self._config.include_subjects else "",
                    "received_ts": received,
                    "unread": bool(getattr(item, "UnRead", False)),
                    "important": int(getattr(item, "Importance", 1) or 1) == _IMPORTANCE_HIGH,
                }
            item = ordered.GetNext()
        return None

    # -- the calendar -------------------------------------------------------

    def _read_calendar(self, namespace: Any) -> dict[str, Any]:
        folder = namespace.GetDefaultFolder(_FOLDER_CALENDAR)
        items = folder.Items

        # Order matters: IncludeRecurrences only expands a series once the
        # collection is sorted by start, and Restrict has to come after both or
        # the expansion is discarded.
        items.IncludeRecurrences = True
        items.Sort("[Start]")

        if not self._date_format:
            self._date_format = _short_date_format()

        now = datetime.now()
        # Bounded by whole days rather than by the hour: only the date order has
        # to survive the locale round trip, and the real window is applied below
        # against the timestamps Outlook hands back.
        window_start = (now - timedelta(hours=1)).strftime(self._date_format)
        window_end = (now + timedelta(hours=self._config.lookahead_hours, days=1)).strftime(
            self._date_format
        )
        restricted = items.Restrict(f"[Start] >= '{window_start}' AND [Start] < '{window_end}'")

        horizon = now.timestamp() + self._config.lookahead_hours * 3600
        today_ends = datetime(now.year, now.month, now.day).timestamp() + 86400

        events: list[dict[str, Any]] = []
        today = 0
        item = restricted.GetFirst()
        # Hard cap: a recurring series with no end date expands for as long as
        # anyone is willing to walk it.
        for _ in range(max(1, self._config.max_events)):
            if item is None:
                break
            event = self._describe_event(item, detail=len(events) < self._config.detail_events)
            item = restricted.GetNext()
            if event is None:
                continue
            if event["start_ts"] is None or event["end_ts"] is None:
                continue
            if event["start_ts"] > horizon:
                break
            if event["start_ts"] < today_ends:
                today += 1
            events.append(event)

        events.sort(key=lambda e: e["start_ts"])
        return {
            "available": True,
            "error": None,
            "events": events,
            "today_count": today,
            "checked_ts": time.time(),
        }

    def _describe_event(self, item: Any, *, detail: bool) -> dict[str, Any] | None:
        """One appointment, or None if it should not be mentioned at all."""
        try:
            if int(getattr(item, "MeetingStatus", 0) or 0) in _MEETING_CANCELLED:
                return None
            # A meeting already declined is not a meeting to be reminded about.
            if int(getattr(item, "ResponseStatus", 0) or 0) == _RESPONSE_DECLINED:
                return None
        except (TypeError, ValueError):
            pass

        all_day = bool(getattr(item, "AllDayEvent", False))
        if all_day and not self._config.include_all_day:
            return None

        event: dict[str, Any] = {
            "subject": str(getattr(item, "Subject", "") or "an untitled meeting"),
            "start_ts": _to_ts(getattr(item, "Start", None)),
            "end_ts": _to_ts(getattr(item, "End", None)),
            "all_day": all_day,
            "location": str(getattr(item, "Location", "") or ""),
            "organizer": str(getattr(item, "Organizer", "") or ""),
            "provider": "",
            "join_url": "",
        }
        if event["start_ts"] is not None and event["end_ts"] is not None:
            event["minutes"] = round((event["end_ts"] - event["start_ts"]) / 60.0)

        if detail:
            # Only the events that might actually be announced pay for a body
            # read; the rest are just there to be counted.
            body = ""
            try:
                body = str(getattr(item, "Body", "") or "")[:_BODY_SCAN_CHARS]
            except Exception as exc:
                log.debug("reading a meeting body failed: %s", exc)
            provider, url = _find_join_url(f"{event['location']}\n{body}")
            if not provider and "teams meeting" in event["location"].lower():
                # An invite whose body was stripped by a mail rule still says
                # where it is; there is just no link to hand back.
                provider = "teams"
            event["provider"] = provider
            event["join_url"] = url

        return event
