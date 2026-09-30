"""Slicer settings for one part, worked out from its own shape.

Elegoo Slicer's Centauri Carbon 2 profile is a good start for anything; what
it cannot know is the part in front of it. This reads the STL and measures
the things that decide the settings - how much of it overhangs, how much
of it touches the bed and how tall it stands on that, how much of the
surface slopes gently enough for layer lines to show as steps, how big it
is - and turns them into a short list of changes from the profile, each
with the reason, so a person can judge it rather than trust it.

Only what differs from part to part is suggested. Speeds, accelerations,
retraction and the like are the printer's business, and the profile has
them right.

Pure Python on purpose: a designed part needs nothing beyond OpenSCAD, and
even a sculpture's 150,000 triangles take well under a second.
"""

from __future__ import annotations

import math
import re
import struct
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

VERSION = 2  # bump when the rules change, so stored settings are worked out again

# Per filament: temperatures, cooling, the enclosure, and density for the
# weight. Temperatures are the middle of each maker's usual range.
MATERIALS: dict[str, dict[str, Any]] = {
    "PLA": {"nozzle": 210, "bed": 60, "fan": "100% after the first 3 layers",
            "enclosure": "lid off or door open - PLA softens in a warm chamber and can clog the nozzle",
            "density": 1.24},
    "PETG": {"nozzle": 245, "bed": 75, "fan": "30-50%",
             "enclosure": "closed", "density": 1.27},
    "ABS": {"nozzle": 260, "bed": 100, "fan": "off, 20% for bridges",
            "enclosure": "closed, warmed up for ten minutes before printing", "density": 1.04},
    "ASA": {"nozzle": 260, "bed": 100, "fan": "off, 20% for bridges",
            "enclosure": "closed, warmed up for ten minutes before printing", "density": 1.07},
    "TPU": {"nozzle": 225, "bed": 45, "fan": "50-100%",
            "enclosure": "lid off", "density": 1.21},
}

DOWN_45 = -math.sqrt(0.5)  # a face pointing down more steeply than 45 degrees
BED_BAND = 0.3             # mm: a face this close to the bottom is on the bed


@dataclass(slots=True)
class Shape:
    size: tuple[float, float, float]
    area: float            # mm², the whole surface
    volume: float          # mm³
    contact: float         # mm² of flat bottom on the bed
    contact_span: float    # mm: the narrow side of that footprint
    overhang: float        # mm² facing down more steeply than 45°, off the bed
    steep: float           # mm² of that within about 20° of flat
    sloped: float          # mm² facing up at a shallow angle: where layers show as steps
    flat_top: float        # mm² facing straight up
    faces: int
    # The biggest flat outer face that is not already on the bed, for a
    # designed part: the direction it faces, and its area.
    best_face: tuple[str, float] | None = None
    pieces: int = 1
    # How far the highest-drawn piece was above the lowest: more than a
    # hair, and the part needs splitting in the slicer. Measured as split.
    lifted: float = 0.0


def triangles(path: Path) -> list[tuple[float, ...]]:
    """Nine floats per triangle: binary STL, or ASCII as a fallback."""
    data = path.read_bytes()
    count = struct.unpack_from("<I", data, 80)[0] if len(data) >= 84 else -1
    if count >= 0 and len(data) == 84 + 50 * count:
        return [tri[3:12] for tri in struct.iter_unpack("<12fH", data[84:])]
    points = [tuple(float(v) for v in m.groups())
              for m in re.finditer(rb"vertex\s+(\S+)\s+(\S+)\s+(\S+)", data)]
    return [points[i] + points[i + 1] + points[i + 2] for i in range(0, len(points) - 2, 3)]


def _bodies(tris: list[tuple[float, ...]]) -> list[int]:
    """Which separate piece each triangle belongs to: pieces share no
    vertex. Union-find over the vertices."""
    index: dict[tuple[float, float, float], int] = {}
    parent: list[int] = []

    def vertex(x: float, y: float, z: float) -> int:
        key = (round(x, 3), round(y, 3), round(z, 3))
        found = index.get(key)
        if found is None:
            found = index[key] = len(parent)
            parent.append(found)
        return found

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    firsts = []
    for t in tris:
        a, b, c = vertex(*t[0:3]), vertex(*t[3:6]), vertex(*t[6:9])
        for other in (b, c):
            ra, ro = root(a), root(other)
            if ra != ro:
                parent[ro] = ra
        firsts.append(a)
    return [root(a) for a in firsts]


