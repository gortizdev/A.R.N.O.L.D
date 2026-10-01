"""Working features on a sculpture: a stand, a magnet, a key ring, a hollow.

A dragon cannot be designed in OpenSCAD and a keychain loop cannot be
sculpted - Hunyuan3D draws the look of a ring, fused shut. But the two meet
here: the organic shape comes from the sculpt, and a few plain, measured
features are merged into the finished mesh (manifold3d, through trimesh).

* **plinth** - a slab under the figure, the shape of its shadow grown by a
  margin. The answer to "it will tip over".
* **magnet** - a round pocket in the underside for a magnet pressed in. In
  the plinth when there is one, which is then made thick enough to hold it.
* **keyring** - a ring standing on top, sunk into the mesh so it cannot come
  off, for a key ring or a lanyard.
* **hollow** - the inside taken out to an even wall, by eroding a voxel copy
  of the part and cutting that away: a planter, a pencil pot, a lamp shade,
  or just a lighter print. open="top" lets the hollow out through the top.

Each is checked where it can be: a magnet pocket needs solid above it, a
ring needs something to hold on to, and a feature that leaves the part in
two pieces is refused rather than printed.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class AddonError(Exception):
    """A feature could not be added. The message is spoken."""


@dataclass
class Addons:
    plinth: dict[str, float] | None = None
    magnet: dict[str, float] | None = None
    keyring: dict[str, float] | None = None
    hollow: dict[str, Any] | None = None

    def any(self) -> bool:
        return any(v is not None for v in (self.plinth, self.magnet, self.keyring, self.hollow))

    def names(self) -> list[str]:
        return [n for n in ("plinth", "magnet", "keyring", "hollow") if getattr(self, n) is not None]

    def to_dict(self) -> dict[str, Any]:
        return {n: getattr(self, n) for n in self.names()}


_DEFAULTS = {
    "plinth": {"margin": 4.0, "thickness": 3.0},
    "magnet": {"d": 10.0, "h": 3.0},
    "keyring": {"hole": 5.0, "band": 2.5, "t": 3.0},
    "hollow": {"wall": 2.0, "open": ""},
}


def parse(raw: Any) -> Addons:
    """From a dict ({"magnet": {"d": 8}, "keyring": true}) or words
    ("plinth, magnet 8x3, keyring, hollow open top")."""
    found = Addons()
    if raw in (None, "", {}, []):
        return found
    items: dict[str, Any] = {}
    if isinstance(raw, dict):
        items = dict(raw)
    else:
        for part in re.split(r"[,;]+", str(raw)):
            words = part.strip().lower()
            if not words:
                continue
            name = words.split()[0]
            if name.startswith(("key", "lanyard")):
                items["keyring"] = True
            elif name.startswith("magnet"):
                size = re.search(r"(\d+(?:\.\d+)?)\s*[x×]\s*(\d+(?:\.\d+)?)", words)
                items["magnet"] = {"d": float(size.group(1)), "h": float(size.group(2))} if size else True
            elif name.startswith(("plinth", "base", "stand")):
                items["plinth"] = True
            elif name.startswith("hollow"):
                items["hollow"] = {"open": "top"} if "top" in words else True
            else:
                raise AddonError(f"I don't know how to add '{part.strip()}'. I can add a plinth, "
                                 "a magnet pocket, a key ring loop or hollow it out.")
    for name, value in items.items():
        if name not in _DEFAULTS:
            raise AddonError(f"I don't know how to add '{name}'. I can add a plinth, "
                             "a magnet pocket, a key ring loop or hollow it out.")
        if value in (False, None, 0, "false", "no"):
            continue
        settings = dict(_DEFAULTS[name])
        if isinstance(value, dict):
            for key, v in value.items():
                if key in settings:
                    settings[key] = v if key == "open" else float(v)
        setattr(found, name, settings)
    return found


# Words in a request that mean a feature, for when none were asked for by name.
_IMPLIED = [
    (re.compile(r"\b(key ?chain|key ?ring|keyring|lanyard|charm|pendant)\b", re.I), "keyring"),
    (re.compile(r"\b(planter|plant pot|flower ?pot|pencil (?:pot|cup|holder)|vase|lamp ?shade)\b", re.I),
     "hollow-open"),
    (re.compile(r"\b(on a (?:base|plinth|stand)|with a (?:base|plinth|stand))\b", re.I), "plinth"),
    (re.compile(r"\bmagnet(?:ic)?\b", re.I), "magnet"),
]


def from_change(change: str) -> Addons:
    """A change that is only about features: "add a key ring loop", "put it
    on a stand", "hollow it out". Empty when it asks for anything else too."""
    from .design import looks_functional

    text = change or ""
    found = implied(text)
    if re.search(r"\bhollow", text, re.I) and found.hollow is None:
        found.hollow = dict(_DEFAULTS["hollow"], open="top" if re.search(r"\b(top|open)\b", text, re.I) else "")
    if not found.any():
        return Addons()
    rest = re.sub(r"\bhollow\w*|\bmagnet\w*|\bkey ?(?:ring|chain)\w*|\bstand\b|\bplinth\b|\bbase\b", "", text, flags=re.I)
    return Addons() if looks_functional(rest) else found


