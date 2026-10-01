"""Looking at a part before anyone prints it.

Rendering proves the code runs, not that the part works. A design can
render and still be solid where it should be hollow, have a piece floating
above the plate, or walls a nozzle cannot lay down; a sculpture can come out
with a tail thinner than a strand of spaghetti, loose crumbs of mesh, or a
footprint it will topple off. None of that shows in "it rendered".

Two looks, cheap first:

* **Geometry** (trimesh, no network): how many pieces and whether each sits
  on the plate, loose fragments, wall and feature thickness by casting rays
  inward from the surface, whether a container is actually hollow, whether a
  figure's centre of mass is over its base, and how much of it overhangs.
* **A review** (a vision model): three renders - a three-quarter view, the
  back, and a cut through the middle - with the request, the concept picture
  if there is one, and the geometry's findings. It answers whether the part
  does what was asked, and if not, what to change. The cut is the view that
  shows a cavity, a gap round a lid, a slot through a loop.

Designs are sent back to the design model with the findings (commands/
printer.py); sculptures and hand-written parts carry them as warnings.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

CHAT_URL = "https://api.openai.com/v1/chat/completions"

# Thinner than this and the slicer drops it or it snaps: two 0.4 mm lines for
# a designed wall, a little more for a sculpture's limb, which is loaded at
# one end and printed as a column.
THIN_DESIGN_MM = 0.8
THIN_SCULPT_MM = 1.2

_CONTAINER = re.compile(
    r"\b(hollow|pouch|box|container|storage|holder|case|compartments?|cup|bin|pot|planter|"
    r"enclosure|organi[sz]er|drawer|tray|vase)\b", re.I)


@dataclass
class Report:
    problems: list[str] = field(default_factory=list)   # would stop it working or printing
    notes: list[str] = field(default_factory=list)      # worth knowing, not a failure
    facts: dict[str, Any] = field(default_factory=dict)
    review: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.problems

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def feedback(self) -> str:
        """For the design model: what to fix, in one message."""
        lines = [f"- {p}" for p in self.problems]
        fix = (self.review or {}).get("fix")
        if fix:
            lines.append(f"Suggested fix: {fix}")
        return "\n".join(lines)

    def spoken(self) -> str:
        if not self.problems:
            return ""
        first = self.problems[0].rstrip(".")
        more = len(self.problems) - 1
        return f" One thing to check: {first}." + (f" And {more} more on the Workshop tab." if more else "")


def _where(point, bounds) -> str:
    """A point in words: near the top, on the left."""
    (x0, y0, z0), (x1, y1, z1) = bounds
    x, y, z = point
    fz = (z - z0) / max(z1 - z0, 1e-6)
    height = "near the top" if fz > 0.66 else "near the bottom" if fz < 0.33 else "about half way up"
    fx = (x - x0) / max(x1 - x0, 1e-6)
    side = " on the left" if fx < 0.3 else " on the right" if fx > 0.7 else ""
    fy = (y - y0) / max(y1 - y0, 1e-6)
    face = " at the front" if fy < 0.3 else " at the back" if fy > 0.7 else ""
    return height + side + face


def geometry(stl: Path, *, kind: str, request: str = "") -> Report:
    """What can be measured without looking. `kind` is "make" or "sculpt"."""
    import numpy as np
    import trimesh

    report = Report()
    mesh = trimesh.load(str(stl), force="mesh")
    if mesh.is_empty:
        report.problems.append("the part came out empty")
        return report
    bounds = mesh.bounds
    total = float(abs(mesh.volume)) if mesh.is_volume else float(mesh.convex_hull.volume)
    report.facts["watertight"] = bool(mesh.is_watertight)
    if not mesh.is_watertight:
        report.notes.append("the mesh has small gaps; the slicer will repair them")

    # -- pieces ------------------------------------------------------------------
    # A hollow's inner wall is a shell facing inwards (negative volume): part
    # of the solid around it, not a piece of its own.
    pieces = [p for p in mesh.split(only_watertight=False) if not (p.is_watertight and p.volume < 0)]
    volumes = [abs(float(p.volume)) if p.is_volume else float(p.convex_hull.volume) for p in pieces]
    biggest = max(volumes) if volumes else 0.0
    crumbs = [p for p, v in zip(pieces, volumes) if v < max(2.0, 0.005 * biggest)]
    real = [p for p, v in zip(pieces, volumes) if v >= max(2.0, 0.005 * biggest)]
    report.facts["pieces"] = len(real)
    if crumbs:
        report.problems.append(
            f"{len(crumbs)} loose fragment{'s' if len(crumbs) > 1 else ''} not attached to the part, "
            + _where(crumbs[0].centroid, bounds))
    floor = bounds[0][2]
    floating = [p for p in real if p.bounds[0][2] - floor > 0.05]
    if floating:
        report.problems.append(
            f"a piece is floating {floating[0].bounds[0][2] - floor:.1f} mm above the plate, "
            + _where(floating[0].centroid, bounds))

    # -- resting on the plate ------------------------------------------------------
    main = max(real, key=lambda p: abs(float(p.volume)) if p.is_volume else 0.0) if real else mesh
    tri = main.triangles
    down = np.all(tri[:, :, 2] < floor + 0.05, axis=1)
    contact = float(main.area_faces[down].sum())
    report.facts["contact_mm2"] = round(contact, 1)
    footprint = float((main.extents[0]) * (main.extents[1]))
    if contact < 1.0:
        report.problems.append("it barely touches the plate - it needs a flat base")
    elif kind == "sculpt":
        try:
            from scipy.spatial import Delaunay

            pts = tri[down][:, :, :2].reshape(-1, 2)
            com = main.center_mass if main.is_volume else main.centroid
            inside = len(pts) >= 3 and Delaunay(pts).find_simplex(com[:2]) >= 0
            if not inside:
                report.problems.append("its centre of mass is outside its base, so it will tip over - "
                                       "it needs a wider base or a stand")
        except Exception as exc:  # a degenerate base is not worth failing over
            log.debug("could not judge balance: %s", exc)
        if contact < 0.05 * footprint:
            report.notes.append("it stands on a small base; use a brim so it stays put while printing")

    # -- thickness ---------------------------------------------------------------------
    limit = THIN_SCULPT_MM if kind == "sculpt" else THIN_DESIGN_MM
    thin_share, thin_at = _thin(main, limit)
    report.facts["thin_share"] = round(thin_share, 4)
    if thin_at is not None:
        if kind == "sculpt" and thin_share > 0.01:
            report.problems.append(f"some parts are thinner than {limit:g} mm and will snap, "
                                   + _where(thin_at, bounds))
        elif kind != "sculpt" and thin_share > 0.03:
            report.problems.append(f"some walls are thinner than {limit:g} mm, "
                                   + _where(thin_at, bounds))
        elif thin_share > 0.002:
            report.notes.append(f"a few details are under {limit:g} mm and may print soft, "
                                + _where(thin_at, bounds))

    # -- a container should contain ------------------------------------------------------
    if kind != "sculpt" and _CONTAINER.search(request or ""):
        hull = float(main.convex_hull.volume) or 1.0
        fill = (abs(float(main.volume)) if main.is_volume else hull) / hull
        report.facts["fill"] = round(fill, 3)
        if fill > 0.8:
            report.problems.append("it is solid - there is no cavity inside it")

    # -- overhangs ----------------------------------------------------------------------------
    normals = mesh.face_normals
    lower = mesh.triangles[:, :, 2].min(axis=1) > floor + 0.2
    steep = (normals[:, 2] < -0.72) & lower
    share = float(mesh.area_faces[steep].sum() / max(mesh.area, 1e-6))
    report.facts["overhang_share"] = round(share, 3)
    if share > 0.08:
        report.notes.append(f"about {share:.0%} of its surface overhangs and will need supports")
    report.facts["volume_mm3"] = round(total, 1)
    return report


def _thin(mesh, limit: float, samples: int = 1500):
    """The share of the surface where the solid behind it is thinner than
    `limit`, and where the thinnest of it is. Rays go inward from sample
    points; the first hit on the far side is the local thickness."""
    import numpy as np
    import trimesh

    if len(mesh.faces) == 0:
        return 0.0, None
    try:
        points, faces = trimesh.sample.sample_surface(mesh, samples, seed=7)
    except TypeError:  # older trimesh has no seed
        points, faces = trimesh.sample.sample_surface(mesh, samples)
    normals = mesh.face_normals[faces]
    origins = points - normals * 0.01
    try:
        hits, rays, _ = mesh.ray.intersects_location(origins, -normals, multiple_hits=False)
    except Exception as exc:  # no ray engine: say nothing rather than guess
        log.info("thickness not measured: %s", exc)
        return 0.0, None
    if len(rays) == 0:
        return 0.0, None
    depth = np.linalg.norm(hits - origins[rays], axis=1)
    thin = depth < limit
    if not thin.any():
        return 0.0, None
    where = hits[thin][np.argmin(depth[thin])]
    return float(thin.sum() / len(rays)), where


# -- looking -----------------------------------------------------------------------

VIEWS = {
    # name: OpenSCAD --camera rotation (rx, ry, rz) in its gimbal form
    "three-quarter": (60, 0, 330),
    "back": (70, 0, 150),
}


def views(stl: Path, out_dir: Path, openscad: Path, *, size: int = 640, timeout: int = 90,
          cut: bool = True) -> list[Path]:
    """Renders to look at: a three-quarter view, the back, and (with `cut`) a
    cut through the middle seen from the front. Missing ones are left out,
    not fatal."""
    from . import process

    out_dir.mkdir(parents=True, exist_ok=True)
    src = str(stl.resolve()).replace("\\", "/")
    made: list[Path] = []
    jobs = [(name, f'import("{src}");', rot) for name, rot in VIEWS.items()]
    # The cut: the back half removed, looked at from the front, so a cavity,
    # the gap round a lid or the slot of a loop shows as a shape.
    try:
        import trimesh

        if cut:
            (x0, y0, z0), (x1, y1, z1) = trimesh.load(str(stl), force="mesh").bounds
            cy = (y0 + y1) / 2
            jobs.append(("cut", f'difference() {{ import("{src}"); translate([-1000, {cy:.2f}, -1000]) cube(2000); }}',
                         (90, 0, 0)))
    except Exception as exc:
        log.debug("no cut view: %s", exc)
    for name, body, (rx, ry, rz) in jobs:
        scad = out_dir / f"{name}.scad"
        png = out_dir / f"{name}.png"
        scad.write_text(body + "\n", encoding="utf-8")
        try:
            done = process.run(
                [str(openscad), "-o", str(png), f"--imgsize={size},{size}", "--viewall", "--autocenter",
                 f"--camera=0,0,0,{rx},{ry},{rz},0", "--colorscheme=Tomorrow", str(scad)],
                timeout=timeout,
            )
            if png.is_file() and png.stat().st_size > 1000:
                made.append(png)
            else:
                log.info("view %s did not render: %s", name, (done.stderr or "")[-200:])
        except Exception as exc:
            log.info("view %s did not render: %s", name, exc)
    return made


REVIEW = (
    "You check 3D-printable parts before they are printed. You are shown renders of "
    "one part - a three-quarter view, the back, and a CUT through the middle seen "
    "from the front, where the orange faces are the cut surface - and what was asked "
    "for. Parts lie as they will print: a lid may be upside down beside its box. "
    "Judge only whether it will WORK and PRINT as asked: is a container hollow with an "
    "opening, does a lid or flap have a visible gap all round and a hinge, is a loop "
    "open for the belt, are there parts missing that the request or concept picture "
    "has, anything floating or fragile. Ignore colour, small cosmetic differences and "
    "style. The measurements given were taken from the mesh and are reliable. When "
    "the part is built on a parts-library template, the mechanisms listed for it are "
    "TESTED and present even where a render is too small to show them - never report "
    "those as missing; judge instead whether the right template, sizes and requested "
    "features and decoration were used, reading the source you are given.\n"
    'Reply with JSON only: {"ok": true|false, "problems": ["..."], "fix": "..."} - '
    "problems in plain words, at most four, only real ones; fix = one instruction "
    "for the designer (empty when ok)."
)


# A sculpture's look is the point of it, so its review judges the look too.
REVIEW_SCULPT = (
    "You check 3D-printable sculptures before they are printed. You are shown renders of "
    "one - a three-quarter view, the back, and a CUT through the middle seen from the "
    "front, where the orange faces are the cut surface - what was asked for, and the "
    "concept picture it was made from. Judge two things. Will it PRINT: anything "
    "floating, fragile or loose. And does it LOOK as it should: every part in the "
    "concept picture present (limbs, wheels, wings, ears), surfaces clean rather than "
    "crumpled, melted or lumpy, gaps open rather than filled with webbing or slabs, no "
    "junk stuck on. Ignore colour and lighting, and the small softening any sculpture "
    "has. The measurements given were taken from the mesh and are reliable.\n"
    'Reply with JSON only: {"ok": true|false, "problems": ["..."], "fix": "..."} - '
    "problems in plain words, at most four, only real ones; fix = one instruction "
    "(empty when ok)."
)

RANK = (
    "These are candidate 3D models of the same object, each shown from the front "
    "three-quarter (top) and from behind (bottom), made from the reference picture. "
    "Rank them best first by how faithfully and cleanly they reproduce the object in "
    "the reference: every part present, clean surfaces rather than crumpled, melted "
    "or lumpy ones, gaps open rather than filled with webbing or slabs, no extra junk. "
    "Ignore colour and lighting.\n"
    'Reply with JSON only: {"ranking": ["A", "B", ...]} - every letter, once.'
)


def rank(reference: Path, stls: list[Path], *, openscad: Path, work_dir: Path, model: str,
         trials: int = 2, timeout: float = 180.0) -> list[int] | None:
    """The candidates best first, as indexes into `stls`, by a vision model
    comparing renders of each with the reference picture. Asked `trials`
    times with the labels shuffled, and the answers added up (a Borda count):
    one look ranks the tiers reliably but not the order within them. None
    when there is no telling - no key, no renders, no answer."""
    import random
    from concurrent.futures import ThreadPoolExecutor

    from PIL import Image, ImageDraw

    key = os.environ.get("OPENAI_API_KEY", "")
    if not key or len(stls) < 2:
        return None
    tiles: list[Image.Image | None] = []
    for i, stl in enumerate(stls):
        shots = views(stl, work_dir / f"candidate-{i}", openscad, size=480, cut=False)
        if len(shots) < 2:
            tiles.append(None)
            continue
        tile = Image.new("RGB", (480, 960), "white")
        for row, shot in enumerate(shots[:2]):
            with Image.open(shot) as picture:
                tile.paste(picture.convert("RGB").resize((480, 480)), (0, 480 * row))
        tiles.append(tile)
    shown = [i for i, tile in enumerate(tiles) if tile is not None]
    if len(shown) < 2:
        return None
    letters = "ABCDEFGH"[:len(shown)]

    def ask(trial: int) -> list[int] | None:
        order = shown[:]
        random.Random(trial).shuffle(order)
        grid = Image.new("RGB", (240 * len(order), 480), "white")
        draw = ImageDraw.Draw(grid)
        for slot, index in enumerate(order):
            grid.paste(tiles[index].resize((240, 480)), (240 * slot, 0))
            draw.rectangle((240 * slot, 0, 240 * slot + 34, 30), fill="black")
            draw.text((240 * slot + 11, 5), letters[slot], fill="white", font_size=20)
        buffer = io.BytesIO()
        grid.save(buffer, "JPEG", quality=88)
        body: dict = {
            "model": model,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": RANK}, {"role": "user", "content": [
                {"type": "text", "text": "The reference:"},
                {"type": "image_url", "image_url": {"url": _image(reference), "detail": "low"}},
                {"type": "text", "text": "The candidates:"},
                {"type": "image_url", "image_url": {
                    "url": "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode(),
                    "detail": "high"}},
            ]}],
        }
        if model.startswith(("gpt-5", "o")):
            body["reasoning_effort"] = "low"
        request = urllib.request.Request(
            CHAT_URL, data=json.dumps(body).encode(), method="POST",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            ranking = json.loads(payload["choices"][0]["message"]["content"] or "{}").get("ranking") or []
        except Exception as exc:
            log.info("ranking the candidates did not happen: %s", exc)
            return None
        picked = [order[letters.index(r)] for r in (str(x).strip().upper()[:1] for x in ranking)
                  if r and r in letters]
        return list(dict.fromkeys(picked)) or None

    with ThreadPoolExecutor(max_workers=trials) as pool:
        answers = [a for a in pool.map(ask, range(trials)) if a]
    if not answers:
        return None
    points = {i: 0 for i in range(len(stls))}
    for answer in answers:
        for place, index in enumerate(answer):
            points[index] += len(shown) - place
    return sorted(points, key=lambda i: (-points[i], i))


def _image(path: Path, side: int = 768) -> str:
    from PIL import Image

    image = Image.open(path).convert("RGB")
    image.thumbnail((side, side))
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=85)
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode()


def review(request: str, report: Report, renders: list[Path], *, model: str,
           picture: Path | None = None, kind: str = "make", source: str = "",
           timeout: float = 180.0) -> dict[str, Any]:
    """The vision model's verdict, or {} when it could not be had - a review
    that did not happen is not a failed part."""
    key = os.environ.get("OPENAI_API_KEY", "")
    if not key or not renders:
        return {}
    facts = {k: v for k, v in report.facts.items()}
    text = (f"What was asked for ({'a designed working part' if kind != 'sculpt' else 'a sculpture'}): "
            f"{request.strip() or 'not recorded'}\n"
            f"Measured: {json.dumps(facts)}\n"
            f"Found by measuring: {json.dumps(report.problems + report.notes)}")
    if source:
        from .design import GUARANTEES, template_used

        used = template_used(source)
        if used:
            text += f"\nBuilt on the parts-library template {used}(), which already has: {GUARANTEES[used]}."
        text += f"\nThe OpenSCAD source:\n{source[:6000]}"
    content: list[dict] = [{"type": "text", "text": text}]
    for path in renders:
        try:
            content.append({"type": "text", "text": f"Render: {path.stem}"})
            content.append({"type": "image_url", "image_url": {"url": _image(path), "detail": "high"}})
        except Exception:
            continue
    if picture is not None:
        try:
            content.append({"type": "text", "text": "The concept picture it should match:"})
            content.append({"type": "image_url", "image_url": {"url": _image(picture), "detail": "low"}})
        except Exception:
            pass
    body: dict = {
        "model": model,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "system", "content": REVIEW_SCULPT if kind == "sculpt" else REVIEW},
                     {"role": "user", "content": content}],
    }
    if model.startswith(("gpt-5", "o")):
        body["reasoning_effort"] = "low"
    request_ = urllib.request.Request(
        CHAT_URL, data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request_, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        verdict = json.loads(payload["choices"][0]["message"]["content"] or "{}")
    except Exception as exc:
        log.info("the part review did not happen: %s", exc)
        return {}
    problems = [str(p).strip() for p in (verdict.get("problems") or []) if str(p).strip()][:4]
    return {"ok": bool(verdict.get("ok")) and not problems, "problems": problems,
            "fix": str(verdict.get("fix") or "").strip()}


def check(stl: Path, *, kind: str, request: str = "", openscad: Path | None = None,
          work_dir: Path | None = None, model: str = "", picture: Path | None = None,
          source: str = "") -> Report:
    """Geometry, then - with a model and OpenSCAD for the renders - a review.
    The review's problems join the report's."""
    try:
        report = geometry(stl, kind=kind, request=request)
    except Exception as exc:
        log.warning("could not measure %s: %s", stl.name, exc)
        report = Report(notes=["it could not be measured"])
    if model and openscad is not None and work_dir is not None:
        renders = views(stl, work_dir, openscad)
        verdict = review(request, report, renders, model=model, picture=picture, kind=kind, source=source)
        if verdict:
            report.review = verdict
            for problem in verdict.get("problems", []):
                if problem not in report.problems:
                    report.problems.append(problem)
    return report