def _drop_pieces(tris: list[tuple[float, ...]]) -> tuple[list[tuple[float, ...]], int, float]:
    """Each separate piece set down on the bed, as the slicer does once the
    part is split into objects: (triangles, pieces, the most any was lifted).
    A designed part drawn with its lid beside it, but lower, otherwise sits
    on the lid's rim with the box in mid-air."""
    bodies = _bodies(tris)
    low: dict[int, float] = {}
    for body, t in zip(bodies, tris):
        low[body] = min(low.get(body, math.inf), t[2], t[5], t[8])
    floor = min(low.values())
    lift = {b: z - floor for b, z in low.items() if z - floor > 0.5}
    if not lift:
        return tris, len(low), 0.0
    moved = [t if b not in lift else (t[0], t[1], t[2] - lift[b], t[3], t[4], t[5] - lift[b],
                                      t[6], t[7], t[8] - lift[b])
             for b, t in zip(bodies, tris)]
    return moved, len(low), max(lift.values())


def measure(path: Path, *, faces: bool = True) -> Shape:
    """The numbers the settings are decided from. `faces` looks for a
    better face to lay it on, and for pieces drawn at different heights,
    which only make sense for a designed part."""
    tris = triangles(path)
    if not tris:
        raise ValueError("the part has no surfaces")
    pieces, lifted = 1, 0.0
    if faces:
        tris, pieces, lifted = _drop_pieces(tris)
    zs = [t[k] for t in tris for k in (2, 5, 8)]
    xs = [t[k] for t in tris for k in (0, 3, 6)]
    ys = [t[k] for t in tris for k in (1, 4, 7)]
    zmin = min(zs)
    area = volume = contact = overhang = steep = sloped = flat_top = 0.0
    foot = [math.inf, math.inf, -math.inf, -math.inf]
    planes: dict[tuple[int, int, int, int], list[float]] = {}
    for x0, y0, z0, x1, y1, z1, x2, y2, z2 in tris:
        ax, ay, az = x1 - x0, y1 - y0, z1 - z0
        bx, by, bz = x2 - x0, y2 - y0, z2 - z0
        cx, cy, cz = ay * bz - az * by, az * bx - ax * bz, ax * by - ay * bx
        norm = math.sqrt(cx * cx + cy * cy + cz * cz)
        if norm == 0:
            continue
        a = norm / 2
        nx, ny, nz = cx / norm, cy / norm, cz / norm
        area += a
        volume += (x0 * (y1 * z2 - z1 * y2) - y0 * (x1 * z2 - z1 * x2) + z0 * (x1 * y2 - y1 * x2)) / 6
        low, high = min(z0, z1, z2), max(z0, z1, z2)
        if nz < -0.9 and high < zmin + BED_BAND:
            contact += a
            foot = [min(foot[0], x0, x1, x2), min(foot[1], y0, y1, y2),
                    max(foot[2], x0, x1, x2), max(foot[3], y0, y1, y2)]
        elif nz < DOWN_45 and low > zmin + BED_BAND:
            overhang += a
            if nz < -0.94:
                steep += a
        elif 0.26 < nz < 0.97:
            sloped += a
        if nz > 0.985:
            flat_top += a
        if faces:
            # Coplanar triangles, bucketed by direction and offset.
            d = nx * x0 + ny * y0 + nz * z0
            key = (round(nx * 20), round(ny * 20), round(nz * 20), round(d * 2))
            entry = planes.setdefault(key, [0.0, nx, ny, nz, d])
            entry[0] += a
    span = min(foot[2] - foot[0], foot[3] - foot[1]) if contact else 0.0
    best = _best_face(planes, xs, ys, zs) if faces else None
    return Shape(size=(max(xs) - min(xs), max(ys) - min(ys), max(zs) - zmin), area=area,
                 volume=abs(volume), contact=contact, contact_span=span, overhang=overhang,
                 steep=steep, sloped=sloped, flat_top=flat_top, faces=len(tris), best_face=best,
                 pieces=pieces, lifted=lifted)


def _best_face(planes: dict, xs: list[float], ys: list[float], zs: list[float]) -> tuple[str, float] | None:
    """The biggest flat face on the outside of the part - one it could lie
    on - that is not already facing down."""
    ranked = sorted(planes.values(), key=lambda p: p[0], reverse=True)
    for a, nx, ny, nz, d in ranked[:12]:
        if a < 50:
            break
        if nz < -0.98:
            continue  # facing down already: on the bed, or held off it by something lower
        # On the outside only if no point of the part lies beyond it.
        reach = max(nx * x + ny * y + nz * z for x, y, z in zip(xs, ys, zs))
        if reach - d < 0.2:
            return _side(nx, ny, nz), a
    return None


