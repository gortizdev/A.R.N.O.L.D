"""Draws the face.

The body is rendered with Pillow at 2x and downscaled, because tkinter's
canvas has no antialiasing and a jagged face looks broken rather than
stylised.

The glow is deliberately *not* supersampled. It is a heavy Gaussian blur, so
it carries no detail that 2x could preserve, and blurring a quarter of the
pixels costs a quarter as much - which is most of what buys the higher frame
rate. It is composited after the downscale, at output resolution.

Windows gives a layered window exactly one transparent colour, so everything
is composited onto that key colour here and pixels that end up mostly
transparent are snapped to it exactly. That keeps the orb's edge clean instead
of leaving a coloured fringe around it.
"""

from __future__ import annotations

import math
from collections import OrderedDict
from dataclasses import dataclass

from PIL import Image, ImageChops, ImageDraw, ImageFilter

from .state import FaceMode, FaceState

# Chosen because nothing in the palette comes near it, so no part of the face
# is accidentally punched transparent.
TRANSPARENT_KEY = (255, 0, 255)

RGB = tuple[int, int, int]


@dataclass(frozen=True)
class Theme:
    accent: RGB
    accent_dim: RGB
    backdrop: RGB
    rim: RGB

    @staticmethod
    def for_mode(mode: FaceMode) -> "Theme":
        return _THEMES.get(mode, _THEMES[FaceMode.IDLE])


_THEMES: dict[FaceMode, Theme] = {
    FaceMode.IDLE: Theme((56, 202, 255), (26, 104, 140), (18, 34, 50), (44, 104, 138)),
    FaceMode.LISTENING: Theme((130, 242, 255), (44, 150, 185), (20, 42, 58), (62, 140, 172)),
    FaceMode.THINKING: Theme((176, 150, 255), (84, 64, 142), (28, 24, 48), (92, 74, 150)),
    FaceMode.SPEAKING: Theme((96, 228, 255), (36, 130, 168), (18, 38, 54), (54, 124, 156)),
    FaceMode.ALERT: Theme((255, 182, 48), (150, 100, 16), (44, 28, 10), (150, 102, 28)),
    FaceMode.OFFLINE: Theme((110, 122, 136), (52, 58, 66), (22, 25, 30), (56, 62, 70)),
}


def lerp(a: RGB, b: RGB, t: float) -> RGB:
    t = max(0.0, min(1.0, t))
    return (
        round(a[0] + (b[0] - a[0]) * t),
        round(a[1] + (b[1] - a[1]) * t),
        round(a[2] + (b[2] - a[2]) * t),
    )


def flatten_to_key(img: Image.Image) -> Image.Image:
    """Composite onto the key colour and harden the edge.

    Snaps near-transparent pixels to fully transparent first, so the colour key
    does not bleed into the antialiased rim as a magenta halo. A layered window
    has no per-pixel alpha to spend anyway: every pixel is either painted or
    punched out, and this is where that decision is made.
    """
    alpha = img.getchannel("A").point(lambda a: 0 if a < 128 else 255)
    img.putalpha(alpha)

    out = Image.new("RGB", img.size, TRANSPARENT_KEY)
    out.paste(img, (0, 0), img)
    return out


def _blend_theme(state: FaceState) -> Theme:
    """Cross-fade between the outgoing and incoming palettes."""
    current = Theme.for_mode(state.mode)
    if state.transition >= 1.0:
        return current
    previous = Theme.for_mode(state.previous_mode)
    t = state.transition
    return Theme(
        lerp(previous.accent, current.accent, t),
        lerp(previous.accent_dim, current.accent_dim, t),
        lerp(previous.backdrop, current.backdrop, t),
        lerp(previous.rim, current.rim, t),
    )


