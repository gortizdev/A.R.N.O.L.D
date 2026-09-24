"""Screen capture.

`capture` saves a PNG. `capture_for_vision` returns a downscaled JPEG for
sending to a model, which is a different job: it wants the smallest image that
is still legible, not an archival copy.

This runs in the desktop session (the voice agent is a logon task), so PIL's
ImageGrab is enough - none of the scheduled-task-and-compiled-exe contortions
the Pi needs to reach across SSH into session 0.
"""

from __future__ import annotations

import base64
import io
import logging
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_DIR = Path("screenshots")

_dpi_ready = False


def _ensure_dpi_aware() -> None:
    """Report real pixels on scaled displays.

    Without this, a process that is not DPI-aware sees the virtual screen in
    logical coordinates, so a 150%-scaled monitor captures blurred and
    cropped. Process-wide and permanent, but this only ever runs in the
    headless agent, never alongside a Tk window that would then need to cope
    with unscaled coordinates.
    """
    global _dpi_ready
    if _dpi_ready:
        return
    _dpi_ready = True
    try:
        import ctypes

        try:
            # Per-monitor v2 where it exists: correct on mixed-DPI setups.
            ctypes.windll.user32.SetProcessDpiAwarenessContext(-4)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception as exc:  # pragma: no cover - not Windows, or already set
        log.debug("could not set DPI awareness: %s", exc)


def active_monitor_rect() -> tuple[int, int, int, int] | None:
    """Bounds of the monitor holding the foreground window, if there is one."""
    import ctypes
    from ctypes import wintypes

    _ensure_dpi_aware()

    class MONITORINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("rcMonitor", wintypes.RECT),
            ("rcWork", wintypes.RECT),
            ("dwFlags", wintypes.DWORD),
        ]

    try:
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None
        monitor = user32.MonitorFromWindow(hwnd, 2)  # MONITOR_DEFAULTTONEAREST
        if not monitor:
            return None
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        if not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            return None
        r = info.rcMonitor
        return (r.left, r.top, r.right, r.bottom)
    except Exception as exc:
        log.debug("active monitor lookup failed: %s", exc)
        return None


def monitor_rects() -> list[tuple[int, int, int, int]]:
    """Virtual-screen bounds of each monitor, ordered left to right."""
    import ctypes
    from ctypes import wintypes

    _ensure_dpi_aware()
    rects: list[tuple[int, int, int, int]] = []
    callback = ctypes.WINFUNCTYPE(
        ctypes.c_int,
        wintypes.HMONITOR,
        wintypes.HDC,
        ctypes.POINTER(wintypes.RECT),
        wintypes.LPARAM,
    )

    def collect(_monitor, _dc, rect, _data):
        r = rect.contents
        rects.append((r.left, r.top, r.right, r.bottom))
        return 1

    if not ctypes.windll.user32.EnumDisplayMonitors(0, 0, callback(collect), 0):
        return []
    rects.sort(key=lambda r: r[0])
    return rects


def capture(
    *,
    directory: Path | None = None,
    all_screens: bool = True,
    max_width: int = 1920,
    as_base64: bool = False,
) -> dict[str, object]:
    """Grab the screen, save a PNG, and optionally return it inline as base64."""
    from PIL import ImageGrab

    _ensure_dpi_aware()
    try:
        image = ImageGrab.grab(all_screens=all_screens)
    except Exception as exc:
        raise RuntimeError(f"screen capture failed: {exc}") from exc

    if max_width and image.width > max_width:
        ratio = max_width / image.width
        image = image.resize((max_width, max(1, round(image.height * ratio))))

    target_dir = Path(directory) if directory else DEFAULT_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    name = f"screen-{datetime.now().strftime('%Y%m%d-%H%M%S')}.png"
    path = target_dir / name
    image.save(path, format="PNG", optimize=True)

    result: dict[str, object] = {
        "path": str(path.resolve()),
        "width": image.width,
        "height": image.height,
    }
    if as_base64:
        buf = io.BytesIO()
        image.save(buf, format="PNG", optimize=True)
        result["png_base64"] = base64.b64encode(buf.getvalue()).decode("ascii")
    return result


# Vision models downscale anything longer than this on the way in, so sending
# more than it just costs bytes and latency.
VISION_MAX_EDGE = 2048


def capture_for_vision(
    *,
    screen: str = "active",
    max_width: int = 1600,
    quality: int = 70,
) -> dict[str, object]:
    """Grab the screen as a base64 JPEG small enough to send to a model.

    `screen` is:
      * "active" (default) - the monitor holding the foreground window, which
        is what someone means by "my screen". Full resolution of the one
        display they are actually looking at.
      * "all" - every monitor stitched into one wide image.
      * a 1-based monitor number counting left to right - the same addressing
        Jarvis uses on the Pi, so "my second monitor" means the same thing to
        both assistants.
    """
    from PIL import Image, ImageGrab

    _ensure_dpi_aware()
    label = str(screen or "active").strip().lower()
    rects = monitor_rects()

    bbox: tuple[int, int, int, int] | None = None
    if label in ("active", "current", "focused", "this", ""):
        bbox = active_monitor_rect()
        # No foreground window (locked, or nothing focused): show everything
        # rather than guessing which display was meant.
        label = "active" if bbox else "all"
    elif label in ("all", "both", "everything"):
        bbox = None
        label = "all"
    else:
        try:
            index = int(label)
        except ValueError:
            raise RuntimeError(f"'{screen}' is not a monitor number") from None
        if not 1 <= index <= len(rects):
            raise RuntimeError(f"there is no monitor {index}; this PC has {len(rects)}")
        bbox = rects[index - 1]
        label = str(index)

    if bbox is not None and label == "active":
        # Name the monitor too, so the assistant can say which one it looked at.
        for i, rect in enumerate(rects, start=1):
            if rect == bbox:
                label = f"active ({i} of {len(rects)}, counting from the left)"
                break

    try:
        image = ImageGrab.grab(bbox=bbox, all_screens=True)
    except Exception as exc:
        raise RuntimeError(f"screen capture failed: {exc}") from exc

    full = (image.width, image.height)
    # A stitched multi-monitor shot is far wider than one screen; holding it to
    # a single monitor's width would leave each half unreadable.
    limit = VISION_MAX_EDGE if bbox is None else max_width
    if limit and image.width > limit:
        ratio = limit / image.width
        image = image.resize(
            (limit, max(1, round(image.height * ratio))), Image.LANCZOS
        )
    # JPEG has no alpha and ImageGrab can hand back RGBA.
    if image.mode != "RGB":
        image = image.convert("RGB")

    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=quality, optimize=True)
    data = buf.getvalue()
    return {
        "jpeg_base64": base64.b64encode(data).decode("ascii"),
        "bytes": len(data),
        "width": image.width,
        "height": image.height,
        "captured_width": full[0],
        "captured_height": full[1],
        "screen": label,
    }