def implied(prompt: str) -> Addons:
    found: dict[str, Any] = {}
    for pattern, name in _IMPLIED:
        if pattern.search(prompt or ""):
            if name == "hollow-open":
                found["hollow"] = {"open": "top"}
            else:
                found[name] = True
    return parse(found) if found else Addons()


# -- the features ---------------------------------------------------------------


def solids(mesh) -> int:
    """How many separate solids: a hollow's inner wall is a shell of its own
    but faces inwards (negative volume), so it is not counted."""
    return sum(1 for shell in mesh.split(only_watertight=False) if shell.volume > 1e-3)


def _union(a, b):
    import trimesh

    return trimesh.boolean.union([a, b], engine="manifold")


def _cut(a, b):
    import trimesh

    return trimesh.boolean.difference([a, b], engine="manifold")


def _footprint(mesh, band: float = 0.15):
    """The XY outline of the bottom `band` of the part, as convex hull points."""
    import numpy as np
    from scipy.spatial import ConvexHull

    z0, z1 = mesh.bounds[0][2], mesh.bounds[1][2]
    low = mesh.vertices[mesh.vertices[:, 2] <= z0 + max(1.0, band * (z1 - z0))][:, :2]
    if len(low) < 3:
        low = mesh.vertices[:, :2]
    hull = ConvexHull(low)
    return low[hull.vertices]


def _prism(points_2d, z0: float, z1: float, grow: float = 0.0):
    """A convex slab: the hull of `points_2d` grown by `grow`, from z0 to z1."""
    import numpy as np
    import trimesh

    pts = np.asarray(points_2d, dtype=float)
    if grow > 0:
        ring = np.array([[math.cos(a), math.sin(a)] for a in np.linspace(0, 2 * math.pi, 24, endpoint=False)])
        pts = (pts[:, None, :] + grow * ring[None, :, :]).reshape(-1, 2)
    top = np.column_stack([pts, np.full(len(pts), z1)])
    bottom = np.column_stack([pts, np.full(len(pts), z0)])
    return trimesh.convex.convex_hull(np.vstack([top, bottom]))


def plinth(mesh, margin: float = 4.0, thickness: float = 3.0):
    """The figure stood on a slab of its whole shadow plus `margin`: under
    everything that reaches out, so its balance is no longer a question."""
    outline = _footprint(mesh, band=1.0)
    lifted = mesh.copy()
    lifted.apply_translation([0, 0, thickness - 0.4])  # sunk 0.4 mm in, so they fuse
    return _union(lifted, _prism(outline, 0.0, thickness, grow=margin))


def magnet_pocket(mesh, d: float = 10.0, h: float = 3.0, clearance: float = 0.2):
    """A pocket in the underside, under the centre of the footprint."""
    import numpy as np
    import trimesh

    outline = _footprint(mesh, band=0.05)
    cx, cy = outline.mean(axis=0)
    radius = d / 2 + clearance
    depth = h + clearance
    # There must be solid all round and 1 mm above the pocket, or the magnet
    # sits in a hole with a wall it can push through.
    ring = np.array([[cx + (radius + 1.2) * math.cos(a), cy + (radius + 1.2) * math.sin(a), 0.3]
                     for a in np.linspace(0, 2 * math.pi, 16, endpoint=False)])
    above = np.array([[cx, cy, depth + 1.0]])
    if not mesh.contains(np.vstack([ring, above])).all():
        raise AddonError(f"There isn't enough solid base for a {d:g} by {h:g} millimetre magnet. "
                         "Add a plinth and it goes in that.")
    pocket = trimesh.creation.cylinder(radius=radius, height=depth + 0.02, sections=48)
    pocket.apply_translation([cx, cy, depth / 2 - 0.01])
    return _cut(mesh, pocket)


