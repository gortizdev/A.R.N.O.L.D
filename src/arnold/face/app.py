"""The floating face window.

tkinter rather than a GUI toolkit dependency: it ships with Python, and
Windows' layered-window support gives a frameless always-on-top widget with a
colour-keyed transparent background - which is all this needs.
"""

from __future__ import annotations

import logging
import math
import queue
import threading
import time
import tkinter as tk

from PIL import Image, ImageDraw, ImageTk

from ..config import Config, IdentityWatcher
from ..platform_win import window
from .holo import HoloRenderer
from .render import TRANSPARENT_KEY, FaceRenderer, Theme
from .sources import FaceFeed
from .state import FaceAnimator, FaceMode

log = logging.getLogger(__name__)

_KEY_HEX = "#%02x%02x%02x" % TRANSPARENT_KEY

# The J.A.R.V.I.S. core, or the older cartoon face.
_STYLES = {"holo": HoloRenderer, "orb": FaceRenderer}

# How long the eyes keep following a cursor that has stopped moving.
CURSOR_ATTENTION_SECONDS = 2.5

# A click and a drag share Button-1, so a press only counts as a click if the
# mouse moved less than this before release.
CLICK_SLOP_PX = 4

# A click waits this long for a second one before opening the hub, so a
# double-click still means "speak status" and nothing else. Windows' default
# double-click time is 500ms.
DOUBLE_CLICK_WAIT_MS = 400