def _side(nx: float, ny: float, nz: float) -> str:
    if nz > 0.9:
        return "top"
    if abs(nx) > 0.9:
        return "right side" if nx > 0 else "left side"
    if abs(ny) > 0.9:
        return "back" if ny > 0 else "front"
    return "sloping face"


def recommend(shape: Shape, *, kind: str, material: str = "PLA",
              watertight: bool | None = None) -> dict[str, Any]:
    """{"summary", "notes", "settings": [{group, name, value, why}], ...}."""
    material = material.upper() if material.upper() in MATERIALS else "PLA"
    mat = MATERIALS[material]
    sculpted = kind != "make"
    x, y, z = shape.size
    small = max(shape.size) < 25
    slope_share = shape.sloped / shape.area if shape.area else 0.0
    over_share = shape.overhang / shape.area if shape.area else 0.0
    settings: list[dict[str, str]] = []
    notes: list[str] = []

    def add(group: str, name: str, value: str, why: str) -> None:
        settings.append({"group": group, "name": name, "value": value, "why": why})

    # -- how it lies -------------------------------------------------------
    if shape.lifted:
        notes.append(f"It is {shape.pieces} separate pieces, drawn at different heights - one is "
                     f"{shape.lifted:.1f} mm above the bed. Split it into objects so each piece drops "
                     "onto the plate; the settings here assume you have.")
        add("Orientation", "Split", "To objects (right-click it, Split, To objects)",
            f"a piece drawn {shape.lifted:.1f} mm up would otherwise print in mid-air on supports")
    turned = False
    if not sculpted and shape.best_face and shape.pieces == 1:
        side, face = shape.best_face
        if face > max(1.5 * shape.contact, shape.contact + 50) and (over_share > 0.02 or face > 2 * shape.contact):
            turned = True
            add("Orientation", "Lay on face", f"its {side} (select it, press F, click that face)",
                f"its {side} is a flat {face:.0f} mm² against {shape.contact:.0f} mm² on the bed now - "
                "more grip, and usually fewer overhangs")

    # -- quality -----------------------------------------------------------
    if sculpted:
        if z < 30 or small:
            layer, why = 0.08, f"only {z:.0f} mm tall, so the finest layers keep its details"
        elif slope_share > 0.2:
            layer, why = 0.12, f"{slope_share:.0%} of its surface slopes gently, where thick layers show as steps"
        else:
            layer, why = 0.16, "mostly steep sides, where layer lines hardly show"
    elif small:
        layer, why = 0.12, "small, so finer layers keep its edges crisp"
    else:
        layer, why = 0.20, "a part that has to fit: dimensions matter more than surface"
    add("Quality", "Layer height", f"{layer:.2f} mm", why)
    if slope_share > 0.25 and layer >= 0.12 and not turned:
        add("Quality", "Variable layer height", "Adaptive, then Smooth",
            f"{slope_share:.0%} of it slopes gently; adaptive layers go finer only there and stay quick elsewhere")
    if sculpted:
        add("Quality", "Seam position", "Back", "puts the seam behind the figure - its front faces the front of the plate")
        add("Quality", "Outer wall speed", "150 mm/s", "a slower outer wall gives a smoother skin; the rest can stay fast")
    else:
        add("Quality", "Seam position", "Aligned", "one tidy line down a corner, away from surfaces that mate")
    if not sculpted and shape.flat_top > 400 and not turned:
        add("Quality", "Ironing", "Topmost surface (optional)",
            f"{shape.flat_top / 100:.0f} cm² of flat top that ironing makes smooth, at the cost of some time")

    # -- strength ------------------------------------------------------------
    walls = 3 if sculpted else 4
    add("Strength", "Wall loops", str(walls),
        "a figurine is mostly shell; three walls hold the detail" if sculpted
        else "a part's strength comes from its walls more than its infill")
    top, bottom = max(4, math.ceil(0.8 / layer)), max(3, math.ceil(0.6 / layer))
    add("Strength", "Top / bottom shell layers", f"{top} / {bottom}",
        f"about 0.8 mm of solid top at {layer:.2f} mm layers, so infill does not show through")
    if shape.volume < 3000:
        infill, why = (15 if sculpted else 40), f"only {shape.volume / 1000:.1f} cm³ - it is nearly all walls anyway"
    elif sculpted:
        infill, why = (15 if z > 120 else 10), ("tall enough to want a stiffer core" if z > 120
                                                else "it only has to hold its shape")
    else:
        infill, why = 20, "enough for everyday loads; raise it to 40% for a part that is clamped or bears weight"
    add("Strength", "Sparse infill", f"{infill}% gyroid", why + "; gyroid is strong in every direction")

    # -- supports ------------------------------------------------------------
    if shape.overhang < 20 or (over_share < 0.01 and shape.steep < 5):
        add("Supports", "Enable support", "Off",
            "nothing overhangs past 45° to speak of" + (" once it is laid on that face" if turned else ""))
    elif sculpted or over_share > 0.08:
        add("Supports", "Enable support", "On - Tree (auto), threshold 45°",
            f"{shape.overhang / 100:.1f} cm² ({over_share:.0%} of it) overhangs past 45°; tree supports "
            "touch it at few points and snap off cleanly")
        add("Supports", "Top Z distance", "0.2 mm", "a gap the supports break away from without scarring the surface")
    else:
        add("Supports", "Enable support", "On - Normal (auto), threshold 45°",
            f"{shape.overhang / 100:.1f} cm² overhangs past 45°" + (" - check again after laying it down" if turned else ""))

    # -- the bed -------------------------------------------------------------
    stance = z / shape.contact_span if shape.contact_span else math.inf
    if shape.contact < 5 and not turned:
        notes.append(f"It barely touches the bed ({shape.contact:.0f} mm² of flat bottom). Cut 1 mm off the "
                     "bottom in the slicer (Cut tool, C) to give it a flat base, or print it on a raft.")
        add("Bed", "Raft layers", "2", "almost nothing of it is flat on the bed")
    elif (stance > 2.5 or shape.contact < 8 * z) and not turned:
        # 8 mm² of base for each millimetre of height, or a stance wider
        # than 1 in 2.5, is where a tall thing stops standing by itself.
        why = (f"it stands {z:.0f} mm tall on a base {shape.contact_span:.0f} mm across" if stance > 2.5
               else f"only {shape.contact / 100:.1f} cm² of it touches the bed, for {z:.0f} mm of height")
        add("Bed", "Brim", "Outer brim only, 5 mm", why + "; a brim keeps it from being knocked over or lifting")
    elif material in ("ABS", "ASA") and max(x, y) > 100:
        add("Bed", "Brim", "Outer brim only, 5 mm", f"{material} shrinks as it cools and a part this long lifts at the corners")
    else:
        add("Bed", "Brim", "None (auto)", f"{shape.contact / 100:.1f} cm² of flat base holds it down on its own")
    if small or max(x, y) < 30:
        add("Bed", "Slow down for layer cooling", "On, layer time 8 s",
            "each layer is small, and needs a moment to cool before the next goes on")

    # -- the filament --------------------------------------------------------
    add("Filament", "Nozzle / bed", f"{mat['nozzle']} / {mat['bed']} °C", f"the middle of the usual range for {material}")
    add("Filament", "Part cooling fan", mat["fan"], "")
    add("Filament", "Enclosure", mat["enclosure"], "")
    if watertight is False:
        notes.append("The mesh has gaps. Say yes when the slicer offers to repair it, or right-click it and Fix model.")

    # -- how much filament, roughly --------------------------------------------
    shell = min(shape.volume, shape.area * walls * 0.42)
    grams = (shell + (shape.volume - shell) * infill / 100) / 1000 * mat["density"]
    supported = settings_value(settings, "Enable support") != "Off"
    grams *= 1.1 if supported else 1.0

    bed = ("a raft" if settings_value(settings, "Raft layers")
           else "5 mm brim" if settings_value(settings, "Brim").startswith("Outer") else "no brim")
    bits = [f"{layer:.2f} mm layers", f"{walls} walls", f"{infill}% infill",
            "supports on" if supported else "no supports", bed]
    if turned and shape.best_face:
        bits.insert(0, f"laid on its {shape.best_face[0]}")
    summary = ", ".join(bits) + f" - about {max(1, round(grams))} g of {material}."
    return {
        "v": VERSION, "material": material, "summary": summary, "notes": notes,
        "settings": settings, "grams": round(grams, 1),
        "shape": {k: (round(v, 1) if isinstance(v, float) else v) for k, v in asdict(shape).items()
                  if k not in ("size", "best_face")},
    }


def settings_value(settings: list[dict[str, str]], name: str) -> str:
    return next((s["value"] for s in settings if s["name"] == name), "")


def for_part(stl: Path, *, kind: str, material: str = "PLA", watertight: bool | None = None) -> dict[str, Any]:
    return recommend(measure(stl, faces=kind == "make"), kind=kind, material=material, watertight=watertight)
