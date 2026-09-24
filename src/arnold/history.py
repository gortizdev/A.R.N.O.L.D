"""What the machine has been doing, not just what it is doing.

Every number the assistant can otherwise reach is instantaneous. That is enough
to answer "how full is drive C" and no use at all for "how long have I got",
which is the more useful question and the one a person actually asks. So the
agent keeps a series.

Two resolutions, because they answer different questions:

* **fine** - one sample per tick, half an hour of them. Drives the dashboard's
  sparklines, and survives a page reload, which the browser's own copy did not.
* **hourly** - one bucket per hour with min/mean/max, kept for months. This is
  what a trend is measured over. A slope taken across half an hour of a laptop
  waking up says nothing; the same slope across two weeks says the disk fills
  on the 14th.

Samples are flat maps keyed by the same dotted metric paths the alert rules
use, so `disks.C.free_bytes` means one thing everywhere.
"""

from __future__ import annotations

import json
import logging
import math
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger(__name__)

# Half an hour at the default 10s tick. Enough to draw, small enough to rewrite
# every tick without thinking about it.
FINE_SAMPLES = 180
# Three months of hourly buckets. At roughly 150 bytes each that is a file you
# would have to go looking for to notice.
HOURLY_BUCKETS = 24 * 90
HOUR_SECONDS = 3600.0

# Metrics worth a series. Anything absent from a snapshot is simply skipped, so
# a machine with no battery or no GPU records neither.
BASE_METRICS = (
    "cpu.percent",
    "memory.percent",
    "swap.percent",
    "network.sent_rate_bps",
    "network.recv_rate_bps",
    "battery.percent",
)


def _get(snapshot: dict[str, Any], path: str) -> Any:
    from .monitors.collector import get_metric

    return get_metric(snapshot, path)


def flatten(snapshot: dict[str, Any]) -> dict[str, float]:
    """The numbers worth keeping from one snapshot, by dotted path."""
    sample: dict[str, float] = {}

    paths = list(BASE_METRICS)
    for drive in (snapshot.get("disks") or {}):
        paths += [f"disks.{drive}.percent", f"disks.{drive}.free_bytes"]
    for index, _ in enumerate(snapshot.get("gpus") or []):
        paths += [
            f"gpus.{index}.utilization_percent",
            f"gpus.{index}.temperature_c",
            f"gpus.{index}.memory_used_mb",
        ]

    for path in paths:
        value = _get(snapshot, path)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if isinstance(value, float) and not math.isfinite(value):
            continue
        # Two decimals is finer than any of these are measured to, and keeps
        # the file from filling with float noise.
        sample[path] = round(float(value), 2)
    return sample


