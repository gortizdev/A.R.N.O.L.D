"""Game boost: what Razer Cortex does when a game starts, done here.

Four things, each undone when the game is over:

- the power plan goes to high performance (the old one is remembered);
- background apps named in `game.close_apps` are closed, and started again
  afterwards with the command line they had;
- every other process's working set is trimmed, so the game gets the RAM
  (EmptyWorkingSet - pages come back on demand, nothing is lost);
- the game's own priority class is raised.

What was changed is written to boost.json beside the state file before
anything else happens, so `arnold game off` from another process - or the
next run after a crash - can put it back.

`arnold game watch` is the launch detection: a small loop that notices a
process starting from a game folder (Steam, Epic, Xbox...), boosts, and
undoes it once no game has been running for `exit_grace_seconds`. It must
not be one of TASKS, because game mode stops those.
"""

from __future__ import annotations

import ctypes
import fnmatch
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Callable

import psutil

from . import process
from .config import Config, GameConfig

log = logging.getLogger(__name__)

# The scheduled tasks that keep the assistant running, in stop order.
TASKS = ("ArnoldVoice", "ArnoldFace", "Arnold")
# The launch watcher's own task. Not in TASKS: game mode must not stop it.
WATCH_TASK = "ArnoldGameWatch"

# powercfg's own aliases, and the Ultimate Performance plan, which exists
# only once someone has duplicated it into the list.
_PLAN_ALIASES = {
    "high": "SCHEME_MIN",
    "balanced": "SCHEME_BALANCED",
    "saver": "SCHEME_MAX",
}
_ULTIMATE = "e9a42b02-d5df-448d-aa00-03f14749eb61"
_PLAN_LINE = re.compile(
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\s+\((.*?)\)", re.I
)

_PRIORITIES = {
    "high": getattr(psutil, "HIGH_PRIORITY_CLASS", None),
    "above_normal": getattr(psutil, "ABOVE_NORMAL_PRIORITY_CLASS", None),
}


def state_path(config: Config) -> Path:
    return Path(config.state_file).with_name("boost.json")


