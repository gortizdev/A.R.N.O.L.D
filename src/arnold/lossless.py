"""Lossless Scaling, switched on for a game as it starts.

Lossless Scaling (the Steam app) scales whichever window is in front when its
hotkey is pressed, and can scale a game by itself if the game has a profile
with AutoScale on - but only while it is running. So for each game the
watcher sees:

- start Lossless Scaling if it is not running;
- if a profile already auto-scales this game, stop there: pressing the hotkey
  as well would toggle the scaling straight back off;
- otherwise wait until the game's window has been in front for
  `scaling_delay_seconds`, and press the hotkey read from its Settings.xml;
- then check it took. While scaling, Lossless Scaling shows a full-screen
  window of class "LosslessScaling", so a press that did nothing (it was
  still loading, the game was resetting its window) is noticed and retried,
  and a press is never made while it is already scaling - that would turn
  it off.

It is usually set to run as administrator. Started from here (a normal,
non-elevated process) that means a UAC prompt in the middle of a game's
launch - so `arnold game scaling --install` registers an elevated task,
once, through a single UAC prompt, and starting it through that task never
prompts. Without the task it is started directly and the prompt appears.
"""

from __future__ import annotations

import base64
import ctypes
import fnmatch
import logging
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import psutil

from . import process
from .config import GameConfig

log = logging.getLogger(__name__)

EXE_NAME = "LosslessScaling.exe"
TASK = "ArnoldLosslessScaling"
SETTINGS = Path(os.environ.get("LOCALAPPDATA", "")) / "Lossless Scaling" / "Settings.xml"
_STEAM_DEFAULT = Path(r"C:\Program Files (x86)\Steam")
_RELATIVE = Path("steamapps") / "common" / "Lossless Scaling" / EXE_NAME

# WPF Key names, as Settings.xml stores them, to virtual-key codes.
_NAMED_KEYS = {
    "space": 0x20, "tab": 0x09, "enter": 0x0D, "return": 0x0D, "escape": 0x1B,
    "home": 0x24, "end": 0x23, "insert": 0x2D, "delete": 0x2E,
    "pageup": 0x21, "prior": 0x21, "pagedown": 0x22, "next": 0x22,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "scroll": 0x91, "pause": 0x13, "capital": 0x14, "capslock": 0x14,
    "oem3": 0xC0, "oemtilde": 0xC0, "oemminus": 0xBD, "oemplus": 0xBB,
    "multiply": 0x6A, "add": 0x6B, "subtract": 0x6D, "divide": 0x6F, "decimal": 0x6E,
}
_MODIFIERS = {"control": 0x11, "ctrl": 0x11, "alt": 0x12, "shift": 0x10, "windows": 0x5B}


def key_code(name: str) -> int | None:
    key = name.strip()
    low = key.lower()
    if len(key) == 1 and key.isalnum():
        return ord(key.upper())
    if re.fullmatch(r"d[0-9]", low):
        return 0x30 + int(low[1])
    if re.fullmatch(r"f([1-9]|1[0-9]|2[0-4])", low):
        return 0x6F + int(low[1:])
    if re.fullmatch(r"numpad[0-9]", low):
        return 0x60 + int(low[-1])
    return _NAMED_KEYS.get(low)


@dataclass(slots=True)
class Settings:
    key: int | None = None
    modifiers: list[int] = field(default_factory=list)
    hotkey_text: str = ""
    # Lower-cased executable paths whose profile has AutoScale on.
    auto_paths: set[str] = field(default_factory=set)


def read_settings(path: Path = SETTINGS) -> Settings | None:
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return None
    key_name = (root.findtext("Hotkey") or "").strip()
    mod_names = (root.findtext("HotkeyModifierKeys") or "").replace(",", " ").split()
    settings = Settings(
        key=key_code(key_name) if key_name else None,
        modifiers=[_MODIFIERS[m.lower()] for m in mod_names if m.lower() in _MODIFIERS],
        hotkey_text="+".join(mod_names + [key_name]),
    )
    for profile in root.iter("Profile"):
        if (profile.findtext("AutoScale") or "").strip().lower() == "true":
            exe = (profile.findtext("Path") or "").strip()
            if exe:
                settings.auto_paths.add(os.path.normcase(exe))
    return settings


def _steam_libraries() -> list[Path]:
    roots = [_STEAM_DEFAULT]
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
            roots.insert(0, Path(winreg.QueryValueEx(key, "SteamPath")[0]))
    except OSError:
        pass
    libraries: list[Path] = []
    for root in roots:
        libraries.append(root)
        try:
            vdf = (root / "steamapps" / "libraryfolders.vdf").read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        libraries += [Path(p.replace("\\\\", "\\")) for p in re.findall(r'"path"\s+"([^"]+)"', vdf)]
    return libraries


