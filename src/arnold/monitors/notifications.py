"""Work notifications: what Teams and Outlook just put on screen.

Neither the new Outlook nor Teams exposes anything a local process can read -
no COM, no file, no socket - and Microsoft Graph wants an app registration and
a tenant administrator's consent. But both of them raise Windows toasts, and
Windows keeps every toast in the notification centre, where the
`UserNotificationListener` API hands them to any app the user has allowed.
That is the whole trick: the assistant reads the same notifications the user
sees, with the same words, and needs nobody's permission but theirs.

What this gives up: only what the toast says. A Teams toast is the sender and
the first line; an Outlook toast is the sender, the subject and a preview. It
is enough to say "Jane is asking about the quarterly numbers", which is the
thing worth interrupting for, and no more than would show on the lock screen.

Two things shape the design:

* **WinRT is not for the tick.** The listener is asynchronous and wants an
  event loop, so one worker thread owns a loop and polls on its own cadence;
  readers take a cached copy, as the Outlook probe does.
* **What was already there is not news.** The first poll records whatever is
  in the notification centre without announcing it, or a restart would read
  the morning's backlog aloud.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from ..config import NotificationsConfig

log = logging.getLogger(__name__)

# App user model ids of the clients that matter, matched as substrings so a
# packaging change (Store vs MSIX, a new publisher hash) does not silently
# stop them being recognised. Configurable; these are the defaults.
DEFAULT_APPS: dict[str, str] = {
    "Teams": "MSTeams",
    "Outlook": "Microsoft.OutlookForWindows",
}

# A raw toast as the listener adapter hands it over: id, app user model id,
# the app's display name, its text lines, and when it was raised.
RawToast = tuple[int, str, str, list[str], float]


class NotificationsUnavailable(RuntimeError):
    """The listener cannot be used; the message says what to do about it."""


@dataclass(slots=True)
class WorkNotification:
    id: int
    app: str  # the configured label: "Teams", "Outlook"
    title: str  # who it is from, as the toast's first line
    body: str  # the message or subject, as the second
    extra: list[str] = field(default_factory=list)  # any further lines
    ts: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "app": self.app,
            "title": self.title,
            "body": self.body,
            "extra": list(self.extra),
            "ts": self.ts,
        }

    def speech(self, include_text: bool = True) -> str:
        """One sentence, in the shape the toast would be read aloud."""
        who = self.title or "someone"
        body = self.body.strip()
        if self.app == "Teams":
            lead = f"Teams message from {who}"
        elif self.app == "Outlook":
            lead = f"Mail from {who}"
        else:
            lead = f"{self.app} from {who}"
        if include_text and body:
            joiner = ", " if self.app == "Outlook" else ": "
            return f"{lead}{joiner}{body}"
        return lead


def classify(aumid: str, display_name: str, apps: dict[str, str]) -> str | None:
    """Which configured app a toast belongs to, or None for anything else."""
    haystack = f"{aumid} {display_name}".lower()
    for label, needle in apps.items():
        if needle and needle.lower() in haystack:
            return label
    return None


def parse(raw: RawToast, apps: dict[str, str]) -> WorkNotification | None:
    toast_id, aumid, display_name, texts, ts = raw
    app = classify(aumid or "", display_name or "", apps)
    if app is None:
        return None
    lines = [" ".join(str(t).split()) for t in texts if str(t).strip()]
    return WorkNotification(
        id=int(toast_id),
        app=app,
        title=lines[0] if lines else "",
        body=lines[1] if len(lines) > 1 else "",
        extra=lines[2:],
        ts=float(ts or time.time()),
    )


def ignored(note: WorkNotification, config: NotificationsConfig) -> bool:
    """Substring matches against who it is from and what it says."""
    title = note.title.lower()
    text = f"{note.body} {' '.join(note.extra)}".lower()
    if any(s.lower() in title for s in config.ignore_senders if s):
        return True
    return any(s.lower() in text for s in config.ignore_subjects if s)


# -- the listener -----------------------------------------------------------


class WinRtListener:
    """The real thing: a thin adapter over `UserNotificationListener`.

    Everything WinRT lives here so the watch above it can be exercised with a
    fake. Import is deferred because the winrt packages are an optional extra.
    """

    def __init__(self) -> None:
        import asyncio

        self._loop = asyncio.new_event_loop()
        try:
            from winrt.windows.ui.notifications import (
                KnownNotificationBindings,
                NotificationKinds,
            )
            from winrt.windows.ui.notifications.management import (
                UserNotificationListener,
                UserNotificationListenerAccessStatus,
            )
        except ImportError as exc:  # pragma: no cover - depends on the extra
            raise NotificationsUnavailable(
                "the winrt packages are not installed - "
                "pip install 'arnold[notifications]'"
            ) from exc
        self._kinds = NotificationKinds
        self._binding = KnownNotificationBindings.toast_generic
        self._status = UserNotificationListenerAccessStatus
        self._listener = UserNotificationListener.current
        self._granted = False

    def _ensure_access(self) -> None:
        if self._granted:
            return
        status = self._loop.run_until_complete(self._listener.request_access_async())
        if status != self._status.ALLOWED:
            raise NotificationsUnavailable(
                "Windows has not allowed notification access - turn on "
                "Settings > Privacy > Notifications > 'Let apps access your notifications'"
            )
        self._granted = True

    def read(self) -> list[RawToast]:
        self._ensure_access()
        notes = self._loop.run_until_complete(
            self._listener.get_notifications_async(self._kinds.TOAST)
        )
        out: list[RawToast] = []
        for n in notes:
            info = n.app_info
            aumid = info.app_user_model_id if info else ""
            name = info.display_info.display_name if info else ""
            binding = n.notification.visual.get_binding(self._binding)
            texts = [t.text for t in binding.get_text_elements()] if binding else []
            created = n.creation_time
            ts = created.timestamp() if created is not None else time.time()
            out.append((int(n.id), str(aumid or ""), str(name or ""), texts, ts))
        return out

    def close(self) -> None:
        try:
            self._loop.close()
        except Exception:
            pass


# -- the watch --------------------------------------------------------------


class NotificationWatch:
    """Polls the notification centre on its own thread; readers take a copy."""

    # Anything under this is pointless: toasts do not arrive faster.
    MIN_POLL = 2.0

    def __init__(
        self,
        config: NotificationsConfig,
        listener_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.config = config
        self.apps = dict(config.apps) if config.apps else dict(DEFAULT_APPS)
        self._factory = listener_factory or WinRtListener
        self._listener: Any = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._seen: set[int] = set()
        self._seeded = False
        self._recent: deque[WorkNotification] = deque(maxlen=max(5, config.keep))
        self._pending: list[WorkNotification] = []
        self._error = ""
        self._polled_at = 0.0

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        if not self.config.enabled or self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="notification-watch", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        if self._listener is not None:
            try:
                self._listener.close()
            except Exception:
                pass
            self._listener = None

    def _run(self) -> None:
        interval = max(self.MIN_POLL, float(self.config.poll_seconds))
        while not self._stop.is_set():
            started = time.monotonic()
            self.poll()
            self._stop.wait(max(0.5, interval - (time.monotonic() - started)))

    # -- reading ----------------------------------------------------------

    def poll(self) -> None:
        """One read of the notification centre. Never raises."""
        try:
            if self._listener is None:
                self._listener = self._factory()
            raws = self._listener.read()
        except NotificationsUnavailable as exc:
            self._fail(str(exc))
            return
        except Exception as exc:
            self._fail(f"could not read notifications: {exc}")
            return
        self._absorb(raws)

    def _fail(self, reason: str) -> None:
        with self._lock:
            if reason != self._error:
                log.warning("work notifications unavailable: %s", reason)
            self._error = reason
            self._polled_at = time.time()
        # A fresh adapter next time: the failure may have been transient.
        if self._listener is not None:
            try:
                self._listener.close()
            except Exception:
                pass
            self._listener = None

    def _absorb(self, raws: Iterable[RawToast]) -> None:
        fresh: list[WorkNotification] = []
        ids: set[int] = set()
        for raw in raws:
            try:
                note = parse(raw, self.apps)
            except Exception as exc:
                log.debug("skipping an unreadable toast: %s", exc)
                continue
            if note is None:
                continue
            ids.add(note.id)
            if note.id in self._seen:
                continue
            fresh.append(note)
        fresh.sort(key=lambda n: n.ts)

        with self._lock:
            self._error = ""
            self._polled_at = time.time()
            self._seen.update(ids)
            for note in fresh:
                if ignored(note, self.config):
                    continue
                self._recent.append(note)
                if self._seeded:
                    self._pending.append(note)
            # What was on screen before the watch started is context, not news.
            self._seeded = True
            # Toasts the user dismissed leave the centre; forget them so a
            # reused id (rare, but Windows does reuse them) is not swallowed.
            self._seen &= ids | {n.id for n in self._recent}

    def drain_new(self) -> list[WorkNotification]:
        """Everything that arrived since the last drain, oldest first."""
        with self._lock:
            out, self._pending = self._pending, []
        return out

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """The `work_notifications` section, or nothing when switched off."""
        if not self.config.enabled:
            return {}
        # With no agent thread (a one-shot exec) read inline, throttled so a
        # burst of queries is not a burst of WinRT calls.
        if self._thread is None and time.time() - self._polled_at > self.MIN_POLL:
            self.poll()
        with self._lock:
            recent = [n.to_dict() for n in reversed(self._recent)]
            cutoff = time.time() - 3600
            by_app: dict[str, int] = {}
            for n in self._recent:
                if n.ts >= cutoff:
                    by_app[n.app] = by_app.get(n.app, 0) + 1
            section: dict[str, Any] = {
                "available": not self._error,
                "recent": recent,
                "last_hour": sum(by_app.values()),
                "by_app": by_app,
                "polled_at": self._polled_at,
            }
            if self._error:
                section["error"] = self._error
        return {"work_notifications": section}