class FaceRenderer:
    # Base radius as a fraction of the window. Leaves headroom for the orb to
    # breathe and overshoot without touching the edge.
    BASE_RADIUS = 0.435
    # Gradient bands in the orb body. Beyond ~20 the steps are invisible.
    ORB_STEPS = 22
    # Orb bodies kept around, keyed by size and palette. Each frame's breathing
    # lands on one of a handful of integer sizes, so this hits almost always
    # once a mode has settled.
    BODY_CACHE = 16

    def __init__(self, size: int = 220, supersample: int = 2) -> None:
        self.size = size
        self.ss = max(1, supersample)
        self._canvas = size * self.ss
        self._bodies: OrderedDict[tuple, Image.Image] = OrderedDict()

    # -- geometry helpers ---------------------------------------------------

    @staticmethod
    def _box(cx: float, cy: float, rx: float, ry: float | None = None) -> list[float]:
        if ry is None:
            ry = rx
        return [cx - rx, cy - ry, cx + rx, cy + ry]

    # -- main entry point ---------------------------------------------------

    def render(self, state: FaceState) -> Image.Image:
        s = self._canvas
        theme = _blend_theme(state)

        cx = s / 2 + state.bob_x * s
        cy = s / 2 + state.bob_y * s
        r = s * self.BASE_RADIUS * state.scale
        # Squash conserves area roughly, so the orb reads as deforming rather
        # than just changing size.
        rx = r * (1.0 - state.squash * 0.5)
        ry = r * (1.0 + state.squash * 0.5)
        # Stacked pops (clicking repeatedly) can drive the spring past the
        # window; clip rather than let the orb render with a flat edge.
        limit = s * 0.49 - abs(state.bob_x * s) - abs(state.bob_y * s)
        rx, ry = min(rx, limit), min(ry, limit)

        img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        body = self._body(theme, int(rx * 2), int(ry * 2))
        img.paste(body, (int(cx - rx), int(cy - ry)))

        draw = ImageDraw.Draw(img, "RGBA")
        self._draw_rim(draw, cx, cy, rx, ry, r, theme, state)
        self._draw_brows(draw, cx, cy, rx, ry, r, theme, state)
        self._draw_eyes(draw, cx, cy, rx, ry, r, theme, state)
        self._draw_mouth(draw, cx, cy, ry, r, theme, state)

        out = img.resize((self.size, self.size), Image.BOX) if self.ss > 1 else img
        out.alpha_composite(self._glow(theme, state, cx / self.ss, cy / self.ss,
                                       rx / self.ss, ry / self.ss))
        return self._flatten(out)

    # -- layers -------------------------------------------------------------

    def _body(self, theme: Theme, w: int, h: int) -> Image.Image:
        """The orb itself: dark, lit from the centre so it reads as a sphere.

        Cached, because it is 20-odd filled ellipses and it only changes when
        the palette or the breathing size does.
        """
        w, h = max(2, w), max(2, h)
        key = (w, h, theme.backdrop, theme.accent)
        hit = self._bodies.get(key)
        if hit is not None:
            self._bodies.move_to_end(key)
            return hit

        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img, "RGBA")
        centre = lerp(theme.backdrop, theme.accent, 0.16)
        edge = lerp(theme.backdrop, (0, 0, 0), 0.55)
        cx, cy = w / 2, h / 2
        for i in range(self.ORB_STEPS, 0, -1):
            t = i / self.ORB_STEPS  # 1 at the rim, approaching 0 at the centre
            draw.ellipse(
                self._box(cx, cy, cx * t, cy * t), fill=(*lerp(centre, edge, t), 255)
            )

        self._bodies[key] = img
        if len(self._bodies) > self.BODY_CACHE:
            self._bodies.popitem(last=False)
        return img

    def _draw_rim(self, draw, cx, cy, rx, ry, r, theme: Theme, st: FaceState) -> None:
        """The resting heartbeat of the face, brightened by whatever it is doing."""
        w = max(2, int(r * 0.030))
        breath = 0.5 + 0.5 * math.sin(st.t * 1.6)
        rim = lerp(theme.rim, theme.accent, 0.16 + 0.20 * breath + 0.34 * st.energy)
        draw.ellipse(self._box(cx, cy, rx, ry), outline=(*rim, 255), width=w)

    def _glow(self, theme: Theme, st: FaceState, cx, cy, rx, ry) -> Image.Image:
        """Everything blurred: rings, sweeps, orbiting motes, the wake flash."""
        s = self.size
        r = (rx + ry) / 2
        w = max(1, int(r * 0.058))
        glow = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        gdraw = ImageDraw.Draw(glow, "RGBA")

        if st.mode == FaceMode.THINKING:
            # A rotating gap reads as work-in-progress without a spinner cliche.
            start = (st.t * 150) % 360
            gdraw.arc(
                self._box(cx, cy, rx * 0.90, ry * 0.90), start, start + 300,
                fill=(*theme.accent, 235), width=w,
            )
            # Motes running the other way, so the two never look like one wheel.
            for k in range(3):
                ang = math.radians(-st.t * 95 + k * 120)
                mr = r * 0.055
                gdraw.ellipse(
                    self._box(cx + math.cos(ang) * rx * 0.74,
                              cy + math.sin(ang) * ry * 0.74, mr),
                    fill=(*theme.accent, 200),
                )
        elif st.mode == FaceMode.LISTENING:
            # Expanding pulses, like something reaching outward to hear.
            for k in range(3):
                phase = (st.t * 0.85 + k / 3.0) % 1.0
                alpha = int(210 * (1 - phase) ** 1.7 * (0.55 + 0.45 * st.energy))
                if alpha > 6:
                    grow = 0.62 + 0.30 * phase
                    gdraw.ellipse(
                        self._box(cx, cy, rx * grow, ry * grow),
                        outline=(*theme.accent, alpha), width=w,
                    )
        elif st.mode == FaceMode.SPEAKING:
            pulse = 0.84 + 0.11 * st.level
            gdraw.ellipse(
                self._box(cx, cy, rx * pulse, ry * pulse),
                outline=(*theme.accent, int(150 + 105 * st.energy)), width=w,
            )
            # Sparks thrown off on the loud syllables.
            if st.level > 0.05:
                for k, band in enumerate(st.eq[::2]):
                    ang = math.radians(k * 45 - st.t * 40)
                    reach = 0.70 + 0.22 * band
                    sr = r * (0.020 + 0.045 * band)
                    gdraw.ellipse(
                        self._box(cx + math.cos(ang) * rx * reach,
                                  cy + math.sin(ang) * ry * reach, sr),
                        fill=(*theme.accent, int(90 + 150 * band)),
                    )
        elif st.mode == FaceMode.ALERT:
            # Deliberately urgent: fast, high-contrast throb.
            urgency = 0.5 + 0.5 * math.sin(st.t * 6.5)
            gdraw.ellipse(
                self._box(cx, cy, rx * 0.90, ry * 0.90),
                outline=(*lerp(theme.accent_dim, theme.accent, urgency), 240),
                width=w,
            )

        if st.flash > 0.01:
            # Announces a mode change: a ring that expands and fades out.
            grow = 0.32 + 0.62 * (1.0 - st.flash)
            gdraw.ellipse(
                self._box(cx, cy, rx * grow, ry * grow),
                outline=(*lerp(theme.accent, (255, 255, 255), 0.4), int(230 * st.flash)),
                width=max(1, int(w * 1.4)),
            )

        glow = glow.filter(ImageFilter.GaussianBlur(radius=max(1.0, self.size * 0.012)))
        # Clip the blur to the orb. Without this it bleeds past the silhouette,
        # and those faint pixels fall on both sides of the transparency
        # threshold - which shows up as a speckled fringe around the edge.
        mask = Image.new("L", (s, s), 0)
        ImageDraw.Draw(mask).ellipse(self._box(cx, cy, rx, ry), fill=255)
        glow.putalpha(ImageChops.multiply(glow.getchannel("A"), mask))
        return glow

    def _eye_geometry(self, cx, cy, rx, ry, r, st: FaceState):
        eye_dx = rx * 0.34
        eye_y = cy - ry * 0.18 + st.gaze_y * r
        eye_w = r * 0.20
        eye_h = r * 0.40

        if st.mode == FaceMode.THINKING:
            # Narrowed, as if concentrating: short *and* wide, or it just looks
            # like the eyes got smaller.
            eye_h *= 0.42
            eye_w *= 1.45
        elif st.mode == FaceMode.LISTENING:
            eye_h *= 1.18  # widened, attentive
        elif st.mode == FaceMode.ALERT:
            eye_h *= 1.10
        elif st.mode == FaceMode.SPEAKING:
            eye_h *= 1.0 + 0.14 * min(1.0, st.level * 1.8)
        return eye_dx, eye_y, eye_w, eye_h

    def _draw_eyes(self, draw, cx, cy, rx, ry, r, theme: Theme, st: FaceState) -> None:
        eye_dx, eye_y, eye_w, eye_h = self._eye_geometry(cx, cy, rx, ry, r, st)
        eye_h *= max(0.06, 1.0 - st.blink)

        for sign in (-1, 1):
            ex = cx + sign * eye_dx + st.gaze_x * r
            box = [ex - eye_w / 2, eye_y - eye_h / 2, ex + eye_w / 2, eye_y + eye_h / 2]
            draw.rounded_rectangle(box, radius=eye_w / 2, fill=(*theme.accent, 255))
            # A brighter core keeps the eyes from looking like flat blobs.
            inset = eye_w * 0.26
            if eye_h > inset * 2.2:
                draw.rounded_rectangle(
                    [box[0] + inset, box[1] + inset, box[2] - inset, box[3] - inset],
                    radius=max(1.0, (eye_w - inset * 2) / 2),
                    fill=(*lerp(theme.accent, (255, 255, 255), 0.55), 255),
                )

    def _draw_brows(self, draw, cx, cy, rx, ry, r, theme: Theme, st: FaceState) -> None:
        """Two short bars above the eyes.

        By far the cheapest expression available: angle alone is the difference
        between concerned, concentrating and delighted.
        """
        if st.mode == FaceMode.OFFLINE:
            return
        eye_dx, eye_y, eye_w, eye_h = self._eye_geometry(cx, cy, rx, ry, r, st)

        half = r * 0.16
        thick = max(2.0, r * 0.052)
        # Raised brows sit higher; a blink drags them down a little with the lid.
        lift = eye_h * 0.62 + r * 0.10 + st.brow * r * 0.075 - st.blink * r * 0.02
        # Only furrowing tilts the brows, dropping the inner ends - that angle
        # is what reads as anger. Raised brows stay level and simply sit
        # higher; tipping their inner ends up instead reads as worry.
        slope = r * (-st.brow * 0.10 if st.brow < 0 else -st.brow * 0.018)
        colour = (*lerp(theme.accent_dim, theme.accent, 0.55 + 0.45 * st.energy), 255)

        for sign in (-1, 1):
            bx = cx + sign * eye_dx + st.gaze_x * r * 0.55
            by = eye_y - lift
            inner = (bx - sign * half, by + slope)
            outer = (bx + sign * half, by - slope * 0.35)
            draw.line([inner, outer], fill=colour, width=int(thick))
            # Pillow has no round caps, so add them.
            for point in (inner, outer):
                draw.ellipse(self._box(point[0], point[1], thick / 2), fill=colour)

    def _draw_mouth(self, draw, cx, cy, ry, r, theme: Theme, st: FaceState) -> None:
        mouth_y = cy + ry * 0.34
        half_w = r * 0.52

        if st.mouth_open > 0.03:
            self._draw_talking_mouth(draw, cx, mouth_y, r, theme, st)
            return

        if st.mode == FaceMode.THINKING:
            # Three dots, filling left to right.
            spacing = r * 0.20
            for i in range(3):
                phase = (st.t * 1.8 - i * 0.28) % 1.6
                on = max(0.18, 1.0 - abs(phase - 0.4) * 1.6) if phase < 1.0 else 0.18
                rr = r * (0.040 + 0.014 * on)
                dx = cx + (i - 1) * spacing
                draw.ellipse(
                    self._box(dx, mouth_y, rr),
                    fill=(*lerp(theme.accent_dim, theme.accent, on), 255),
                )
            return

        if st.mode == FaceMode.OFFLINE:
            draw.line(
                [cx - half_w * 0.5, mouth_y, cx + half_w * 0.5, mouth_y],
                fill=(*theme.accent_dim, 255), width=max(2, int(r * 0.035)),
            )
            return

        width = max(2, int(r * 0.038))
        if st.mode == FaceMode.ALERT:
            # Inverted: the top half of the ellipse, so it frowns. An urgent
            # face that is still smiling reads as a bug.
            draw.arc(
                [cx - half_w * 0.62, mouth_y - r * 0.06,
                 cx + half_w * 0.62, mouth_y + r * 0.30],
                200, 340, fill=(*theme.accent, 255), width=width,
            )
            return

        # Resting: a gentle upward curve that deepens as the brows lift.
        curve = r * (0.10 + 0.05 * st.brow)
        draw.arc(
            [cx - half_w * 0.62, mouth_y - curve - r * 0.12,
             cx + half_w * 0.62, mouth_y + curve + r * 0.12],
            20, 160, fill=(*theme.accent, 255), width=width,
        )

    def _draw_talking_mouth(self, draw, cx, mouth_y, r, theme: Theme, st: FaceState) -> None:
        """A mouth that opens and changes shape, rather than a level meter.

        Two ellipses: the lips in the accent colour, and the cavity behind them
        in the orb's own dark. Height comes from the jaw, width from the vowel
        shape, so "ee" is a wide slot and "oo" a small round one.
        """
        openness = min(1.0, st.mouth_open)
        # A rounded mouth is narrow, a spread one is wide. The floor is set so
        # that a fully open, fully rounded mouth is about as wide as it is
        # tall: any narrower and every vowel reads as the same startled "oh".
        half_w = r * (0.26 + 0.22 * st.mouth_wide)
        half_h = r * (0.035 + 0.26 * openness)
        # Opening the jaw pulls the whole mouth down a little.
        mouth_y += r * 0.05 * openness

        lip = max(r * 0.028, half_h * 0.30)
        draw.ellipse(
            self._box(cx, mouth_y, half_w, half_h),
            fill=(*theme.accent, 255),
        )

        # The cavity only appears once there is room for it; below that the
        # mouth is just a bright line, which is what a closed mouth looks like
        # in this style.
        inner_h = half_h - lip
        inner_w = half_w - lip * 0.55
        if inner_h > r * 0.012 and inner_w > 0:
            draw.ellipse(
                self._box(cx, mouth_y + lip * 0.12, inner_w, inner_h),
                fill=(*lerp(theme.backdrop, (0, 0, 0), 0.45), 255),
            )
            # A hint of tongue at the bottom, so a wide-open mouth has depth
            # instead of being a hole.
            if openness > 0.45:
                tongue = (openness - 0.45) / 0.55
                draw.ellipse(
                    self._box(
                        cx,
                        mouth_y + inner_h * 0.52,
                        inner_w * 0.62,
                        inner_h * 0.45 * tongue,
                    ),
                    fill=(*lerp(theme.accent_dim, theme.accent, 0.35), 255),
                )

    # -- output -------------------------------------------------------------

    def _flatten(self, img: Image.Image) -> Image.Image:
        return flatten_to_key(img)