def keyring(mesh, hole: float = 5.0, band: float = 2.5, t: float = 3.0):
    """A ring standing on the highest point, its lower part sunk into the mesh."""
    import numpy as np
    import trimesh

    top = mesh.vertices[np.argmax(mesh.vertices[:, 2])]
    outer = hole / 2 + band
    ring = trimesh.creation.annulus(r_min=hole / 2, r_max=outer, height=t, sections=48)
    ring.apply_transform(trimesh.transformations.rotation_matrix(math.pi / 2, [1, 0, 0]))  # stand it up
    sink = min(band + 0.5, outer)  # enough of the ring inside to hold, the hole left clear
    ring.apply_translation([top[0], top[1], top[2] + outer - sink])
    joined = _union(mesh, ring)
    if solids(joined) > solids(mesh):
        raise AddonError("The top is too thin to hold a key ring loop.")
    return joined


def hollow(mesh, wall: float = 2.0, open: str = ""):
    """The inside removed to an even `wall`. open="top" lets it out upwards."""
    import numpy as np
    import trimesh
    from scipy import ndimage

    size = float(max(mesh.extents))
    pitch = max(0.35, min(1.0, size / 180))
    grid = mesh.voxelized(pitch).fill()
    solid = grid.matrix.copy()
    steps = max(1, int(math.ceil(wall / pitch)))
    inner = ndimage.binary_erosion(solid, iterations=steps)
    # Keep a floor: nothing within `wall` of the bottom is removed.
    inner[:, :, :steps + 1] = False
    if open == "top":
        # Every column open from its cavity up through the top.
        columns = inner.any(axis=2)
        highest = np.where(inner.any(axis=2), inner.shape[2] - 1 - np.argmax(inner[:, :, ::-1], axis=2), -1)
        for x, y in zip(*np.nonzero(columns)):
            inner[x, y, highest[x, y]:] = True
        pad = np.zeros((inner.shape[0], inner.shape[1], steps + 2), dtype=bool)
        pad[columns] = True
        inner = np.concatenate([inner, pad], axis=2)
    if inner.sum() < 8:
        raise AddonError(f"It's too slender to hollow with {wall:g} millimetre walls.")
    cavity = trimesh.voxel.VoxelGrid(inner, transform=grid.transform).marching_cubes
    cavity.apply_transform(grid.transform)
    # Voxels leave the inside terraced; a few passes of smoothing that keeps
    # the volume (Taubin) take the steps off without thinning the wall.
    trimesh.smoothing.filter_taubin(cavity, iterations=12)
    trimesh.repair.fix_normals(cavity)
    return _cut(mesh, cavity)


def apply(stl: Path, addons: Addons) -> dict[str, Any]:
    """Add the features to the part in place. Order matters: the hollow
    before anything is stood on it, the plinth before its magnet."""
    import trimesh

    mesh = trimesh.load(str(stl), force="mesh")
    if not mesh.is_watertight:
        trimesh.repair.fill_holes(mesh)
    before = solids(mesh)
    done: list[str] = []
    try:
        if addons.hollow is not None:
            mesh = hollow(mesh, wall=addons.hollow["wall"], open=str(addons.hollow.get("open") or ""))
            done.append("hollowed")
        if addons.plinth is not None:
            thickness = addons.plinth["thickness"]
            if addons.magnet is not None:
                thickness = max(thickness, addons.magnet["h"] + 1.6)
            mesh = plinth(mesh, margin=addons.plinth["margin"], thickness=thickness)
            done.append("a plinth")
        if addons.magnet is not None:
            mesh = magnet_pocket(mesh, d=addons.magnet["d"], h=addons.magnet["h"])
            done.append("a magnet pocket")
        if addons.keyring is not None:
            mesh = keyring(mesh, hole=addons.keyring["hole"], band=addons.keyring["band"], t=addons.keyring["t"])
            done.append("a key ring loop")
    except AddonError:
        raise
    except Exception as exc:
        log.warning("adding features failed: %s", exc)
        raise AddonError(f"I couldn't add that to the sculpture: {exc}") from None
    if solids(mesh) > max(before, 1):
        raise AddonError("Adding that left the part in pieces, so I've left it off.")
    low, high = mesh.bounds
    mesh.apply_translation([0, 0, -low[2]])
    mesh.export(str(stl), file_type="stl")
    return {"added": done, "size": tuple(float(v) for v in mesh.extents),
            "faces": len(mesh.faces), "watertight": bool(mesh.is_watertight)}
