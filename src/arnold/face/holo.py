"""The J.A.R.V.I.S. core: a spinning cage of gold filament, not a face.

What the films actually put on screen is a sphere woven out of light - rings,
meridians and a scatter of short traces - turning slowly, flaring and
bristling in time with the voice. This draws that.

Three things make it read as a solid object rather than a doodle:

* **It is genuinely 3-D.** Every filament is stored as unit vectors on a
  sphere, spun about the vertical each frame and projected orthographically,
  so the far side really does pass behind the near side instead of merely
  being drawn first.
* **Depth is carried by colour, not alpha.** The window is colour-keyed, so
  there is no per-pixel alpha to spend: a pixel is painted or it is punched
  out, nothing in between. Fading the far side towards the backdrop's own dark
  gold does the same job and survives the flattening intact.
* **The voice drives the geometry, not just the brightness.** Loudness widens
  the sphere, and each band of the spectrum lengthens its own spikes in the
  corona, so speech makes the whole thing pulse and bristle rather than simply
  glow harder.

The pulse is driven from `mouth_open` where that exists, because it is
measured from the real PCM and timed to when the audio is actually heard;
`level` is the fallback when nothing better is available.
"""

from __future__ import annotations

import logging
import math
import random
from collections import OrderedDict
from dataclasses import dataclass

from PIL import Image, ImageChops, ImageDraw, ImageFilter

from .render import RGB, flatten_to_key, lerp
from .state import EQ_BANDS, FaceMode, FaceState

log = logging.getLogger(__name__)

Vec3 = tuple[float, float, float]


@dataclass(frozen=True)
class HoloTheme:
    # The core, near-white: what the hottest part of the hologram burns at.
    hot: RGB
    # Filaments on the near side.
    bright: RGB
    # Filaments crossing the silhouette.
    wire: RGB
    # Filaments on the far side, only just above the backdrop.
    dim: RGB
    # The dark the whole thing is suspended in.
    backdrop: RGB

    @staticmethod
    def for_mode(mode: FaceMode, palette: str = "gold") -> "HoloTheme":
        themes = PALETTES.get(palette, PALETTES["gold"])
        return themes.get(mode, themes[FaceMode.IDLE])


# Gold throughout, because that is the colour of the thing being imitated.
# Modes separate themselves by temperature - cool amber at rest, deep orange
# while working - rather than by hopping around the wheel, which would stop it
# looking like one object.
#
# Listening is the one exception. It is the state that has to be read at a
# glance from across the room, and in a quiet room a slightly whiter gold is
# not that: the mic is open and nothing on screen says so. So it goes
# ice-white, the one colour that cannot be mistaken for a warm idle.
_GOLD: dict[FaceMode, HoloTheme] = {
    FaceMode.IDLE: HoloTheme(
        (255, 232, 186), (255, 182, 74), (238, 136, 24), (112, 54, 10), (13, 8, 3)
    ),
    FaceMode.LISTENING: HoloTheme(
        (255, 255, 255), (232, 242, 255), (150, 196, 255), (48, 78, 130), (5, 9, 18)
    ),
    FaceMode.THINKING: HoloTheme(
        (255, 222, 160), (255, 158, 46), (224, 104, 12), (100, 42, 6), (14, 7, 2)
    ),
    FaceMode.SPEAKING: HoloTheme(
        (255, 248, 226), (255, 200, 100), (255, 150, 32), (130, 62, 12), (16, 9, 4)
    ),
    FaceMode.ALERT: HoloTheme(
        (255, 226, 198), (255, 108, 48), (226, 52, 16), (96, 20, 8), (20, 5, 3)
    ),
    FaceMode.OFFLINE: HoloTheme(
        (152, 160, 170), (98, 106, 118), (62, 68, 78), (30, 33, 38), (9, 10, 12)
    ),
}

# The other machine. Cool cyan at rest, so a glance says which assistant this
# is; listening swings to violet-white, thinking to teal, speaking to a bright
# ice-blue. Alert and offline are shared - red is red on any machine.
_STEEL: dict[FaceMode, HoloTheme] = {
    FaceMode.IDLE: HoloTheme(
        (214, 242, 255), (110, 196, 238), (36, 138, 196), (16, 58, 92), (3, 8, 13)
    ),
    FaceMode.LISTENING: HoloTheme(
        (255, 255, 255), (240, 232, 255), (196, 168, 255), (86, 64, 150), (10, 6, 22)
    ),
    FaceMode.THINKING: HoloTheme(
        (200, 255, 236), (90, 220, 180), (24, 168, 128), (10, 66, 52), (2, 10, 8)
    ),
    FaceMode.SPEAKING: HoloTheme(
        (240, 252, 255), (170, 226, 255), (70, 180, 240), (24, 80, 130), (4, 10, 16)
    ),
    FaceMode.ALERT: _GOLD[FaceMode.ALERT],
    FaceMode.OFFLINE: _GOLD[FaceMode.OFFLINE],
}

