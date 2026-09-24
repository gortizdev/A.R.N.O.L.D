"""Things worth mentioning without being asked.

Alert rules answer "has a number crossed a line". This answers a different and
harder question: is anything going on that a person would want to be told about
before they think to ask? The difference between the two is the difference
between a monitoring system and an assistant.

Three parts, deliberately separate:

* **observers** look at the history, the snapshot and the log, and produce
  candidate remarks. They know nothing about whether saying them is a good
  idea.
* **policy** decides whether now is a moment to speak at all - quiet hours, how
  recently it last spoke, how much it has already said today, whether the same
  thing was said yesterday.
* **the judge** picks between what survives, and may be the model itself. A
  rules engine can tell you the disk is filling. Only something that has read
  the room can tell you it is not worth interrupting a film to say so.

Nothing here speaks unless `proactive.speak` is on. Observers still run and
their notices are recorded, so you can read what it *would* have said before
letting it say anything. An assistant that starts talking in your house is not
a feature to turn on unseen.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .humanize import duration_speech

log = logging.getLogger(__name__)

GB = 1024 ** 3


@dataclass(slots=True)
class Notice:
    """One thing the assistant could say, and why it thinks so."""

    key: str  # stable per subject, so the same remark is not repeated
    text: str  # said aloud, verbatim
    priority: int = 5  # 1 (mention sometime) to 10 (say this now)
    detail: dict[str, Any] = field(default_factory=dict)
    ts: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "text": self.text,
            "priority": self.priority,
            "detail": self.detail,
            "ts": self.ts or time.time(),
        }


# -- observers ---------------------------------------------------------------
#
# Each takes the world and returns nothing, one notice, or several. They must
# be cheap: this runs on the agent's tick.


def disk_filling(world: "World") -> list[Notice]:
    """A disk on course to run out, while there is still time to do something.

    The threshold is the projection, not the percentage. A drive sitting at 91%
    for a year is not news; one that has shed 40 GB this week is, even at 60%.
    """
    notices = []
    for drive, disk in (world.snapshot.get("disks") or {}).items():
        trend = world.history.trend(f"disks.{drive}.free_bytes", hours=24 * 14)
        if not trend:
            continue
        days = trend.get("days_to_zero")
        if days is None or days > world.config.proactive.disk_days_ahead:
            continue

        free = disk.get("free_bytes") or 0
        rate = abs(trend["per_day"]) / GB
        when = "today" if days < 1 else f"in about {round(days)} days"
        notices.append(
            Notice(
                key=f"disk_filling:{drive}",
                text=(
                    f"Drive {drive} is losing about {rate:.0f} gigabytes a day. "
                    f"At that rate it runs out {when}, with "
                    f"{free / GB:.0f} gigabytes left now."
                ),
                priority=9 if days < 3 else 6,
                detail={"drive": drive, "days_to_zero": days, **trend},
            )
        )
    return notices


def memory_creeping(world: "World") -> list[Notice]:
    """Memory climbing steadily with a process to blame.

    Worth saying only with a culprit: "memory is high" is something the user
    can see, and "Chrome has taken twelve gigabytes since this morning" is
    something they can act on.
    """
    trend = world.history.trend("memory.percent", hours=12)
    if not trend or trend["per_day"] < world.config.proactive.memory_climb_per_day:
        return []
    if trend["current"] < 70:
        return []

    top = (world.snapshot.get("processes") or {}).get("top_memory") or []
    if not top:
        return []
    worst = top[0]
    gigabytes = (worst.get("memory_bytes") or 0) / GB
    if gigabytes < 2:
        return []

    return [
        Notice(
            key="memory_creeping",
            text=(
                f"Memory has been climbing all day - it's at "
                f"{trend['current']:.0f} percent now, up from {trend['first']:.0f}. "
                f"{worst.get('name', 'Something')} is holding {gigabytes:.0f} gigabytes."
            ),
            priority=5,
            detail={"process": worst.get("name"), "gigabytes": round(gigabytes, 1), **trend},
        )
    ]


def gpu_running_hot(world: "World") -> list[Notice]:
    """A GPU whose temperature has drifted up over days, not one that spiked.

    A spike is what the alert rules are for. This is the fan that is quietly
    losing to the dust.
    """
    notices = []
    for index, gpu in enumerate(world.snapshot.get("gpus") or []):
        trend = world.history.trend(f"gpus.{index}.temperature_c", hours=24 * 10)
        if not trend or trend["span_hours"] < 48:
            continue
        if trend["per_day"] < world.config.proactive.gpu_drift_per_day:
            continue
        if (gpu.get("temperature_c") or 0) < 70:
            continue
        notices.append(
            Notice(
                key=f"gpu_hot_drift:{index}",
                text=(
                    f"The GPU is running {trend['current'] - trend['first']:.0f} degrees "
                    f"warmer than it was a few days ago, at {trend['current']:.0f} now. "
                    "It may be worth a look at the fans."
                ),
                priority=4,
                detail={"gpu": index, **trend},
            )
        )
    return notices


def log_errors(world: "World") -> list[Notice]:
    """The agent's own log filling with errors it has not mentioned.

    Something that fails every tick tells nobody, because each failure is
    handled. A count over an hour is how that surfaces.
    """
    threshold = world.config.proactive.log_error_threshold
    errors = world.recent_log_errors()
    if len(errors) < threshold:
        return []

    kinds = sorted({line.split(" - ")[-1][:60] for line in errors})
    return [
        Notice(
            key="log_errors",
            text=(
                f"I've logged {len(errors)} errors in the last hour. "
                f"The first one reads: {kinds[0]}"
            ),
            priority=6,
            detail={"count": len(errors), "kinds": kinds[:5]},
        )
    ]


def pending_reboot(world: "World") -> list[Notice]:
    """Windows waiting on a restart, on a machine that has been up for weeks.

    Neither half is worth saying alone. A pending update on a machine rebooted
    this morning is nothing - they have just done it. A long uptime with
    nothing waiting is a boast, not a notice. Together they are the reason
    somebody's machine spends a fortnight half-patched.
    """
    from .platform_win import IS_WINDOWS

    if not IS_WINDOWS:
        return []

    uptime = float(world.snapshot.get("uptime_seconds") or 0.0)
    threshold = world.config.proactive.reboot_after_hours * 3600
    if not threshold or uptime < threshold:
        return []

    book = world.book
    now = time.time()
    # It cannot change without a reboot or an update landing, so once every
    # half hour is plenty and the registry stays out of the tick.
    cached = book.recall("pending_reboot_probe") if book else None
    if isinstance(cached, dict) and now - float(cached.get("ts") or 0) < 1800:
        waiting = bool(cached.get("waiting"))
    else:
        waiting = _reboot_is_pending()
        if book:
            book.remember("pending_reboot_probe", {"ts": now, "waiting": waiting})
    if not waiting:
        return []

    days = uptime / 86400
    return [
        Notice(
            key="pending_reboot",
            text=(
                f"Windows has been waiting to restart since an update, and this "
                f"machine has been up for {days:.0f} days. It's a good moment to "
                "reboot."
            ),
            priority=8 if days >= 21 else 6,
            detail={"uptime_days": round(days, 1)},
        )
    ]


def _reboot_is_pending() -> bool:
    """The three markers Windows leaves. Read-only, no elevation, no subprocess."""
    try:
        import winreg
    except ImportError:
        return False

    access = winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0)
    for path in (
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending",
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired",
    ):
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path, 0, access):
                return True
        except OSError:
            continue

    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Session Manager",
            0,
            access,
        ) as key:
            value, _ = winreg.QueryValueEx(key, "PendingFileRenameOperations")
            return bool(value)
    except OSError:
        return False


def watched_process_ended(world: "World") -> list[Notice]:
    """A process being watched has just stopped.

    The transition, not the condition: an alert rule can say "Steam should be
    running", and this says "Steam has closed", which is the one that answers
    "did the render finish while I was out".
    """
    if not world.config.proactive.announce_watched_exit or world.book is None:
        return []

    watched = (world.snapshot.get("processes") or {}).get("watched") or {}
    notices = []
    for name, state in watched.items():
        if not isinstance(state, dict):
            continue
        running = bool(state.get("running"))
        memo_key = f"watched_running:{name}"
        before = world.book.recall(memo_key)
        was_running = isinstance(before, dict) and bool(before.get("running"))

        # Carry the start time forward while it keeps running, so the exit can
        # say how long it went on for.
        started = float(before.get("since") or 0.0) if was_running else 0.0
        if running:
            started = started or time.time()
        world.book.remember(memo_key, {"running": running, "since": started})

        # Nothing on the first pass: not having seen it before is not an exit.
        if not was_running or running:
            continue

        ran_for = time.time() - started if started else 0.0
        label = name[:-4] if name.lower().endswith(".exe") else name
        how_long = f", after {duration_speech(ran_for)}" if ran_for > 300 else ""
        notices.append(
            Notice(
                key=f"watched_ended:{name}:{int(started)}",
                text=f"{label.capitalize()} has closed{how_long}.",
                priority=4,
                detail={"process": name, "ran_for_seconds": round(ran_for)},
            )
        )
    return notices


def battery_low(world: "World") -> list[Notice]:
    """On battery and getting short, on a machine that has one."""
    battery = world.snapshot.get("battery")
    if not isinstance(battery, dict) or battery.get("percent") is None:
        return []
    if battery.get("plugged_in"):
        return []

    cfg = world.config.proactive
    percent = float(battery.get("percent") or 0.0)
    seconds_left = battery.get("seconds_left")
    minutes_left = (
        float(seconds_left) / 60 if isinstance(seconds_left, (int, float)) and seconds_left > 0
        else None
    )
    by_percent = percent <= cfg.battery_percent
    by_time = minutes_left is not None and minutes_left <= cfg.battery_minutes
    if not (by_percent or by_time):
        return []

    # Banded, so it can speak again on the way down without repeating itself
    # at the same level.
    band = 5 if percent <= 5 else 10 if percent <= 10 else 20
    left = f" - about {duration_speech(minutes_left * 60)} left" if minutes_left else ""
    return [
        Notice(
            key=f"battery_low:{band}",
            text=f"You're on battery, at {percent:.0f} percent{left}.",
            priority=8,
            detail={"percent": percent, "minutes_left": minutes_left},
        )
    ]


def pi_unreachable(world: "World") -> list[Notice]:
    """The Pi has been silent long enough to be worth mentioning.

    Worth saying because it changes what the assistant can do: with the Pi
    down, nothing reaches the other room and everything comes out of these
    speakers instead. Better said once than discovered.
    """
    down_since = float(world.links.get("pi_down_since") or 0.0)
    if not down_since:
        return []
    hours = (time.time() - down_since) / 3600
    if hours < world.config.proactive.pi_quiet_hours:
        return []

    when = datetime.fromtimestamp(down_since).strftime("%H:%M")
    return [
        # Keyed on the day the outage began, so a Pi that is off for a week
        # is one remark rather than one on every pass.
        Notice(
            key=f"pi_unreachable:{int(down_since // 86400)}",
            text=(
                f"I haven't been able to reach the Pi since {when}, so anything "
                "I say is only coming out of these speakers."
            ),
            priority=5,
            detail={"down_since": down_since, "hours": round(hours, 1)},
        )
    ]


def drive_vanished(world: "World") -> list[Notice]:
    """A drive that was there an hour ago and is not there now."""
    if not world.config.proactive.announce_missing_drives or world.book is None:
        return []

    present = set(world.snapshot.get("disks") or {})
    if not present:
        return []  # a snapshot with no disks at all is a collector problem
    before = world.book.recall("drives_seen")
    world.book.remember("drives_seen", sorted(present))
    if not isinstance(before, list):
        return []

    notices = []
    for drive in sorted(set(before) - present):
        # Only for a drive that has been around a while: a USB stick that came
        # and went this morning is not news.
        trend = world.history.trend(f"disks.{drive}.free_bytes", hours=24 * 7)
        if not trend or trend.get("span_hours", 0) < 24:
            continue
        notices.append(
            Notice(
                key=f"drive_gone:{drive}",
                text=(
                    f"Drive {drive} isn't there any more. It was showing "
                    f"{(trend.get('current') or 0) / GB:.0f} gigabytes free an hour ago."
                ),
                priority=7,
                detail={"drive": drive},
            )
        )
    return notices


def long_session(world: "World") -> list[Notice]:
    """Hours at the keyboard with no real break. Off unless asked for.

    Priority 3 puts it under the default `min_priority`, so even switched on
    it is only written down until the operator also lowers the floor. Two
    switches for the one feature here that can nag is the right ratio.
    """
    hours_wanted = world.config.proactive.break_after_hours
    if not hours_wanted or world.book is None:
        return []

    from .platform_win.window import idle_seconds

    idle = idle_seconds()
    if idle is None:
        return []

    now = time.time()
    started = float(world.book.recall("session_started") or 0.0)
    if idle > 300 or not started:
        # A real break, or the first pass: the clock starts here.
        world.book.remember("session_started", now if idle <= 300 else 0.0)
        return []

    at_it = (now - started) / 3600
    if at_it < hours_wanted:
        return []
    return [
        # Keyed on the session, not the hour within it, so this is said once
        # when the line is crossed rather than again every hour after.
        Notice(
            key=f"long_session:{int(started)}",
            text=f"You've been at this for {at_it:.0f} hours without a break.",
            priority=3,
            detail={"hours": round(at_it, 1)},
        )
    ]


OBSERVERS: tuple[Callable[["World"], list[Notice]], ...] = (
    disk_filling,
    memory_creeping,
    gpu_running_hot,
    log_errors,
    pending_reboot,
    watched_process_ended,
    battery_low,
    pi_unreachable,
    drive_vanished,
    long_session,
)


# -- the world an observer sees ----------------------------------------------


class World:
    def __init__(
        self,
        config,
        snapshot: dict[str, Any],
        history,
        alerts: list[str],
        links: dict[str, Any] | None = None,
    ) -> None:
        self.config = config
        self.snapshot = snapshot
        self.history = history
        self.alerts = alerts
        # What the agent knows about its own connections - whether the broker
        # is up, how long the Pi has been silent. Not in the snapshot because
        # it is about this process rather than about the machine.
        self.links = links or {}
        # Set by the engine before the judge sees it.
        self.book: NoticeBook | None = None
        self._log_errors: list[str] | None = None

    def recent_log_errors(self, within_seconds: float = 3600.0) -> list[str]:
        """ERROR lines from the tail of the agent log, this hour only."""
        if self._log_errors is not None:
            return self._log_errors

        self._log_errors = []
        path = self.config.log_file
        if not path:
            return self._log_errors
        try:
            with open(path, "rb") as fh:
                fh.seek(0, 2)
                fh.seek(max(0, fh.tell() - 128 * 1024))
                lines = fh.read().decode("utf-8", "replace").splitlines()
        except OSError:
            return self._log_errors

        cutoff = datetime.fromtimestamp(time.time() - within_seconds)
        for line in lines:
            if " ERROR " not in line and " CRITICAL " not in line:
                continue
            try:
                when = datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue  # a traceback's continuation lines have no stamp
            if when >= cutoff:
                self._log_errors.append(line.strip())
        return self._log_errors


# -- policy ------------------------------------------------------------------


def _in_quiet_hours(config, when: float) -> bool:
    """Quiet hours as 'HH:MM-HH:MM', crossing midnight if the end is smaller."""
    window = (config.proactive.quiet_hours or "").strip()
    if not window or "-" not in window:
        return False
    try:
        start_text, end_text = window.split("-", 1)
        start = datetime.strptime(start_text.strip(), "%H:%M").time()
        end = datetime.strptime(end_text.strip(), "%H:%M").time()
    except ValueError:
        log.warning("proactive.quiet_hours %r is not 'HH:MM-HH:MM'; ignoring it", window)
        return False

    now = datetime.fromtimestamp(when).time()
    if start <= end:
        return start <= now < end
    return now >= start or now < end  # 22:00-08:00


class NoticeBook:
    """What has been said, and whether anything more should be.

    Kept on disk beside the state file: the agent restarts, and an assistant
    that repeats yesterday's remark every time it is updated is worse than one
    that says nothing.
    """

    # A runaway observer must not be able to grow this file without bound.
    MAX_MEMO_KEYS = 100

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._said: dict[str, float] = {}
        self._recent: list[dict[str, Any]] = []
        self._memo: dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        try:
            with self.path.open("r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return
        said = payload.get("said")
        if isinstance(said, dict):
            self._said = {k: float(v) for k, v in said.items() if isinstance(v, (int, float))}
        recent = payload.get("recent")
        if isinstance(recent, list):
            self._recent = recent[-50:]
        memo = payload.get("memo")
        if isinstance(memo, dict):
            self._memo = memo

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "said": self._said,
                        "recent": self._recent[-50:],
                        "memo": self._memo,
                    },
                    fh,
                )
        except OSError as exc:
            log.debug("could not write the notice book: %s", exc)

    # -- what an observer remembers between passes ---------------------------
    #
    # Some things are only worth saying at the moment they change - a process
    # that has just closed, a drive that has just gone. The history keeps
    # numbers and drops booleans, so an observer that needs an edge needs
    # somewhere of its own to write down what it saw last time.

    def remember(self, key: str, value: Any) -> None:
        self._memo[key] = value
        if len(self._memo) > self.MAX_MEMO_KEYS:
            for stale in list(self._memo)[: len(self._memo) - self.MAX_MEMO_KEYS]:
                self._memo.pop(stale, None)
        self._save()

    def recall(self, key: str, default: Any = None) -> Any:
        return self._memo.get(key, default)

    def said_at(self, key: str) -> float:
        return self._said.get(key, 0.0)

    def spoken_since(self, since: float) -> int:
        return sum(1 for when in self._said.values() if when >= since)

    def last_spoken(self) -> float:
        return max(self._said.values(), default=0.0)

    def record(self, notice: Notice, spoken: bool) -> None:
        if spoken:
            self._said[notice.key] = notice.ts or time.time()
        self._recent.append({**notice.to_dict(), "spoken": spoken})
        self._save()

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        return self._recent[-limit:]


# -- judges ------------------------------------------------------------------


class RuleJudge:
    """Deterministic: the highest priority that clears the bar, or nothing."""

    def choose(self, candidates: list[Notice], world: World) -> Notice | None:
        floor = world.config.proactive.min_priority
        eligible = [n for n in candidates if n.priority >= floor]
        if not eligible:
            return None
        return max(eligible, key=lambda n: (n.priority, n.ts))


class ModelJudge:
    """Asks a model whether any of this is worth interrupting for.

    The rules can tell you a disk is filling. Whether that is worth saying to
    someone who is three hours into a game at eleven at night is a judgement,
    and a small model makes it better than a threshold does. It sees the same
    context a person in the room would: what is on screen, the time, and what
    it has already said today.

    Any failure - no key, no network, a slow reply, a shape it did not expect -
    falls back to the rules. Proactive speech must never depend on the internet
    being up.
    """

    URL = "https://api.openai.com/v1/chat/completions"

    def __init__(self, model: str = "gpt-4o-mini", timeout: float = 8.0) -> None:
        self.model = model
        self.timeout = timeout
        self._fallback = RuleJudge()

    def choose(self, candidates: list[Notice], world: World) -> Notice | None:
        key = os.environ.get("OPENAI_API_KEY", "")
        if not key or not candidates:
            return self._fallback.choose(candidates, world)

        window = (world.snapshot.get("active_window") or {}).get("title") or "nothing"
        prompt = {
            "time": datetime.now().strftime("%A %H:%M"),
            "on_screen": str(window)[:120],
            "already_said_today": [
                item["text"][:80]
                for item in (world.book.recent(6) if world.book else [])
                if item.get("spoken")
            ],
            "candidates": [
                {"id": i, "text": n.text, "urgency": n.priority}
                for i, n in enumerate(candidates)
            ],
        }
        instructions = (
            "You are the judgement of a home assistant deciding whether to speak "
            "unprompted. The user did not ask for any of this. Choose at most one "
            "candidate, and only if a thoughtful person would interrupt what is on "
            "screen right now to say it. Prefer silence: saying nothing is always "
            "acceptable and repeating yourself never is. Reply with JSON only: "
            '{"say": <id or null>, "because": "<a few words>"}'
        )
        body = json.dumps({
            "model": self.model,
            "messages": [
                {"role": "system", "content": instructions},
                {"role": "user", "content": json.dumps(prompt)},
            ],
            "response_format": {"type": "json_object"},
            "max_tokens": 80,
            "temperature": 0.2,
        }).encode()

        try:
            request = urllib.request.Request(
                self.URL, data=body, method="POST",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            answer = json.loads(payload["choices"][0]["message"]["content"])
        except (urllib.error.URLError, OSError, KeyError, IndexError,
                ValueError, json.JSONDecodeError) as exc:
            log.debug("the model judge was no help (%s); falling back to the rules", exc)
            return self._fallback.choose(candidates, world)

        chosen = answer.get("say")
        if chosen is None or not isinstance(chosen, int) or not 0 <= chosen < len(candidates):
            log.info("nothing worth saying: %s", str(answer.get("because"))[:80])
            return None
        log.info("saying it because: %s", str(answer.get("because"))[:80])
        return candidates[chosen]


def build_judge(config):
    if config.proactive.judge == "model":
        return ModelJudge(config.proactive.judge_model)
    return RuleJudge()


# -- the loop ----------------------------------------------------------------


class ProactiveEngine:
    """Runs the observers, applies the policy, and returns what to say."""

    def __init__(self, config, history, book: NoticeBook | None = None, judge=None) -> None:
        self.config = config
        self.history = history
        self.book = book or NoticeBook(Path(config.state_file).with_name("notices.json"))
        self.judge = judge or build_judge(config)
        self._last_run = 0.0

    def due(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        return (now - self._last_run) >= max(30.0, self.config.proactive.interval_seconds)

    def observe(self, world: World) -> list[Notice]:
        """Every candidate, whether or not any of them will be said."""
        now = time.time()
        candidates: list[Notice] = []
        for observer in OBSERVERS:
            try:
                found = observer(world) or []
            except Exception:
                log.exception("observer %s failed; skipping it", observer.__name__)
                continue
            for notice in found:
                notice.ts = notice.ts or now
                candidates.append(notice)
        return candidates

    def _silenced(self, notice: Notice, world: World, now: float) -> str:
        """Why this particular notice must not be said, or an empty string."""
        proactive = self.config.proactive
        since = self.book.said_at(notice.key)
        if since and (now - since) < proactive.repeat_after_hours * 3600:
            return "said recently"
        if world.alerts:
            # An alert is already being announced; two voices about the same
            # machine at once is one too many.
            return "an alert is firing"
        return ""

    def run(self, config_world: World, *, now: float | None = None) -> Notice | None:
        """One pass. Returns the notice to speak, or None.

        Recording happens either way: the notice book is the record of what it
        would have said, which is what makes turning this on safe.
        """
        now = time.time() if now is None else now
        self._last_run = now
        world = config_world
        world.book = self.book  # the judge reads what has already been said

        proactive = self.config.proactive
        candidates = self.observe(world)
        if not candidates:
            return None

        eligible = []
        for notice in candidates:
            reason = self._silenced(notice, world, now)
            if reason:
                log.debug("not saying %s: %s", notice.key, reason)
                continue
            eligible.append(notice)

        if not eligible:
            return None

        # Whether it is a moment to speak at all is asked once, after there is
        # something to say - so the log records the remark either way.
        quiet = _in_quiet_hours(self.config, now)
        too_soon = (now - self.book.last_spoken()) < proactive.min_gap_minutes * 60
        spent = self.book.spoken_since(now - 86400) >= proactive.max_per_day

        chosen = self.judge.choose(eligible, world)
        if chosen is None:
            for notice in eligible:
                self.book.record(notice, spoken=False)
            return None

        if quiet or too_soon or spent or not proactive.speak:
            log.info(
                "keeping quiet about %s (%s)",
                chosen.key,
                "quiet hours" if quiet else "too soon" if too_soon
                else "said enough today" if spent else "proactive.speak is off",
            )
            self.book.record(chosen, spoken=False)
            return None

        self.book.record(chosen, spoken=True)
        return chosen
