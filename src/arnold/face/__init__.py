"""An on-screen presence for the assistant.

A frameless, always-on-top window that reacts to what the assistant is doing:
idle, listening, thinking, speaking, or raising an alert. When the Pi-side
state bridge is installed it also follows Jarvis's real audio levels.

Two looks, chosen with `face.style`:

* **holo** - the J.A.R.V.I.S. core. A spinning cage of gold filament that
  flares and bristles in time with the voice. See `holo.py`.
* **orb** - the older cartoon face, with eyes, brows and a lip-synced mouth.
  See `render.py`.

Both take the same `FaceState`, so the animation in `state.py` is shared and
neither knows which one is on screen.
"""

from .state import FaceMode, FaceState

__all__ = ["FaceMode", "FaceState"]