def _write_json(path: Path, payload: Any) -> None:
    """Atomic, so a reader never catches a half-written file."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".hist-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, separators=(",", ":"))
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    except OSError as exc:
        log.debug("could not write %s: %s", path, exc)


def _read_json(path: Path, default: Any) -> Any:
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return default


def _slope_per_day(points: list[tuple[float, float]]) -> float | None:
    """Least-squares gradient in units per day, or None if it means nothing.

    Least squares rather than first-to-last: one reboot, one big delete, and a
    two-point line says the disk empties on Tuesday.
    """
    if len(points) < 4:
        return None
    span = points[-1][0] - points[0][0]
    if span < HOUR_SECONDS:
        return None

    n = len(points)
    mean_t = sum(t for t, _ in points) / n
    mean_v = sum(v for _, v in points) / n
    numerator = sum((t - mean_t) * (v - mean_v) for t, v in points)
    denominator = sum((t - mean_t) ** 2 for t, _ in points)
    if denominator <= 0:
        return None
    return (numerator / denominator) * 86400.0


class History:
    """The series on disk. One instance per process; the agent owns the writer."""

    def __init__(self, path: Path | str, *, fine: int = FINE_SAMPLES,
                 hourly: int = HOURLY_BUCKETS) -> None:
        self.path = Path(path)
        self.hourly_path = self.path.with_name(self.path.stem + "-hourly.json")
        self._fine_limit = max(2, fine)
        self._hourly_limit = max(2, hourly)

        self._fine: list[dict[str, Any]] = _read_json(self.path, {}).get("samples", [])
        self._hourly: list[dict[str, Any]] = _read_json(self.hourly_path, {}).get("buckets", [])
        # The hour being filled. Held in memory and written only when it
        # closes, so the long file is touched once an hour rather than
        # every tick.
        self._bucket: dict[str, Any] | None = None
        self._restore_open_bucket()

    def _restore_open_bucket(self) -> None:
        """Reopen the current hour after a restart rather than losing it."""
        if not self._hourly:
            return
        last = self._hourly[-1]
        if last.get("hour") == self._hour_of(time.time()):
            self._bucket = self._hourly.pop()

    @staticmethod
    def _hour_of(ts: float) -> int:
        return int(ts // HOUR_SECONDS)

    # -- writing ------------------------------------------------------------

    def record(self, snapshot: dict[str, Any], *, now: float | None = None) -> None:
        now = time.time() if now is None else now
        values = flatten(snapshot)
        if not values:
            return

        self._fine.append({"ts": round(now, 1), **values})
        del self._fine[: max(0, len(self._fine) - self._fine_limit)]
        _write_json(self.path, {"samples": self._fine})

        self._fold(values, now)

    def _fold(self, values: dict[str, float], now: float) -> None:
        """Add a sample to the open hour, closing the previous one if it ended."""
        hour = self._hour_of(now)
        if self._bucket is not None and self._bucket["hour"] != hour:
            self._close_bucket()
        if self._bucket is None:
            self._bucket = {"hour": hour, "ts": hour * HOUR_SECONDS, "n": 0, "m": {}}

        bucket = self._bucket
        bucket["n"] += 1
        for key, value in values.items():
            entry = bucket["m"].get(key)
            if entry is None:
                # [min, sum, max, count] - count per metric, because a GPU that
                # appears halfway through the hour must not skew the mean.
                bucket["m"][key] = [value, value, value, 1]
            else:
                entry[0] = min(entry[0], value)
                entry[1] += value
                entry[2] = max(entry[2], value)
                entry[3] += 1

    def _close_bucket(self) -> None:
        if self._bucket is None:
            return
        self._hourly.append(self._bucket)
        del self._hourly[: max(0, len(self._hourly) - self._hourly_limit)]
        self._bucket = None
        _write_json(self.hourly_path, {"buckets": self._hourly})

    def flush(self) -> None:
        """Persist the part-filled hour without closing it.

        For shutdown. The bucket goes into the file as the last entry, which is
        exactly where `_restore_open_bucket` looks for it on the way back up,
        so an agent restarted mid-hour carries on filling the same bucket
        instead of leaving a gap.
        """
        if self._bucket is None:
            return
        buckets = (self._hourly + [self._bucket])[-self._hourly_limit:]
        _write_json(self.hourly_path, {"buckets": buckets})

    # -- reading ------------------------------------------------------------

    def recent(self, limit: int = FINE_SAMPLES) -> list[dict[str, Any]]:
        """The fine samples, oldest first. What the sparklines are drawn from."""
        return self._fine[-max(1, limit):]

    def _buckets(self) -> Iterable[dict[str, Any]]:
        yield from self._hourly
        if self._bucket is not None:
            yield self._bucket

    def series(self, metric: str, hours: float = 24 * 7) -> list[tuple[float, float]]:
        """(timestamp, mean) per hour for one metric, oldest first."""
        cutoff = time.time() - hours * HOUR_SECONDS
        points: list[tuple[float, float]] = []
        for bucket in self._buckets():
            if bucket["ts"] < cutoff:
                continue
            entry = bucket["m"].get(metric)
            if entry and entry[3]:
                points.append((float(bucket["ts"]), entry[1] / entry[3]))
        return points

    def latest(self, metric: str) -> float | None:
        for sample in reversed(self._fine):
            value = sample.get(metric)
            if isinstance(value, (int, float)):
                return float(value)
        return None

    def trend(self, metric: str, hours: float = 24 * 7) -> dict[str, Any] | None:
        """Where a metric is heading: slope per day, and when it hits a wall.

        None when there is not enough history to say anything honest, which is
        the whole point - a fresh install must not announce that the disk fills
        on Thursday because it happened to be written to twice.
        """
        points = self.series(metric, hours)
        slope = _slope_per_day(points)
        if slope is None:
            return None

        current = self.latest(metric)
        if current is None:
            current = points[-1][1]

        span_hours = (points[-1][0] - points[0][0]) / HOUR_SECONDS
        trend: dict[str, Any] = {
            "metric": metric,
            "current": round(current, 2),
            "per_day": round(slope, 2),
            "span_hours": round(span_hours, 1),
            "samples": len(points),
            "first": round(points[0][1], 2),
        }
        # Only downward series run out of anything.
        if slope < 0 and current > 0:
            trend["days_to_zero"] = round(current / -slope, 1)
        return trend


def path_for(config) -> Path:
    """Beside the state file, which is already where per-run data lives."""
    return Path(config.state_file).with_name("history.json")