PALETTES: dict[str, dict[FaceMode, HoloTheme]] = {"gold": _GOLD, "steel": _STEEL}


@dataclass(frozen=True)
class HoloDesign:
    """The shape of the thing, as opposed to its colour.

    Two assistants in one house should not be the same sphere in two paints.
    A design fixes the geometry: how the cage is ruled, how the traces lie,
    what orbits it, what sits at the centre, and which way it turns.
    """

    # Latitude rings, in degrees, and the number of meridians.
    latitudes: tuple[float, ...]
    meridians: int
    # Short traces scattered on the surface: how many, and the span (radians)
    # each is drawn from.
    filaments: int
    filament_span: tuple[float, float]
    # Stubs standing off the surface, one band of the spectrum each.
    studs: int
    # Corona spikes around the rim.
    spikes: int
    # Rings orbiting outside the sphere: (radius, pitch degrees, yaw).
    halos: tuple[tuple[float, float, float], ...]
    # The knot at the middle, same triples.
    core_rings: tuple[tuple[float, float, float], ...]
    # Radians per second at rest; negative turns the other way.
    spin: float
    tilt_degrees: float
    # The gauge ring just inside the rim: tick count, and every Nth is long.
    ticks: int
    tick_every: int


DESIGNS: dict[str, HoloDesign] = {
    # The J.A.R.V.I.S. core as it has always been drawn here.
    "core": HoloDesign(
        latitudes=(-58.0, -30.0, -4.0, 24.0, 52.0),
        meridians=6,
        filaments=120,
        filament_span=(0.18, 0.62),
        studs=160,
        spikes=76,
        halos=((1.11, 22.0, 0.0), (1.21, 74.0, 1.1)),
        core_rings=((0.30, 8.0, 0.0), (0.24, 68.0, 0.6), (0.36, 120.0, 2.0)),
        spin=0.40,
        tilt_degrees=19.0,
        ticks=40,
        tick_every=5,
    ),
    # Arnold. A regular lattice rather than a city seen from orbit: fewer,
    # longer traces over an evenly ruled globe, densely studded like a plot
    # of data points; a gyroscope of three equal rings at the centre instead
    # of a knot; one wide near-equatorial halo and one small polar one; and
    # it turns the other way, tipped further over.
    "lattice": HoloDesign(
        latitudes=(-48.0, -16.0, 16.0, 48.0),
        meridians=9,
        filaments=36,
        filament_span=(0.55, 1.25),
        studs=240,
        spikes=48,
        halos=((1.15, 86.0, 0.35), (1.27, 12.0, 2.4)),
        core_rings=((0.21, 4.0, 0.0), (0.21, 88.0, 0.9), (0.21, 46.0, 1.9)),
        spin=-0.32,
        tilt_degrees=27.0,
        ticks=60,
        tick_every=6,
    ),
}


def _blend_theme(state: FaceState, palette: str = "gold") -> HoloTheme:
    current = HoloTheme.for_mode(state.mode, palette)
    if state.transition >= 1.0:
        return current
    previous = HoloTheme.for_mode(state.previous_mode, palette)
    t = state.transition
    return HoloTheme(
        lerp(previous.hot, current.hot, t),
        lerp(previous.bright, current.bright, t),
        lerp(previous.wire, current.wire, t),
        lerp(previous.dim, current.dim, t),
        lerp(previous.backdrop, current.backdrop, t),
    )


# -- geometry, built once -------------------------------------------------


def _unit(lon: float, lat: float) -> Vec3:
    """A point on the unit sphere. Y is the spin axis."""
    c = math.cos(lat)
    return (c * math.cos(lon), math.sin(lat), c * math.sin(lon))


def _latitude(lat: float, samples: int) -> list[Vec3]:
    step = 2 * math.pi / samples
    return [_unit(i * step, lat) for i in range(samples + 1)]


def _meridian(lon: float, samples: int) -> list[Vec3]:
    """A great circle through both poles."""
    cl, sl = math.cos(lon), math.sin(lon)
    step = 2 * math.pi / samples
    out = []
    for i in range(samples + 1):
        ca, sa = math.cos(i * step), math.sin(i * step)
        out.append((ca * cl, sa, ca * sl))
    return out