def running() -> psutil.Process | None:
    for proc in psutil.process_iter(["name"]):
        if (proc.info["name"] or "").lower() == EXE_NAME.lower():
            return proc
    return None


def find_exe(configured: str = "") -> Path | None:
    if configured:
        path = Path(os.path.expandvars(os.path.expanduser(configured)))
        return path if path.is_file() else None
    proc = running()
    if proc is not None:
        try:
            return Path(proc.exe())
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    for library in _steam_libraries():
        candidate = library / _RELATIVE
        if candidate.is_file():
            return candidate
    return None


def task_installed() -> bool:
    proc = process.run(
        ["powershell", "-NoProfile", "-Command",
         f"if (Get-ScheduledTask -TaskName '{TASK}' -ErrorAction SilentlyContinue) {{ 'yes' }}"],
        timeout=30,
    )
    return "yes" in (proc.stdout or "")


def start(exe: Path) -> str:
    """Start it minimised, elevated through the task when there is one."""
    if task_installed():
        proc = process.run(
            ["powershell", "-NoProfile", "-Command", f"Start-ScheduledTask -TaskName '{TASK}'"],
            timeout=30,
        )
        if proc.returncode == 0:
            return "started through its elevated task"
    process.launch([str(exe), "-StartMinimized"])
    return "started directly (expect a UAC prompt - `arnold game scaling --install` avoids it)"


def _elevated(script: str) -> str | None:
    """Run a PowerShell script elevated: one UAC prompt, and wait for it."""
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    outer = (
        "$p = Start-Process powershell -Verb RunAs -Wait -PassThru -WindowStyle Hidden "
        f"-ArgumentList '-NoProfile','-EncodedCommand','{encoded}'; exit $p.ExitCode"
    )
    proc = process.run(["powershell", "-NoProfile", "-Command", outer], timeout=180)
    if proc.returncode != 0:
        return (proc.stderr or "").strip() or f"exit {proc.returncode} (UAC declined?)"
    return None


def install_task(exe: Path) -> str | None:
    user = os.environ.get("USERDOMAIN", "") + "\\" + os.environ.get("USERNAME", "")
    script = (
        f"$a = New-ScheduledTaskAction -Execute '{exe}' -Argument '-StartMinimized'"
        f" -WorkingDirectory '{exe.parent}'; "
        f"$p = New-ScheduledTaskPrincipal -UserId '{user}' -LogonType Interactive -RunLevel Highest; "
        "$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries"
        " -ExecutionTimeLimit 0; "
        f"Register-ScheduledTask -TaskName '{TASK}' -Action $a -Principal $p -Settings $s -Force"
        " | Out-Null"
    )
    return _elevated(script)


def remove_task() -> str | None:
    return _elevated(f"Unregister-ScheduledTask -TaskName '{TASK}' -Confirm:$false")


# -- pressing the hotkey -----------------------------------------------------


def foreground_pid() -> int | None:
    user32 = ctypes.WinDLL("user32")
    user32.GetForegroundWindow.restype = ctypes.c_void_p
    user32.GetWindowThreadProcessId.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32))
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    pid = ctypes.c_uint32()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value or None


def _belongs_to(pid: int | None, game_pid: int) -> bool:
    """The window's process is the game, or one of its children - some games
    are a small starter that opens the real window from a child process."""
    if pid is None:
        return False
    if pid == game_pid:
        return True
    try:
        return any(parent.pid == game_pid for parent in psutil.Process(pid).parents())
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def scaling_active() -> bool:
    """Lossless Scaling is scaling something right now."""
    user32 = ctypes.WinDLL("user32")
    user32.FindWindowW.restype = ctypes.c_void_p
    user32.FindWindowW.argtypes = (ctypes.c_wchar_p, ctypes.c_wchar_p)
    user32.IsWindowVisible.argtypes = (ctypes.c_void_p,)
    hwnd = user32.FindWindowW("LosslessScaling", None)
    return bool(hwnd and user32.IsWindowVisible(hwnd))


def age(proc: psutil.Process | None) -> float:
    try:
        return time.time() - proc.create_time() if proc else 0.0
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return 0.0