def read_boost(config: Config) -> dict[str, Any] | None:
    try:
        return json.loads(state_path(config).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_boost(config: Config, payload: dict[str, Any]) -> None:
    path = state_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


# -- scheduled tasks ----------------------------------------------------------


def task_states(names: tuple[str, ...] = TASKS) -> dict[str, str]:
    quoted = ",".join(f"'{t}'" for t in names)
    proc = process.run(
        ["powershell", "-NoProfile", "-Command",
         f"Get-ScheduledTask -TaskName {quoted} -ErrorAction SilentlyContinue"
         " | ForEach-Object { \"$($_.TaskName)=$($_.State)\" }"],
        timeout=30,
    )
    states = {}
    for line in (proc.stdout or "").splitlines():
        name, _, state = line.strip().partition("=")
        if name:
            states[name] = state
    return states


def switch_tasks(stop: bool) -> str | None:
    """Stop (or start) every task. Returns an error message, or None."""
    verb = "Stop-ScheduledTask" if stop else "Start-ScheduledTask"
    order = TASKS if stop else tuple(reversed(TASKS))
    script = "; ".join(f"{verb} -TaskName '{t}' -ErrorAction SilentlyContinue" for t in order)
    proc = process.run(["powershell", "-NoProfile", "-Command", script], timeout=60)
    if proc.returncode != 0:
        return (proc.stderr or "").strip() or f"exit {proc.returncode}"
    return None


# -- power plan ----------------------------------------------------------------


def active_plan() -> str | None:
    proc = process.run(["powercfg", "/getactivescheme"], timeout=15)
    match = _PLAN_LINE.search(proc.stdout or "")
    return match.group(1).lower() if match else None


def list_plans() -> dict[str, str]:
    proc = process.run(["powercfg", "/list"], timeout=15)
    return {m.group(1).lower(): m.group(2) for m in _PLAN_LINE.finditer(proc.stdout or "")}


def resolve_plan(wanted: str, plans: dict[str, str]) -> str | None:
    """A powercfg argument for `wanted`, or None if there is no such plan."""
    key = wanted.strip().lower()
    if not key:
        return None
    if key == "ultimate":
        # Not listed means never duplicated in; high performance is next best.
        return _ULTIMATE if _ULTIMATE in plans else _PLAN_ALIASES["high"]
    if key in _PLAN_ALIASES:
        return _PLAN_ALIASES[key]
    if key in plans:
        return key
    for guid, name in plans.items():
        if name.lower() == key:
            return guid
    return None


def set_plan(plan: str) -> bool:
    return process.run(["powercfg", "/setactive", plan], timeout=15).returncode == 0


# -- background apps ---------------------------------------------------------


def _matches(name: str, patterns: list[str]) -> bool:
    lowered = name.lower()
    return any(fnmatch.fnmatchcase(lowered, p.lower()) for p in patterns)


def close_apps(patterns: list[str]) -> list[dict[str, Any]]:
    """Close every process matching `patterns`; return how to start them again.

    Only the root of each app is remembered - OneDrive or Teams run as a tree
    of processes, and starting the root brings the rest back.
    """
    if not patterns:
        return []
    me = os.getpid()
    matched: list[psutil.Process] = []
    for proc in psutil.process_iter(["name"]):
        if proc.pid != me and _matches(proc.info["name"] or "", patterns):
            matched.append(proc)
    pids = {p.pid for p in matched}
    relaunch: list[dict[str, Any]] = []
    for proc in matched:
        try:
            if proc.ppid() in pids:
                continue
            argv = proc.cmdline() or [proc.exe()]
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        entry = {"name": proc.info["name"], "argv": argv}
        if entry not in relaunch:
            relaunch.append(entry)
    for proc in matched:
        try:
            proc.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    _, alive = psutil.wait_procs(matched, timeout=5)
    for proc in alive:
        try:
            proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return relaunch


def reopen_apps(entries: list[dict[str, Any]]) -> list[str]:
    started = []
    for entry in entries:
        argv = entry.get("argv") or []
        if not argv or not Path(argv[0]).is_file():
            continue
        try:
            process.launch(argv)
            started.append(entry.get("name") or Path(argv[0]).name)
        except OSError as exc:
            log.warning("could not start %s again: %s", argv[0], exc)
    return started


# -- memory and priority ------------------------------------------------------


def trim_memory(skip: set[int] | None = None) -> int:
    """Empty the working set of every process we may touch. Returns the bytes
    of available memory gained (an estimate - other things move too)."""
    if os.name != "nt":
        return 0
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
    kernel32.K32EmptyWorkingSet.argtypes = (ctypes.c_void_p,)
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    access = 0x1000 | 0x0100  # PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SET_QUOTA
    skip = skip or set()
    before = psutil.virtual_memory().available
    for pid in psutil.pids():
        if pid in skip or pid <= 4:
            continue
        handle = kernel32.OpenProcess(access, False, pid)
        if not handle:
            continue
        try:
            kernel32.K32EmptyWorkingSet(handle)
        finally:
            kernel32.CloseHandle(handle)
    return max(0, psutil.virtual_memory().available - before)


def raise_priority(pid: int, level: str) -> bool:
    cls = _PRIORITIES.get(level.strip().lower())
    if cls is None:
        return False
    try:
        psutil.Process(pid).nice(cls)
        return True
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


# -- boost and restore -------------------------------------------------------


def apply(config: Config, game_pid: int | None = None, by: str = "manual") -> list[str]:
    """Boost. Safe to call again while boosted: what was saved is kept, so
    restore still goes back to how things were before the first call."""
    game = config.game
    done: list[str] = []
    saved = read_boost(config)
    if saved is None:
        saved = {"since": time.time(), "by": by, "power_plan": None, "reopen": []}
        if game.power_plan.strip():
            plans = list_plans()
            target = resolve_plan(game.power_plan, plans)
            if target is None:
                done.append(f"no power plan called {game.power_plan!r}")
            else:
                saved["power_plan"] = active_plan()
                # Saved before switching, so a crash mid-way can still be undone.
                _write_boost(config, saved)
                if set_plan(target):
                    now = active_plan() or ""
                    done.append("power plan: " + plans.get(now, game.power_plan))
                else:
                    done.append("could not change the power plan")
        _write_boost(config, saved)
        if game.close_apps:
            closed = close_apps(game.close_apps)
            if game.reopen_apps:
                saved["reopen"] = closed
                _write_boost(config, saved)
            if closed:
                done.append("closed " + ", ".join(e["name"] for e in closed))
    if game.trim_memory:
        freed = trim_memory({game_pid} if game_pid else None)
        done.append(f"trimmed memory ({freed // (1024 * 1024)} MB more available)")
    if game_pid and game.game_priority.strip():
        if raise_priority(game_pid, game.game_priority):
            done.append(f"{game.game_priority} priority for pid {game_pid}")
    return done


def restore(config: Config) -> list[str]:
    saved = read_boost(config)
    if saved is None:
        return []
    done: list[str] = []
    plan = saved.get("power_plan")
    if plan and set_plan(plan):
        done.append("power plan restored")
    started = reopen_apps(saved.get("reopen") or [])
    if started:
        done.append("started " + ", ".join(started))
    state_path(config).unlink(missing_ok=True)
    return done


# -- launch detection ----------------------------------------------------------


def is_game(name: str, exe: str, game: GameConfig) -> bool:
    if not name or _matches(name, game.ignore):
        return False
    if game.games and _matches(name, game.games):
        return True
    path = exe.lower().replace("/", "\\")
    return any(f.lower().replace("/", "\\") in path for f in game.game_folders if f)


def _describe(pid: int) -> tuple[str, str]:
    try:
        proc = psutil.Process(pid)
        name = proc.name()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return "", ""
    try:
        return name, proc.exe()
    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
        return name, ""


def running_games(game: GameConfig) -> list[tuple[int, str]]:
    found = []
    for pid in psutil.pids():
        name, exe = _describe(pid)
        if is_game(name, exe, game):
            found.append((pid, name))
    return found


class GameWatcher:
    """Notices games starting and stopping. One scan() per tick; callbacks do
    the work, so the loop can be driven by hand in tests.

    Only new pids are examined, so a tick costs one pid listing - reading
    every process's executable path each time would not be cheap.
    """

    def __init__(
        self,
        game: GameConfig,
        started: Callable[[int, str, str], None],
        ended: Callable[[], None],
        *,
        active: bool = False,
        pids: Callable[[], list[int]] = psutil.pids,
        describe: Callable[[int], tuple[str, str]] = _describe,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.game = game
        self.started = started
        self.ended = ended
        self.active = active
        self._pids = pids
        self._describe = describe
        self._clock = clock
        self._seen: dict[int, str] = {}  # pid -> game name, "" if not a game
        self._idle_since: float | None = None

    def scan(self) -> None:
        current = set(self._pids())
        for pid in list(self._seen):
            if pid not in current:
                del self._seen[pid]
        for pid in sorted(current - set(self._seen)):
            name, exe = self._describe(pid)
            self._seen[pid] = name if is_game(name, exe, self.game) else ""
            if self._seen[pid]:
                log.info("game started: %s (pid %s)", name, pid)
                self.active = True
                self.started(pid, name, exe)
        if any(self._seen.values()):
            self._idle_since = None
            return
        if not self.active:
            return
        now = self._clock()
        if self._idle_since is None:
            self._idle_since = now
        elif now - self._idle_since >= self.game.exit_grace_seconds:
            log.info("no game running for %ss; ending the boost", self.game.exit_grace_seconds)
            self.active = False
            self._idle_since = None
            self.ended()


def watch(config: Config) -> int:
    """Run until stopped: boost when a game starts, undo it when it ends."""
    from .lossless import Scaler

    game = config.game
    scaler = Scaler(game)

    def started(pid: int, name: str, exe: str) -> None:
        scaler.activate_later(pid, name, exe)
        saved = read_boost(config)
        if saved is None:
            if game.stop_tasks:
                error = switch_tasks(stop=True)
                if error:
                    log.warning("could not stop the tasks: %s", error)
            done = apply(config, pid, by="watch") if game.boost else []
            if not game.boost:
                _write_boost(config, {"since": time.time(), "by": "watch"})
        else:
            # Already boosted - by hand, or for an earlier game still open.
            done = []
            if game.boost and game.game_priority.strip():
                if raise_priority(pid, game.game_priority):
                    done.append(f"{game.game_priority} priority")
        log.info("%s: %s", name, "; ".join(done) or "nothing to do")

    def ended() -> None:
        scaler.reset()
        saved = read_boost(config)
        if saved is None or saved.get("by") != "watch":
            return  # a boost someone else started is theirs to end
        done = restore(config)
        if game.stop_tasks:
            error = switch_tasks(stop=False)
            if error:
                log.warning("could not start the tasks: %s", error)
        log.info("game over: %s", "; ".join(done) or "restored")

    leftover = read_boost(config)
    watcher = GameWatcher(game, started, ended, active=bool(leftover and leftover.get("by") == "watch"))
    log.info("watching for games every %ss", game.scan_seconds)
    try:
        while True:
            try:
                watcher.scan()
            except Exception:  # one bad tick must not end the watcher
                log.exception("game watch tick failed")
            time.sleep(max(0.5, game.scan_seconds))
    except KeyboardInterrupt:
        return 0


def install_watch(config: Config) -> str | None:
    """Register (or replace) the logon task that runs `arnold game watch`."""
    import sys

    pythonw = Path(sys.executable).with_name("pythonw.exe")
    exe = pythonw if pythonw.is_file() else Path(sys.executable)
    workdir = config.source_path.parent if config.source_path else Path.cwd()
    script = (
        f"$a = New-ScheduledTaskAction -Execute '{exe}' -Argument '-m arnold game watch'"
        f" -WorkingDirectory '{workdir}'; "
        "$t = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME; "
        "$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries"
        " -ExecutionTimeLimit 0 -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1); "
        f"Register-ScheduledTask -TaskName '{WATCH_TASK}' -Action $a -Trigger $t -Settings $s -Force"
        " | Out-Null; "
        f"Start-ScheduledTask -TaskName '{WATCH_TASK}'"
    )
    proc = process.run(["powershell", "-NoProfile", "-Command", script], timeout=60)
    if proc.returncode != 0:
        return (proc.stderr or "").strip() or f"exit {proc.returncode}"
    return None


def remove_watch() -> str | None:
    script = (
        f"Stop-ScheduledTask -TaskName '{WATCH_TASK}' -ErrorAction SilentlyContinue; "
        f"Unregister-ScheduledTask -TaskName '{WATCH_TASK}' -Confirm:$false"
    )
    proc = process.run(["powershell", "-NoProfile", "-Command", script], timeout=60)
    if proc.returncode != 0:
        return (proc.stderr or "").strip() or f"exit {proc.returncode}"
    return None