def _random_unit(rng: random.Random) -> Vec3:
    """Uniform on the sphere - sampling the two angles instead clumps at the poles."""
    y = rng.uniform(-1.0, 1.0)
    a = rng.uniform(0.0, 2 * math.pi)
    r = math.sqrt(max(0.0, 1.0 - y * y))
    return (r * math.cos(a), y, r * math.sin(a))


def _perpendicular(p: Vec3, rng: random.Random) -> Vec3:
    """Some unit vector at right angles to p, chosen at random."""
    while True:
        q = _random_unit(rng)
        x = p[1] * q[2] - p[2] * q[1]
        y = p[2] * q[0] - p[0] * q[2]
        z = p[0] * q[1] - p[1] * q[0]
        n = math.sqrt(x * x + y * y + z * z)
        if n > 1e-3:
            return (x / n, y / n, z / n)


def _circle(u: Vec3, v: Vec3, radius: float, samples: int) -> list[Vec3]:
    """A full circle of the given radius in the plane spanned by u and v."""
    step = 2 * math.pi / samples
    out = []
    for i in range(samples + 1):
        c, s = math.cos(i * step) * radius, math.sin(i * step) * radius
        out.append((u[0] * c + v[0] * s, u[1] * c + v[1] * s, u[2] * c + v[2] * s))
    return out


def _tipped(pitch: float, yaw: float) -> tuple[Vec3, Vec3]:
    """A basis for the equatorial plane, pitched over and then swung round."""
    cp, sp = math.cos(pitch), math.sin(pitch)
    cy, sy = math.cos(yaw), math.sin(yaw)
    u = (cy, 0.0, sy)
    v = (-sy * cp, -sp, cy * cp)
    return u, v


def _arc(p: Vec3, tangent: Vec3, span: float, samples: int) -> list[Vec3]:
    """A short piece of the great circle through p in the direction of tangent."""
    out = []
    for i in range(samples + 1):
        a = span * (i / samples - 0.5)
        c, s = math.cos(a), math.sin(a)
        out.append(
            (
                p[0] * c + tangent[0] * s,
                p[1] * c + tangent[1] * s,
                p[2] * c + tangent[2] * s,
            )
        )
    return out


