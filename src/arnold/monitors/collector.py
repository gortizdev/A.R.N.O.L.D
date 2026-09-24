"""Builds the telemetry snapshot that everything else reads from.

One `Collector` instance lives for the life of the process. It holds the state
that rate- and percentage-style metrics need in order to mean anything: network
byte counters from the previous tick, and per-process CPU samplers that only
return real numbers once they have been called twice.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

import psutil

from ..config import ClaudeConfig, MonitorsConfig, NotificationsConfig, OutlookConfig
from ..platform_win.window import active_window
from . import gpu as gpu_probe
from .claude import ClaudeWatch
from .notifications import NotificationWatch
from .outlook import OutlookProbe

log = logging.getLogger(__name__)


def _resolve(node: Any, parts: list[str]) -> Any:
    if not parts:
        return node

    if isinstance(node, dict):
        # Keys can themselves contain dots (`processes.watched.steam.exe.running`),
        # so try the longest key match at each level before falling back.
        for size in range(len(parts), 0, -1):
            key = ".".join(parts[:size])
            if key in node:
                found = _resolve(node[key], parts[size:])
                if found is not None or size == len(parts):
                    return found
        return None

    if isinstance(node, list):
        try:
            return _resolve(node[int(parts[0])], parts[1:])
        except (ValueError, IndexError):
            return None

    return None


def get_metric(snapshot: dict[str, Any], path: str) -> Any:
    """Resolve a dotted metric path such as `disks.C.percent`. None if absent."""
    return _resolve(snapshot, path.split("."))


def _drive_key(mountpoint: str) -> str:
    """'C:\\' -> 'C' so metric paths stay writable as `disks.C.percent`."""
    return mountpoint.rstrip(":\\/").upper() or mountpoint


# PID 0 is "System Idle Process" on Windows; it reports the machine's *unused*
# CPU, so it would always top a busiest-processes list.
_PSEUDO_PIDS = frozenset({0})


class Collector:
    def __init__(
        self,
        config: MonitorsConfig,
        outlook: OutlookConfig | None = None,
        notifications: NotificationsConfig | None = None,
        claude: ClaudeConfig | None = None,
    ) -> None:
        self._config = config
        # Its own cache and its own cadence: a COM call into Outlook is far too
        # slow to sit on the telemetry tick. Disabled unless configured, so the
        # default snapshot has no `mail` or `calendar` section at all.
        self.outlook = OutlookProbe(outlook or OutlookConfig())
        # Same arrangement for the notification centre: WinRT on its own
        # thread, a `work_notifications` section only when switched on.
        self.notifications = NotificationWatch(notifications or NotificationsConfig())
        # And for the Claude Code sessions open on this machine: transcripts
        # tailed on their own thread, a `claude_sessions` section.
        self.claude = ClaudeWatch(claude or ClaudeConfig())
        self._proc_cache: dict[int, psutil.Process] = {}
        self._last_net: tuple[float, Any] | None = None
        self._boot_time = psutil.boot_time()
        self._cpu_count = psutil.cpu_count(logical=True) or 1
        # The MQTT callback thread answers queries while the tick loop collects;
        # both mutate the rate counters and CPU samplers.
        self._lock = threading.Lock()
        # Probes that have already failed once, so the warning is logged once
        # rather than on every tick.
        self._failed: set[str] = set()

        # cpu_percent(interval=None) reports since the previous call, so the
        # first reading is meaningless unless we prime it here.
        psutil.cpu_percent(interval=None)
        psutil.cpu_percent(interval=None, percpu=True)
        self._first_sample = True

        self._disks = list(config.disks) or self._autodetect_disks()
        log.info("monitoring disks: %s", ", ".join(self._disks) or "(none found)")

        self._prime_processes()

    def _prime_processes(self) -> None:
        """Take a first per-process CPU reading so the next one is meaningful.

        psutil reports per-process CPU as a delta between successive calls on the
        same Process object. Without this, a one-shot `exec query.processes`
        would report every process at 0%.
        """
        for proc in psutil.process_iter(["pid"]):
            try:
                proc.cpu_percent(interval=None)
                self._proc_cache[proc.pid] = proc
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

    @staticmethod
    def _autodetect_disks() -> list[str]:
        found: list[str] = []
        for part in psutil.disk_partitions(all=False):
            # Skip optical/removable so an empty DVD drive is not a 'disk full' alert.
            if "cdrom" in part.opts or not part.fstype:
                continue
            found.append(part.mountpoint)
        return found

    # -- individual sections ------------------------------------------------

    def _cpu(self) -> dict[str, Any]:
        freq = None
        try:
            current = psutil.cpu_freq()
            freq = round(current.current) if current else None
        except Exception:
            freq = None

        # A one-shot CLI call would otherwise read 0% - there has been no elapsed
        # window since priming. Block briefly for the first sample only.
        interval = 0.15 if self._first_sample else None
        self._first_sample = False

        return {
            "percent": round(psutil.cpu_percent(interval=interval), 1),
            "per_core": [round(v, 1) for v in psutil.cpu_percent(interval=None, percpu=True)],
            "cores_physical": psutil.cpu_count(logical=False),
            "cores_logical": psutil.cpu_count(logical=True),
            "freq_mhz": freq,
        }

    def _memory(self) -> dict[str, Any]:
        mem = psutil.virtual_memory()
        return {
            "percent": round(mem.percent, 1),
            "total_bytes": mem.total,
            "used_bytes": mem.used,
            "available_bytes": mem.available,
        }

    def _swap(self) -> dict[str, Any]:
        # Reads a Windows performance counter, which is disabled or corrupted on
        # some machines (`PdhAddEnglishCounterW failed`). Not worth failing over.
        swap = psutil.swap_memory()
        return {
            "percent": round(swap.percent, 1),
            "total_bytes": swap.total,
            "used_bytes": swap.used,
        }

    def _disks_section(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for mount in self._disks:
            try:
                usage = psutil.disk_usage(mount)
            except (PermissionError, OSError) as exc:
                log.debug("disk_usage(%s) failed: %s", mount, exc)
                continue
            result[_drive_key(mount)] = {
                "mountpoint": mount,
                "percent": round(usage.percent, 1),
                "total_bytes": usage.total,
                "used_bytes": usage.used,
                "free_bytes": usage.free,
            }
        return result

    def _network(self, now: float) -> dict[str, Any]:
        counters = psutil.net_io_counters()
        section: dict[str, Any] = {
            "bytes_sent": counters.bytes_sent,
            "bytes_recv": counters.bytes_recv,
            "sent_rate_bps": None,
            "recv_rate_bps": None,
        }
        if self._last_net is not None:
            prev_time, prev = self._last_net
            elapsed = now - prev_time
            if elapsed > 0:
                # Counters reset on adapter reset/reboot; a negative delta is not a rate.
                sent_delta = counters.bytes_sent - prev.bytes_sent
                recv_delta = counters.bytes_recv - prev.bytes_recv
                if sent_delta >= 0 and recv_delta >= 0:
                    section["sent_rate_bps"] = round(sent_delta / elapsed, 1)
                    section["recv_rate_bps"] = round(recv_delta / elapsed, 1)
        self._last_net = (now, counters)
        return section

    def _battery(self) -> dict[str, Any] | None:
        try:
            battery = psutil.sensors_battery()
        except Exception:
            return None
        if battery is None:
            return None
        secs = battery.secsleft
        if secs in (psutil.POWER_TIME_UNLIMITED, psutil.POWER_TIME_UNKNOWN):
            secs = None
        return {
            "percent": round(battery.percent, 1),
            "plugged_in": bool(battery.power_plugged),
            "seconds_left": secs,
        }

    def _processes(self) -> dict[str, Any]:
        """Process count, top consumers, and the state of any watched names."""
        watch = {name.lower() for name in self._config.watch_processes}
        watched: dict[str, dict[str, Any]] = {
            name: {"running": False, "count": 0, "pids": []} for name in self._config.watch_processes
        }
        by_lower = {name.lower(): name for name in self._config.watch_processes}

        live: dict[int, psutil.Process] = {}
        rows: list[dict[str, Any]] = []

        for proc in psutil.process_iter(["pid", "name", "memory_info"]):
            try:
                info = proc.info
                pid, name = info["pid"], info["name"] or ""
                live[pid] = proc

                sampler = self._proc_cache.get(pid)
                if sampler is None or sampler.pid != pid:
                    sampler = proc
                    self._proc_cache[pid] = sampler
                    cpu = 0.0  # first sample for this pid is always 0
                else:
                    # psutil reports this relative to one core, so a busy process
                    # can read 400% on a 4-core box. Normalise to whole-machine
                    # percent, which is what Task Manager shows and what someone
                    # asking "what's using my CPU?" expects to hear.
                    cpu = sampler.cpu_percent(interval=None) / self._cpu_count

                mem_info = info.get("memory_info")
                # The idle process is by definition whatever is left over, so
                # ranking it as the top CPU consumer is worse than useless.
                if name and pid not in _PSEUDO_PIDS:
                    rows.append(
                        {
                            "pid": pid,
                            "name": name,
                            "cpu_percent": round(cpu, 1),
                            "memory_bytes": getattr(mem_info, "rss", 0) if mem_info else 0,
                        }
                    )

                lowered = name.lower()
                if lowered in watch:
                    entry = watched[by_lower[lowered]]
                    entry["running"] = True
                    entry["count"] += 1
                    entry["pids"].append(pid)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        # Drop samplers for processes that have exited, or the cache grows forever.
        for dead in set(self._proc_cache) - set(live):
            self._proc_cache.pop(dead, None)

        top_n = max(0, self._config.top_processes)
        return {
            "count": len(rows),
            "top_cpu": sorted(rows, key=lambda r: r["cpu_percent"], reverse=True)[:top_n],
            "top_memory": sorted(rows, key=lambda r: r["memory_bytes"], reverse=True)[:top_n],
            "watched": watched,
        }

    # -- public API ---------------------------------------------------------

    def _safe(self, name: str, probe: Callable[[], Any], default: Any) -> Any:
        """Run one probe, logging and substituting a default if it fails.

        Hardware and OS facilities vary: performance counters get disabled,
        drives disappear mid-read, sensors go missing. A single broken probe
        should degrade that one metric, not stop telemetry entirely.
        """
        try:
            return probe()
        except Exception as exc:
            if name not in self._failed:
                # Log the first failure per probe at WARNING; after that it is
                # a known condition and would only spam the log every tick.
                log.warning("%s probe failed (%s); reporting it as unavailable", name, exc)
                self._failed.add(name)
            return default

    def snapshot(self, *, include_window: bool = True) -> dict[str, Any]:
        # Outside the lock on purpose: with no agent running this can make a COM
        # call, and none of it touches the rate counters the lock protects.
        mailbox = self._safe("outlook", self.outlook.snapshot, {})
        work = self._safe("notifications", self.notifications.snapshot, {})
        coding = self._safe("claude", self.claude.snapshot, {})

        with self._lock:
            now = time.time()
            snap: dict[str, Any] = {
                "ts": now,
                "uptime_seconds": round(now - self._boot_time),
                "boot_time": self._boot_time,
                "cpu": self._safe("cpu", self._cpu, {"percent": None, "per_core": []}),
                "memory": self._safe("memory", self._memory, {"percent": None}),
                "swap": self._safe("swap", self._swap, {"percent": None}),
                "disks": self._safe("disks", self._disks_section, {}),
                "network": self._safe("network", lambda: self._network(now), {}),
                "processes": self._safe(
                    "processes", self._processes, {"count": 0, "top_cpu": [], "top_memory": [], "watched": {}}
                ),
            }

            battery = self._safe("battery", self._battery, None)
            if battery is not None:
                snap["battery"] = battery

            snap.update(mailbox)
            snap.update(work)
            snap.update(coding)

            snap["gpus"] = self._safe("gpu", gpu_probe.probe, []) if self._config.gpu else []
            if include_window:
                snap["active_window"] = self._safe(
                    "active_window", active_window, {"title": None, "process": None, "pid": None}
                )
            return snap
