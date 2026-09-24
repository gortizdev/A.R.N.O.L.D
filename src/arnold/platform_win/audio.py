"""Volume and media-transport control.

Two tiers: virtual key presses always work with no extra dependency, and
`pip install arnold[audio]` adds pycaw for absolute volume
("set volume to 40%") and for reading the current level back.
"""

from __future__ import annotations

import ctypes
import logging
import time

from . import require_windows

log = logging.getLogger(__name__)

VK_VOLUME_MUTE = 0xAD
VK_VOLUME_DOWN = 0xAE
VK_VOLUME_UP = 0xAF
VK_MEDIA_NEXT_TRACK = 0xB0
VK_MEDIA_PREV_TRACK = 0xB1
VK_MEDIA_STOP = 0xB2
VK_MEDIA_PLAY_PAUSE = 0xB3

MEDIA_KEYS = {
    "play": VK_MEDIA_PLAY_PAUSE,
    "pause": VK_MEDIA_PLAY_PAUSE,
    "playpause": VK_MEDIA_PLAY_PAUSE,
    "next": VK_MEDIA_NEXT_TRACK,
    "previous": VK_MEDIA_PREV_TRACK,
    "prev": VK_MEDIA_PREV_TRACK,
    "stop": VK_MEDIA_STOP,
}

_KEYEVENTF_KEYUP = 0x0002
# One VK_VOLUME_UP/DOWN press moves the Windows master volume by 2 points.
_STEP_PERCENT = 2


def _tap(vk: int, presses: int = 1) -> None:
    require_windows("media/volume keys")
    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    for i in range(presses):
        user32.keybd_event(vk, 0, 0, 0)
        user32.keybd_event(vk, 0, _KEYEVENTF_KEYUP, 0)
        if i + 1 < presses:
            time.sleep(0.01)


def press_media_key(action: str) -> None:
    key = MEDIA_KEYS.get(action.lower().replace("_", "").replace("-", ""))
    if key is None:
        raise ValueError(
            f"unknown media action {action!r}; expected one of: {', '.join(sorted(MEDIA_KEYS))}"
        )
    _tap(key)


def _endpoint():
    """Return a pycaw IAudioEndpointVolume, or None when pycaw is unavailable."""
    try:
        from comtypes import CLSCTX_ALL  # type: ignore
        from ctypes import POINTER, cast  # noqa: F401

        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume  # type: ignore
    except Exception:
        return None
    try:
        devices = AudioUtilities.GetSpeakers()
        interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        from ctypes import POINTER, cast  # noqa: F811

        return cast(interface, POINTER(IAudioEndpointVolume))
    except Exception as exc:
        log.debug("pycaw endpoint unavailable: %s", exc)
        return None


def get_volume() -> dict[str, object]:
    """Current master volume. `level` is None when pycaw is not installed."""
    endpoint = _endpoint()
    if endpoint is None:
        return {"level": None, "muted": None, "precise": False}
    return {
        "level": round(endpoint.GetMasterVolumeLevelScalar() * 100),
        "muted": bool(endpoint.GetMute()),
        "precise": True,
    }


def set_volume(level: int) -> dict[str, object]:
    """Set absolute volume 0-100. Falls back to stepping when pycaw is absent."""
    level = max(0, min(100, int(level)))
    endpoint = _endpoint()
    if endpoint is not None:
        endpoint.SetMasterVolumeLevelScalar(level / 100.0, None)
        return {"level": level, "precise": True}

    # No pycaw: floor to 0 with a generous run of key-downs, then step up.
    _tap(VK_VOLUME_DOWN, presses=(100 // _STEP_PERCENT) + 2)
    _tap(VK_VOLUME_UP, presses=level // _STEP_PERCENT)
    return {"level": level, "precise": False}


def adjust_volume(delta: int) -> dict[str, object]:
    """Nudge volume by `delta` percentage points (negative lowers)."""
    delta = max(-100, min(100, int(delta)))
    endpoint = _endpoint()
    if endpoint is not None:
        current = round(endpoint.GetMasterVolumeLevelScalar() * 100)
        return set_volume(current + delta)

    presses = max(1, abs(delta) // _STEP_PERCENT)
    _tap(VK_VOLUME_UP if delta > 0 else VK_VOLUME_DOWN, presses=presses)
    return {"level": None, "delta": delta, "precise": False}


def set_mute(muted: bool | None = None) -> dict[str, object]:
    """Mute, unmute, or (with None) toggle."""
    endpoint = _endpoint()
    if endpoint is None:
        _tap(VK_VOLUME_MUTE)  # hardware key only toggles
        return {"muted": None, "precise": False}
    if muted is None:
        muted = not bool(endpoint.GetMute())
    endpoint.SetMute(bool(muted), None)
    return {"muted": bool(muted), "precise": True}