class FaceWindow:
    def __init__(self, config: Config) -> None:
        self.config = config
        face = config.face

        self.size = max(80, face.size)
        style = (face.style or "holo").strip().lower()
        if style not in _STYLES:
            log.warning("face.style %r is not one of %s; using holo",
                        face.style, ", ".join(sorted(_STYLES)))
            style = "holo"
        self._style = style
        self.renderer = self._build_renderer()
        self.animator = FaceAnimator()
        self.feed = FaceFeed(config)
        # A profile switch edits config.yaml; the face repaints itself in the
        # new colours and moves to the new assistant's topics, no restart.
        self._watcher = IdentityWatcher(config)

        self.root = tk.Tk()
        self.root.title(config.assistant_name())
        self.root.overrideredirect(True)  # no titlebar or border
        self.root.attributes("-topmost", bool(face.always_on_top))
        self.root.attributes("-alpha", max(0.1, min(1.0, face.opacity)))
        # Everything painted in this colour becomes a hole in the window, which
        # is what makes the orb look like it floats on the desktop.
        self.root.attributes("-transparentcolor", _KEY_HEX)
        self.root.configure(bg=_KEY_HEX)

        self.canvas = tk.Canvas(
            self.root,
            width=self.size,
            height=self.size,
            highlightthickness=0,
            bd=0,
            bg=_KEY_HEX,
        )
        self.canvas.pack()
        # One Tk image, reused for every frame. Allocating a fresh
        # PhotoImage per frame costs more than drawing the face does, and at
        # 60 fps it churns enough memory to make the collector visible.
        self._photo = ImageTk.PhotoImage(
            Image.new("RGB", (self.size, self.size), TRANSPARENT_KEY)
        )
        self._image_id = self.canvas.create_image(0, 0, anchor="nw", image=self._photo)

        # Whether the user wants the avatar on screen. Separate from
        # hide_when_idle: this one is a decision, that one is a mood.
        self._avatar_visible = True
        # Tray callbacks arrive on the tray's own thread; they are queued
        # here and drained by _tick, so all UI work stays on the Tk thread.
        self._actions: queue.Queue = queue.Queue()
        self._tray = None
        self._tray_key: tuple | None = None
        self._tray_checked = 0.0

        self._place_window(face.position, face.margin)
        self._bind_events()
        if face.tray:
            self._start_tray()

        self._frame_interval = 1.0 / min(144, max(5, face.fps))
        self._last_tick = time.monotonic()
        self._drag_origin: tuple[int, int] | None = None
        self._dragged = False
        self._pending_click: str | None = None
        self._last_cursor: tuple[int, int] | None = None
        self._cursor_moved_at = 0.0
        self._running = True

    # -- identity -----------------------------------------------------------

    def _build_renderer(self):
        face = self.config.face
        if self._style == "holo":
            return HoloRenderer(
                self.size,
                supersample=face.supersample,
                palette=self.config.face_palette(),
                design=self.config.face_design(),
            )
        return _STYLES[self._style](self.size, supersample=face.supersample)

    def _apply_identity(self, fresh: Config) -> None:
        """Become the assistant the config now describes. Tk thread only."""
        if not self.config.adopt_identity(fresh):
            return
        self.renderer = self._build_renderer()
        self.root.title(self.config.assistant_name())
        # The feed's subscriptions are fixed when its MQTT thread starts, so
        # a new feed is the clean way to move to the new prefix.
        old = self.feed
        self.feed = FaceFeed(self.config)
        self.feed.start()
        old.stop()
        self._tray_key = None  # so the tooltip picks up the new name
        log.info(
            "face is now %s (%s %s)",
            self.config.assistant_name(),
            self.config.face_palette(),
            self.config.face_design(),
        )

    # -- placement ----------------------------------------------------------

    def _place_window(self, position: str, margin: int) -> None:
        self.root.update_idletasks()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        s = self.size

        if "," in position:
            try:
                x_str, y_str = position.split(",", 1)
                x, y = int(x_str.strip()), int(y_str.strip())
            except ValueError:
                log.warning("face.position %r is not 'x,y'; using bottom-right", position)
                x, y = sw - s - margin, sh - s - margin - 48
        else:
            anchors = {
                "top-left": (margin, margin),
                "top-right": (sw - s - margin, margin),
                # Bottom placements clear the taskbar.
                "bottom-left": (margin, sh - s - margin - 48),
                "bottom-right": (sw - s - margin, sh - s - margin - 48),
                "center": ((sw - s) // 2, (sh - s) // 2),
            }
            x, y = anchors.get(position, anchors["bottom-right"])

        # Keep it on screen even if the config asks for something silly.
        x = max(0, min(x, sw - s))
        y = max(0, min(y, sh - s))
        self.root.geometry(f"{s}x{s}+{x}+{y}")

    # -- interaction --------------------------------------------------------

    def _bind_events(self) -> None:
        self.canvas.bind("<Button-1>", self._drag_start)
        self.canvas.bind("<B1-Motion>", self._drag_move)
        self.canvas.bind("<ButtonRelease-1>", self._drag_end)
        self.canvas.bind("<Button-3>", self._show_menu)
        self.canvas.bind("<Double-Button-1>", self._double_click)
        self.root.bind("<Escape>", lambda e: self._hide_or_quit())

        self.menu = tk.Menu(self.root, tearoff=0)
        self.menu.add_command(label="Status", command=self._speak_status)
        self.menu.add_command(label="Open hub", command=self._open_hub)
        self.menu.add_separator()
        self.menu.add_command(label="Always on top", command=self._toggle_topmost)
        # With a tray icon running, "Hide" only tucks the avatar away and the
        # icon brings it back; without one, hiding it would strand the
        # process, so it quits as it always did.
        self.menu.add_command(label="Hide", command=self._hide_or_quit)
        self.menu.add_command(label="Exit", command=self.quit)

    def _track_cursor(self) -> None:
        """Point the eyes at the mouse, wherever it is on screen.

        Polled, because a colour-keyed window only receives mouse messages
        over its own opaque pixels - binding <Motion> would only notice the
        pointer once it was already on the orb.

        Tracking lapses a couple of seconds after the mouse stops, handing the
        eyes back to their idle wandering. A face that stares at a parked
        cursor indefinitely looks switched off.
        """
        if not self.config.face.follow_cursor:
            return
        position = window.cursor_position()
        if position is None:
            return

        # The animator's own clock, not the wall clock. Everything else in the
        # face advances on the dt handed to tick(), and mixing the two is what
        # makes animation behave differently at a different frame rate.
        now = self.animator.state.t
        if position != self._last_cursor:
            self._last_cursor = position
            self._cursor_moved_at = now
        elif now - self._cursor_moved_at > CURSOR_ATTENTION_SECONDS:
            return

        dx = position[0] - (self.root.winfo_rootx() + self.size / 2)
        dy = position[1] - (self.root.winfo_rooty() + self.size / 2)
        distance = math.hypot(dx, dy)
        if distance < 1.0:
            return

        # Deflection saturates with distance, so the eyes point *at* the mouse
        # rather than snapping to their limit the moment it leaves the orb.
        reach = min(1.0, distance / (self.size * 1.6))
        scale = reach * self.animator.LOOK_REACH / distance
        self.animator.look_at(dx * scale, dy * scale)

    def _drag_start(self, event) -> None:
        self._drag_origin = (event.x, event.y)
        self._dragged = False
        self.animator.nudge()

    def _drag_move(self, event) -> None:
        if self._drag_origin is None:
            return
        dx, dy = self._drag_origin
        if not self._dragged and abs(event.x - dx) + abs(event.y - dy) < CLICK_SLOP_PX:
            return
        self._dragged = True
        self.root.geometry(f"+{event.x_root - dx}+{event.y_root - dy}")

    def _drag_end(self, event) -> None:
        was_click = self._drag_origin is not None and not self._dragged
        self._drag_origin = None
        if was_click:
            self._pending_click = self.root.after(DOUBLE_CLICK_WAIT_MS, self._open_hub)

    def _double_click(self, event) -> None:
        if self._pending_click is not None:
            self.root.after_cancel(self._pending_click)
            self._pending_click = None
        self._speak_status()

    def _open_hub(self) -> None:
        """A click on the face: put the dashboard in front of the user.

        ensure_serving connects (and may bind a server), so it runs off the Tk
        thread - a stalled probe must not freeze the animation.
        """
        self._pending_click = None

        def run() -> None:
            try:
                import webbrowser

                from ..ui.server import ensure_serving

                url, started = ensure_serving(self.config)
                if started:
                    log.info("started the dashboard to answer a face click")
                webbrowser.open(url)
            except Exception as exc:
                log.error("could not open the hub: %s", exc)

        threading.Thread(target=run, daemon=True).start()

    def _show_menu(self, event) -> None:
        self.menu.entryconfigure(0, label=self.feed.status_text())
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    def _toggle_topmost(self) -> None:
        current = bool(self.root.attributes("-topmost"))
        self.root.attributes("-topmost", not current)

    # -- tray icon -----------------------------------------------------------

    def _start_tray(self) -> None:
        try:
            from ..platform_win.tray import TrayIcon
        except Exception as exc:
            log.debug("tray unavailable: %s", exc)
            return

        def enqueue(fn):
            return lambda: self._actions.put(fn)

        def menu():
            return [
                (self.feed.status_text(), None),
                ("-", None),
                (
                    "Hide avatar" if self._avatar_visible else "Show avatar",
                    enqueue(self.toggle_avatar),
                ),
                ("Open hub", enqueue(self._open_hub)),
                ("Speak status", enqueue(self._speak_status)),
                ("-", None),
                ("Exit", enqueue(self.quit)),
            ]

        tray = TrayIcon(
            tooltip=f"{self.config.assistant_name()}: starting",
            image=_tray_image(Theme.for_mode(FaceMode.OFFLINE), active=False),
            on_click=enqueue(self.toggle_avatar),
            menu=menu,
        )
        if tray.start():
            self._tray = tray
        else:
            log.info("no tray icon; the face menu is the only way to hide")

    def _refresh_tray(self) -> None:
        """Keep the icon's colour and tooltip honest, at most once a second.

        Active means the Pi-side bridge has published a fresh Jarvis state:
        a coloured, filled icon. No bridge (or no agent) is a grey ring.
        """
        now = time.monotonic()
        if self._tray is None or now - self._tray_checked < 1.0:
            return
        self._tray_checked = now

        details = self.feed.details()
        if details["alerts"]:
            mode = FaceMode.ALERT
        elif details["voice_state"]:
            mode = FaceMode(details["voice_state"])
        else:
            mode = FaceMode.OFFLINE
        active = mode is not FaceMode.OFFLINE

        key = (mode, active)
        if key == self._tray_key:
            return
        self._tray_key = key
        try:
            self._tray.update(
                image=_tray_image(Theme.for_mode(mode), active),
                tooltip=self.feed.status_text(),
            )
        except Exception as exc:
            log.debug("tray update failed: %s", exc)

    def toggle_avatar(self) -> None:
        self._avatar_visible = not self._avatar_visible

    def _hide_or_quit(self) -> None:
        if self._tray is not None:
            self._avatar_visible = False
        else:
            self.quit()

    def _speak_status(self) -> None:
        """Double-click or menu: have the assistant say how the PC is doing."""

        def run() -> None:
            try:
                from ..alerts import AlertEngine
                from ..commands import CommandContext, build_registry
                from ..jarvis import JarvisClient
                from ..monitors.collector import Collector

                ctx = CommandContext(
                    config=self.config,
                    collector=Collector(self.config.monitors),
                    alerts=AlertEngine(self.config.alerts, self.config.device.friendly_name),
                    jarvis=JarvisClient(self.config.jarvis),
                )
                result = build_registry().dispatch("query.system", {}, ctx)
                if result.speech:
                    ctx.jarvis.say(result.speech)
            except Exception as exc:
                log.error("status request failed: %s", exc)

        threading.Thread(target=run, daemon=True).start()

    # -- animation loop -----------------------------------------------------

    def _tick(self) -> None:
        if not self._running:
            return

        # Whatever the tray thread queued since the last frame.
        try:
            while True:
                self._actions.get_nowait()()
        except queue.Empty:
            pass
        if not self._running:  # an action may have been Exit
            return

        fresh = self._watcher.changed()
        if fresh is not None:
            self._apply_identity(fresh)

        now = time.monotonic()
        dt = min(0.25, now - self._last_tick)
        self._last_tick = now

        mode, levels, mouth = self.feed.current()
        self.animator.set_mode(mode)
        self.animator.set_levels(levels)
        self.animator.set_mouth(mouth)
        self._track_cursor()
        state = self.animator.tick(dt)

        self._refresh_tray()

        hidden = not self._avatar_visible or (
            self.config.face.hide_when_idle and state.mode is FaceMode.IDLE
        )
        if hidden:
            self.root.withdraw()
        else:
            if not self.root.winfo_viewable():
                self.root.deiconify()
            self._photo.paste(self.renderer.render(state))

        # Subtract the time this frame took rather than always waiting a full
        # interval, or the real rate settles well below the configured one.
        spent = time.monotonic() - now
        self.root.after(max(1, round((self._frame_interval - spent) * 1000)), self._tick)

    def run(self) -> int:
        self.feed.start()
        log.info("face running (%dpx, %d fps)", self.size, self.config.face.fps)
        self.root.after(0, self._tick)
        try:
            self.root.mainloop()
        except KeyboardInterrupt:
            pass
        finally:
            self.feed.stop()
        return 0

    def quit(self) -> None:
        self._running = False
        self.feed.stop()
        if self._tray is not None:
            self._tray.stop()
            self._tray = None
        try:
            self.root.destroy()
        except tk.TclError:
            pass


def _tray_image(theme: Theme, active: bool) -> Image.Image:
    """The orb reduced to what survives at 16 pixels: a ring and a core.

    Active gets a filled core in the mode's accent colour; inactive is a thin
    grey ring, so "Jarvis is there" and "Jarvis is not" read as different
    shapes, not just different colours.
    """
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((6, 6, 58, 58), outline=theme.accent + (255,), width=7)
    if active:
        draw.ellipse((21, 21, 43, 43), fill=theme.accent + (255,))
    else:
        draw.ellipse((24, 24, 40, 40), outline=theme.accent_dim + (255,), width=4)
    return img