def press(key: int, modifiers: list[int]) -> None:
    user32 = ctypes.WinDLL("user32")
    up = 0x0002  # KEYEVENTF_KEYUP
    for mod in modifiers:
        user32.keybd_event(mod, 0, 0, 0)
    user32.keybd_event(key, 0, 0, 0)
    time.sleep(0.05)
    user32.keybd_event(key, 0, up, 0)
    for mod in reversed(modifiers):
        user32.keybd_event(mod, 0, up, 0)


def wait_in_front(
    game_pid: int,
    settle: float,
    timeout: float,
    *,
    fg: Callable[[], int | None] = foreground_pid,
    alive: Callable[[int], bool] = psutil.pid_exists,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """True once the game's window has been in front for `settle` seconds
    without a break; False if it never is within `timeout`, or the game ends."""
    deadline = clock() + timeout
    since: float | None = None
    while clock() < deadline and alive(game_pid):
        if _belongs_to(fg(), game_pid):
            if since is None:
                since = clock()
            if clock() - since >= settle:
                return True
        else:
            since = None
        sleep(0.5)
    return False


class Scaler:
    """Switches Lossless Scaling on once per game session. activate() runs in
    a thread of its own, so waiting for the window does not stall the
    watcher; reset() when the session ends."""

    def __init__(self, game: GameConfig) -> None:
        self.game = game
        self._lock = threading.Lock()
        self._done = False
        self._busy: set[int] = set()

    def reset(self) -> None:
        with self._lock:
            self._done = False

    def wanted(self, name: str) -> bool:
        patterns = self.game.scaling_games
        return not patterns or any(fnmatch.fnmatchcase(name.lower(), p.lower()) for p in patterns)

    def activate_later(self, pid: int, name: str, exe: str) -> None:
        if not self.game.scaling or not self.wanted(name):
            return
        with self._lock:
            if self._done or pid in self._busy:
                return
            self._busy.add(pid)
        threading.Thread(
            target=self._activate, args=(pid, name, exe), name=f"scaling-{pid}", daemon=True
        ).start()

    def _activate(self, pid: int, name: str, exe: str) -> None:
        try:
            log.info("lossless scaling: %s", self.activate(pid, name, exe))
        except Exception:
            log.exception("lossless scaling failed for %s", name)
        finally:
            with self._lock:
                self._busy.discard(pid)

    ATTEMPTS = 3

    def activate(self, pid: int, name: str, exe: str, sleep: Callable[[float], None] = time.sleep) -> str:
        game = self.game
        ls_exe = find_exe(game.scaling_exe)
        if ls_exe is None:
            return "not installed (set game.scaling_exe if it lives somewhere unusual)"
        started = ""
        proc = running()
        if proc is None:
            started = start(ls_exe) + "; "
            for _ in range(20):
                sleep(0.5)
                proc = running()
                if proc is not None:
                    break
        warm = game.scaling_warmup_seconds - age(proc)
        if warm > 0:
            sleep(warm)
        settings = read_settings()
        if settings is None:
            return started + f"could not read {SETTINGS}"
        auto = bool(exe) and os.path.normcase(exe) in settings.auto_paths
        if settings.key is None and not auto:
            return started + f"hotkey {settings.hotkey_text!r} is not one this can press"

        settle = game.scaling_delay_seconds
        for attempt in range(1, self.ATTEMPTS + 1):
            if not wait_in_front(pid, settle, game.scaling_timeout_seconds):
                return started + f"{name}'s window never stayed in front; not scaling"
            settle = 3.0  # later attempts: the game has already settled once
            if auto and attempt == 1:
                # Its own profile scales it; give that the first chance.
                sleep(5.0)
            if scaling_active():
                return started + self._finish(f"{name} is scaling")
            with self._lock:
                if self._done:
                    return started + "already switched on for this session"
            if settings.key is None:
                return started + f"{name} did not auto-scale, and the hotkey cannot be pressed"
            press(settings.key, settings.modifiers)
            sleep(3.0)
            if not scaling_active():
                log.info("lossless scaling: press %s for %s did not take", attempt, name)
                continue
            # A game that resets its window right after it appears drops the
            # scaling; one look a little later catches that.
            sleep(10.0)
            if scaling_active() or not _belongs_to(foreground_pid(), pid):
                return started + self._finish(f"pressed {settings.hotkey_text} for {name}; scaling")
            log.info("lossless scaling: %s dropped the scaling; pressing again", name)
        return started + f"pressed {settings.hotkey_text} {self.ATTEMPTS} times for {name}; it never scaled"

    def _finish(self, message: str) -> str:
        with self._lock:
            self._done = True
        return message