class HoloRenderer:
    """Draws the core. Same contract as FaceRenderer: a state in, an RGB frame out."""

    # Radius of the sphere itself as a fraction of the window. Small enough
    # that the corona has somewhere to go.
    BASE_RADIUS = 0.345
    # How far a fully extended spike reaches, as a multiple of that radius.
    # Everything is clamped so this stays inside the window.
    CORONA_MAX = 1.28
    # Where the spikes start - just outside the sphere's silhouette.
    CORONA_BASE = 1.015
    # Radians per second at rest. Slow: a fast spin looks like a loading icon.
    SPIN = 0.40
    # Radians per second of the listening beacon's pulse: about 40 beats a
    # minute, a calm attentive rate rather than an alarm.
    LISTEN_BEAT = 4.2
    # Tipped away from edge-on, so the latitude rings read as rings.
    TILT = math.radians(19.0)

    LATITUDES = (-58.0, -30.0, -4.0, 24.0, 52.0)
    MERIDIANS = 6
    FILAMENT_SPAN = (0.18, 0.62)
    TICKS = 40
    TICK_EVERY = 5
    RING_SAMPLES = 40
    FILAMENTS = 120
    STUDS = 160
    SPIKES = 76
    # Rings orbiting outside the sphere. The one thing in the reference that is
    # not part of the cage: a couple of arcs swung round the outside of it.
    HALOS = ((1.11, 22.0, 0.0), (1.21, 74.0, 1.1))
    # The knot at the middle. Small, tight, and bright - it is what the eye
    # settles on, so it has to be structure rather than a blur.
    CORE_RINGS = ((0.30, 8.0, 0.0), (0.24, 68.0, 0.6), (0.36, 120.0, 2.0))
    # Depth is quantised into this many shades so runs of filament sharing one
    # colour can go out as a single polyline instead of a call per segment.
    SHADES = 12

    DISC_STEPS = 12
    DISC_CACHE = 16

    def __init__(
        self,
        size: int = 220,
        supersample: int = 2,
        palette: str = "gold",
        design: str = "core",
    ) -> None:
        self.size = size
        self.ss = max(1, supersample)
        if palette not in PALETTES:
            log.warning("face.palette %r is not one of %s; using gold",
                        palette, ", ".join(sorted(PALETTES)))
            palette = "gold"
        self.palette = palette
        if design not in DESIGNS:
            log.warning("face.design %r is not one of %s; using core",
                        design, ", ".join(sorted(DESIGNS)))
            design = "core"
        self.design = design
        # The design overrides the class-level geometry constants on this
        # instance; everything below reads them through self.
        d = DESIGNS[design]
        self.LATITUDES = d.latitudes
        self.MERIDIANS = d.meridians
        self.FILAMENTS = d.filaments
        self.FILAMENT_SPAN = d.filament_span
        self.STUDS = d.studs
        self.SPIKES = d.spikes
        self.HALOS = d.halos
        self.CORE_RINGS = d.core_rings
        self.SPIN = d.spin
        self.TILT = math.radians(d.tilt_degrees)
        self.TICKS = d.ticks
        self.TICK_EVERY = d.tick_every
        self._canvas = size * self.ss
        self._discs: OrderedDict[tuple, Image.Image] = OrderedDict()
        # The cage's yaw, integrated frame by frame. It cannot be `t * spin`:
        # the rate changes with mode and energy, and a rate change would then
        # multiply the whole uptime into a jump, so the sphere whipped round
        # on every breath of energy and got worse the longer it had been up.
        self._yaw = 0.0
        self._yaw_t: float | None = None
        self._build()

    # -- structure ----------------------------------------------------------

    def _build(self) -> None:
        """Lay out the cage once. Seeded, so the sphere is the same object every run."""
        rng = random.Random(0x5EED)

        self._rings: list[list[Vec3]] = [
            _latitude(math.radians(lat), self.RING_SAMPLES) for lat in self.LATITUDES
        ]
        self._rings += [
            _meridian(math.pi * i / self.MERIDIANS, self.RING_SAMPLES)
            for i in range(self.MERIDIANS)
        ]

        self._halos = [
            _circle(*_tipped(math.radians(pitch), yaw), radius, self.RING_SAMPLES)
            for radius, pitch, yaw in self.HALOS
        ]
        self._core = [
            _circle(*_tipped(math.radians(pitch), yaw), radius, 24)
            for radius, pitch, yaw in self.CORE_RINGS
        ]

        # Short traces scattered over the surface. These are what turn a tidy
        # wireframe globe into something that looks like a city seen from
        # orbit, which is the texture the films use.
        self._filaments: list[list[Vec3]] = []
        for _ in range(self.FILAMENTS):
            p = _random_unit(rng)
            span = rng.uniform(*self.FILAMENT_SPAN)
            self._filaments.append(_arc(p, _perpendicular(p, rng), span, 3))

        # Stubs standing off the surface. Each is tied to one band of the
        # spectrum and grows when that band is loud, so speech makes the
        # sphere bristle from the inside as well as around the rim.
        self._studs: list[tuple[Vec3, float, int]] = [
            (_random_unit(rng), rng.uniform(0.02, 0.085), i % EQ_BANDS)
            for i in range(self.STUDS)
        ]

        # The corona sits in screen space rather than on the sphere: it is the
        # readout, and a readout that rotated away from you would be useless.
        # Bands are mirrored left to right so there is no seam where the
        # spectrum wraps.
        self._spikes: list[tuple[float, float, int]] = []
        for k in range(self.SPIKES):
            frac = k / self.SPIKES
            mirrored = abs(((frac * 2.0) % 2.0) - 1.0)
            band = min(EQ_BANDS - 1, int(mirrored * EQ_BANDS))
            self._spikes.append((frac * 2 * math.pi, rng.uniform(0.35, 1.0), band))

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _box(cx: float, cy: float, rx: float, ry: float | None = None) -> list[float]:
        if ry is None:
            ry = rx
        return [cx - rx, cy - ry, cx + rx, cy + ry]

    def _shades(self, theme: HoloTheme, energy: float) -> list[tuple[int, int, int, int]]:
        """The depth ramp for this frame: far side to near side, dim to hot."""
        out = []
        for i in range(self.SHADES):
            t = i / (self.SHADES - 1)
            if t < 0.5:
                colour = lerp(theme.dim, theme.wire, t * 2.0)
            else:
                colour = lerp(theme.wire, theme.bright, (t - 0.5) * 2.0)
            # Excitement pulls the near side towards white without washing out
            # the far side, which is what keeps the sphere looking round.
            colour = lerp(colour, theme.hot, 0.30 * energy * t)
            out.append((*colour, 255))
        return out

    def _halo_shades(self, theme: HoloTheme, energy: float) -> list[tuple[int, int, int, int]]:
        """Depth ramp for the rings orbiting outside the sphere.

        These are the only filaments with bare desktop behind them, so here
        depth *is* alpha: the far half falls under the transparency threshold
        and vanishes, which is exactly the occlusion it should have.
        """
        out = []
        for i in range(self.SHADES):
            t = i / (self.SHADES - 1)
            colour = lerp(theme.wire, theme.hot, (0.15 + 0.65 * t) * (0.7 + 0.3 * energy))
            out.append((*colour, int(255 * t**1.4)))
        return out

    def _project(self, pts: list[Vec3], view: tuple) -> list[tuple[float, float, int]]:
        """Spin, tip, and flatten onto the screen. Returns x, y and a shade index."""
        cx, cy, rx, ry, cos_y, sin_y, cos_t, sin_t = view
        top = self.SHADES - 1
        out = []
        for x, y, z in pts:
            xr = x * cos_y + z * sin_y
            zr = z * cos_y - x * sin_y
            yt = y * cos_t - zr * sin_t
            zt = y * sin_t + zr * cos_t
            # Clamped, because the halo rings sit outside the unit sphere and
            # would otherwise index past the end of the ramp.
            shade = int((zt + 1.0) * 0.5 * top)
            out.append((cx + xr * rx, cy - yt * ry, 0 if shade < 0 else min(shade, top)))
        return out

    def _draw_path(self, draw, pts, shades, width: int) -> None:
        """Draw a projected path, breaking it wherever the depth shade changes."""
        if len(pts) < 2:
            return
        run = [(pts[0][0], pts[0][1])]
        current = None
        for i in range(1, len(pts)):
            a, b = pts[i - 1], pts[i]
            shade = (a[2] + b[2]) // 2
            if current is None:
                current = shade
            elif shade != current:
                draw.line(run, fill=shades[current], width=width)
                run = [(a[0], a[1])]
                current = shade
            run.append((b[0], b[1]))
        if current is not None and len(run) > 1:
            draw.line(run, fill=shades[current], width=width)

    # -- main entry point ---------------------------------------------------

    def _advance_yaw(self, t: float, spin: float) -> float:
        """Turn the cage by the current rate since the last frame; return the yaw.

        A stalled frame (or the clock going backwards after a reset) advances
        by at most a quarter second, so a hitch never reads as a lurch.
        """
        if self._yaw_t is not None:
            dt = t - self._yaw_t
            if 0.0 < dt:
                self._yaw += spin * min(dt, 0.25)
        self._yaw_t = t
        self._yaw %= 2 * math.pi
        return self._yaw

    def render(self, state: FaceState) -> Image.Image:
        s = self._canvas
        theme = _blend_theme(state, self.palette)
        # The lip-sync envelope when there is one - it is measured from the
        # real audio and timed to when it is heard, so the sphere pulses on the
        # syllable rather than a moment after it.
        voice = max(state.level, state.mouth_open)

        cx = s / 2 + state.bob_x * s
        cy = s / 2 + state.bob_y * s
        r = s * self.BASE_RADIUS * state.scale * (1.0 + 0.07 * voice)
        # Stacked pops can drive the spring past the window; clamp on the
        # corona's reach, not the sphere's, or the spikes clip against the edge.
        limit = s * 0.492 - abs(state.bob_x * s) - abs(state.bob_y * s)
        r = min(r, limit / self.CORONA_MAX)
        # A wireframe sphere squashes less convincingly than a solid one, so
        # take only a third of the spring's deformation.
        rx = r * (1.0 - state.squash * 0.34)
        ry = r * (1.0 + state.squash * 0.34)

        spin = self.SPIN * (1.0 + 0.9 * state.energy)
        if state.mode is FaceMode.THINKING:
            spin *= 2.3
        elif state.mode is FaceMode.OFFLINE:
            spin *= 0.25
        angle = self._advance_yaw(state.t, spin)
        # The gaze is still worth having without eyes to point: leaning the
        # whole sphere towards the cursor is a solid object turning to face
        # you, which is a better trick than a pair of eyes anyway. It is a
        # rotation rather than a shift, so it cannot walk the silhouette off
        # the edge of the window.
        angle += state.gaze_x * 1.3
        tilt = self.TILT + state.gaze_y * 1.1
        view = (
            cx, cy, rx, ry,
            math.cos(angle), math.sin(angle),
            math.cos(tilt), math.sin(tilt),
        )

        img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        img.paste(self._disc(theme, int(rx * 2), int(ry * 2)), (int(cx - rx), int(cy - ry)))

        draw = ImageDraw.Draw(img, "RGBA")
        shades = self._shades(theme, state.energy)
        self._draw_cage(draw, view, shades, r)
        self._draw_core(draw, view, theme, r, voice)
        self._draw_studs(draw, view, shades, r, state)
        self._draw_horizon(draw, cx, cy, rx, ry, r, theme, state)
        self._draw_corona(draw, cx, cy, rx, ry, r, theme, state, voice)
        img.alpha_composite(self._halos_layer(view, theme, state.energy, r))

        out = img.resize((self.size, self.size), Image.BOX) if self.ss > 1 else img
        k = self.ss
        out.alpha_composite(
            self._glow(theme, state, voice, cx / k, cy / k, rx / k, ry / k)
        )
        return flatten_to_key(out)

    # -- layers -------------------------------------------------------------

    def _disc(self, theme: HoloTheme, w: int, h: int) -> Image.Image:
        """The dark the sphere hangs in.

        Opaque, and only as wide as the sphere: the colour key gives binary
        transparency, so faint light has nowhere to land unless there is
        something solid under it. Everything beyond this - the corona - is
        drawn bright enough to stand on the desktop by itself.

        Cached, because it is a dozen filled ellipses that only change when the
        palette or the breathing size does.
        """
        w, h = max(2, w), max(2, h)
        key = (w, h, theme.backdrop, theme.wire)
        hit = self._discs.get(key)
        if hit is not None:
            self._discs.move_to_end(key)
            return hit

        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img, "RGBA")
        centre = lerp(theme.backdrop, theme.wire, 0.30)
        edge = lerp(theme.backdrop, (0, 0, 0), 0.55)
        cx, cy = w / 2, h / 2
        for i in range(self.DISC_STEPS, 0, -1):
            t = i / self.DISC_STEPS
            draw.ellipse(
                self._box(cx, cy, cx * t, cy * t), fill=(*lerp(centre, edge, t), 255)
            )

        self._discs[key] = img
        if len(self._discs) > self.DISC_CACHE:
            self._discs.popitem(last=False)
        return img

    def _draw_cage(self, draw, view, shades, r: float) -> None:
        ring_w = max(1, int(r * 0.012))
        thread_w = max(1, int(r * 0.008))
        for ring in self._rings:
            self._draw_path(draw, self._project(ring, view), shades, ring_w)
        for arc in self._filaments:
            self._draw_path(draw, self._project(arc, view), shades, thread_w)

    def _halos_layer(self, view, theme: HoloTheme, energy: float, r: float) -> Image.Image:
        """The orbiting rings, on their own layer.

        They are the only thing drawn with real alpha, and ImageDraw in RGBA
        mode overwrites the destination's alpha rather than compositing with
        it - drawing them straight onto the sphere would punch a translucent
        hole through it. So they go on a layer of their own and are composited
        properly.
        """
        layer = Image.new("RGBA", (self._canvas, self._canvas), (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer, "RGBA")
        shades = self._halo_shades(theme, energy)
        width = max(2, int(r * 0.018))
        for halo in self._halos:
            self._draw_path(draw, self._project(halo, view), shades, width)
        return layer

    def _draw_core(self, draw, view, theme: HoloTheme, r: float, voice: float) -> None:
        """The knot at the centre, spun the other way so it reads as separate."""
        cx, cy, rx, ry, cos_y, sin_y, cos_t, sin_t = view
        # Counter-rotation: swap the sign of the yaw the view was built with.
        inner = (cx, cy, rx, ry, cos_y, -sin_y, cos_t, sin_t)
        near = lerp(theme.bright, theme.hot, 0.25 + 0.75 * voice)
        far = lerp(theme.wire, theme.bright, 0.3 + 0.5 * voice)
        shades = [
            (*lerp(far, near, i / (self.SHADES - 1)), 255) for i in range(self.SHADES)
        ]
        for ring in self._core:
            self._draw_path(draw, self._project(ring, inner), shades, max(1, int(r * 0.010)))

    def _draw_studs(self, draw, view, shades, r: float, st: FaceState) -> None:
        """Stubs standing off the surface, lengthened by their own audio band."""
        cx, cy, rx, ry, cos_y, sin_y, cos_t, sin_t = view
        top = self.SHADES - 1
        eq = st.eq
        width = max(1, int(r * 0.011))
        for (x, y, z), length, band in self._studs:
            xr = x * cos_y + z * sin_y
            zr = z * cos_y - x * sin_y
            yt = y * cos_t - zr * sin_t
            zt = y * sin_t + zr * cos_t
            # The far side is behind the sphere; drawing it only adds clutter
            # the eye cannot resolve.
            if zt < -0.55:
                continue
            reach = 1.0 + length + 0.16 * eq[band]
            shade = int((zt + 1.0) * 0.5 * top)
            draw.line(
                [
                    (cx + xr * rx, cy - yt * ry),
                    (cx + xr * rx * reach, cy - yt * ry * reach),
                ],
                fill=shades[shade],
                width=width,
            )

    def _draw_horizon(self, draw, cx, cy, rx, ry, r, theme: HoloTheme, st: FaceState) -> None:
        """The bright rim, and the ticked ring just inside it.

        The rim is the resting heartbeat - it breathes on its own and brightens
        with whatever the assistant is doing.
        """
        breath = 0.5 + 0.5 * math.sin(st.t * 1.5)
        rim = lerp(theme.wire, theme.hot, 0.06 + 0.14 * breath + 0.34 * st.energy)
        draw.ellipse(
            self._box(cx, cy, rx * 0.995, ry * 0.995),
            outline=(*rim, 255),
            width=max(2, int(r * 0.011)),
        )

        # A gauge ring, counter-rotating so it never looks welded to the cage.
        tick_w = max(1, int(r * 0.014))
        spin = -st.t * 0.22
        colour = (*lerp(theme.dim, theme.bright, 0.35 + 0.4 * st.energy), 255)
        for k in range(self.TICKS):
            a = spin + k * 2 * math.pi / self.TICKS
            long = k % self.TICK_EVERY == 0
            inner = 0.90 if long else 0.935
            ca, sa = math.cos(a), math.sin(a)
            draw.line(
                [
                    (cx + ca * rx * inner, cy - sa * ry * inner),
                    (cx + ca * rx * 0.965, cy - sa * ry * 0.965),
                ],
                fill=colour,
                width=tick_w,
            )

    def _draw_corona(
        self, draw, cx, cy, rx, ry, r, theme: HoloTheme, st: FaceState, voice: float
    ) -> None:
        """Spikes radiating past the silhouette, one per band of the spectrum.

        This is the pulse: at rest it is a ring of short ticks, and speech
        drives each spike out in time with its own band, so the sphere throws
        light outwards on every syllable.
        """
        if st.mode is FaceMode.OFFLINE:
            return
        # Wide enough that the halved resolution still leaves a solid core -
        # these are the only marks with nothing but desktop behind them, so a
        # thin one would be eaten by the transparency threshold.
        width = max(2, int(r * 0.014))
        gain = 0.24 if st.mode is FaceMode.SPEAKING else 0.16
        idle = 0.5 + 0.5 * math.sin(st.t * 1.9)
        # Listening swells the whole corona in time with the rim beacon, so
        # the silhouette itself is visibly breathing harder than at rest.
        swell = (
            0.06 * (0.5 + 0.5 * math.sin(st.t * self.LISTEN_BEAT))
            if st.mode is FaceMode.LISTENING
            else 0.0
        )

        for angle, weight, band in self._spikes:
            level = st.eq[band]
            reach = (
                self.CORONA_BASE
                + weight * (0.045 + 0.020 * idle + swell)
                + gain * weight * level
            )
            reach = min(reach, self.CORONA_MAX)
            hot = min(1.0, level * 1.4)
            colour = (*lerp(theme.wire, theme.hot, 0.15 + 0.85 * hot), 255)
            ca, sa = math.cos(angle), math.sin(angle)
            draw.line(
                [
                    (cx + ca * rx * 0.98, cy - sa * ry * 0.98),
                    (cx + ca * rx * reach, cy - sa * ry * reach),
                ],
                fill=colour,
                width=width,
            )

        # A crest riding round the corona keeps it alive when nothing is
        # speaking, and reads as a scan when something is.
        crest = (st.t * 0.9) % (2 * math.pi)
        for k in range(3):
            a = crest + k * 0.09
            ca, sa = math.cos(a), math.sin(a)
            draw.line(
                [
                    (cx + ca * rx * 0.98, cy - sa * ry * 0.98),
                    (cx + ca * rx * (self.CORONA_BASE + 0.10 + 0.10 * voice),
                     cy - sa * ry * (self.CORONA_BASE + 0.10 + 0.10 * voice)),
                ],
                fill=(*theme.hot, 255),
                width=width,
            )

    def _glow(self, theme: HoloTheme, st: FaceState, voice, cx, cy, rx, ry) -> Image.Image:
        """Everything blurred: the hot core, the pulse rings, the working sweep.

        Composited after the downscale and clipped to the sphere. Blur carries
        no detail that supersampling would preserve, and blurring a quarter of
        the pixels costs a quarter as much.
        """
        s = self.size
        r = (rx + ry) / 2
        w = max(1, int(r * 0.040))
        glow = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        g = ImageDraw.Draw(glow, "RGBA")

        # The core. Bright at rest, white-hot and swollen on a loud syllable -
        # but kept tight, because the structure drawn under it is the point and
        # a wide bloom would simply erase it.
        core = r * (0.055 + 0.075 * voice + 0.025 * st.energy)
        g.ellipse(
            self._box(cx, cy, core * 3.0),
            fill=(*theme.bright, int(40 + 60 * voice)),
        )
        g.ellipse(self._box(cx, cy, core), fill=(*theme.hot, int(170 + 85 * voice)))

        if st.mode is FaceMode.LISTENING:
            # Rings running *inward* - the sphere drawing sound in - at full
            # strength whether or not anyone has started talking. Listening
            # used to share the speaking rings, whose brightness follows the
            # mic; in a silent room that left the open mic looking like idle
            # with the lights up.
            for k in range(3):
                phase = (st.t * 1.1 + k / 3.0) % 1.0
                alpha = int(235 * phase ** 1.4)
                if alpha > 6:
                    grow = 0.96 - 0.62 * phase
                    g.ellipse(
                        self._box(cx, cy, rx * grow, ry * grow),
                        outline=(*theme.hot, alpha),
                        width=w,
                    )
            # And a beacon at the rim: one unbroken bright ring, beating
            # rather than flying, so there is never a frame without it.
            beat = 0.5 + 0.5 * math.sin(st.t * self.LISTEN_BEAT)
            g.ellipse(
                self._box(cx, cy, rx * 0.90, ry * 0.90),
                outline=(*theme.hot, int(150 + 105 * beat)),
                width=max(1, int(w * 1.6)),
            )
        elif st.mode is FaceMode.SPEAKING:
            # Rings running outward from the core, thrown harder the louder it
            # is. Three of them, evenly out of phase, so there is always one in
            # flight.
            for k in range(3):
                phase = (st.t * 1.35 + k / 3.0) % 1.0
                alpha = int(210 * (1 - phase) ** 1.6 * (0.25 + 0.75 * voice))
                if alpha > 6:
                    grow = 0.18 + 0.78 * phase
                    g.ellipse(
                        self._box(cx, cy, rx * grow, ry * grow),
                        outline=(*theme.hot, alpha),
                        width=w,
                    )
        elif st.mode is FaceMode.THINKING:
            # A rotating gap: work in progress, without a spinner cliche.
            start = (st.t * 150) % 360
            g.arc(
                self._box(cx, cy, rx * 0.72, ry * 0.72),
                start, start + 290,
                fill=(*theme.hot, 225),
                width=w,
            )
        elif st.mode is FaceMode.ALERT:
            urgency = 0.5 + 0.5 * math.sin(st.t * 6.5)
            g.ellipse(
                self._box(cx, cy, rx * 0.86, ry * 0.86),
                outline=(*lerp(theme.wire, theme.hot, urgency), 240),
                width=w,
            )

        if st.flash > 0.01:
            grow = 0.30 + 0.66 * (1.0 - st.flash)
            g.ellipse(
                self._box(cx, cy, rx * grow, ry * grow),
                outline=(*lerp(theme.hot, (255, 255, 255), 0.4), int(230 * st.flash)),
                width=max(1, int(w * 1.4)),
            )

        glow = glow.filter(ImageFilter.GaussianBlur(radius=max(1.0, self.size * 0.008)))
        # Clip to the sphere. Without this the blur bleeds past the silhouette,
        # and those faint pixels fall on both sides of the transparency
        # threshold - which shows up as a speckled fringe around the edge.
        mask = Image.new("L", (s, s), 0)
        ImageDraw.Draw(mask).ellipse(self._box(cx, cy, rx, ry), fill=255)
        glow.putalpha(ImageChops.multiply(glow.getchannel("A"), mask))
        return glow
