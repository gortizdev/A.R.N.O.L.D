"""arnold_parts.scad: the mechanisms the design model builds on.

Real OpenSCAD throughout - the point is that the geometry works: a hinged lid
swings through its range without touching the box, a lidded box's lid fits,
and every template lands flat on the plate in pieces that print.
"""

import shutil
import subprocess

import pytest

trimesh = pytest.importorskip("trimesh")

from arnold.commands import printer as printer_cmd
from arnold.design import LIBRARY

OPENSCAD = printer_cmd.find_openscad()
pytestmark = pytest.mark.skipif(OPENSCAD is None, reason="OpenSCAD is not installed")


def run(tmp_path, body: str, name: str = "t"):
    """Render `body` with the library included. The STL path, or None when
    nothing is left; output text either way."""
    shutil.copyfile(LIBRARY, tmp_path / LIBRARY.name)
    scad, stl = tmp_path / f"{name}.scad", tmp_path / f"{name}.stl"
    scad.write_text(f"include <{LIBRARY.name}>\n{body}\n", encoding="utf-8")
    done = subprocess.run([str(OPENSCAD), "-o", str(stl), str(scad)], capture_output=True, text=True, timeout=300)
    out = done.stdout + done.stderr
    if "top level object is empty" in out.lower() or not stl.is_file():
        return None, out
    return stl, out


def overlap(stl) -> float:
    """How much two things share: none rendered, or only touching faces, is 0."""
    return 0.0 if stl is None else abs(float(trimesh.load(stl).volume))


def bodies(stl):
    return [b for b in trimesh.load(stl).split(only_watertight=False) if abs(b.volume) > 1]


POUCH = "pouch([40, 25, 60], belt=40, {extra})"
BOX = "hinged_box([60, 40, 30], {extra})"


@pytest.mark.parametrize("template", [POUCH, BOX])
@pytest.mark.parametrize("angle", [0, 15, 90, 170])
def test_a_hinged_lid_swings_clear_of_the_box(tmp_path, template, angle):
    extra = f'layout="open", angle={angle}'
    body = template.format(extra=extra + ', part="body"')
    lid = template.format(extra=extra + ', part="lid"')
    stl, out = run(tmp_path, f"intersection() {{ {body}; {lid}; }}")
    assert overlap(stl) < 0.01, f"lid and box overlap at {angle} degrees"


@pytest.mark.parametrize("call, pieces", [
    ('pouch([40, 25, 60], belt=40) { relief(1) text("X", 12, halign="center", valign="center"); relief(1) circle(6); }', 2),
    ('hinged_box([60, 40, 30]) { relief(1) text("HI", 8, halign="center", valign="center"); relief(1) square([20, 5], center=true); }', 2),
    ('lidded_box([50, 50, 25]) relief(1) text("HI", 10, halign="center", valign="center");', 2),
    ("tray([120, 80, 25], rows=2, cols=3);", 1),
])
def test_templates_print_flat_in_whole_pieces(tmp_path, call, pieces):
    stl, out = run(tmp_path, call)
    assert stl is not None, out
    mesh = trimesh.load(stl)
    assert mesh.bounds[0][2] == pytest.approx(0, abs=0.01)  # on the plate
    assert mesh.is_watertight
    assert len(bodies(stl)) == pieces  # decoration fused on, nothing loose


def test_the_pouch_is_hollow_with_a_real_cavity(tmp_path):
    stl, out = run(tmp_path, 'pouch([40, 25, 60], belt=40, part="body");')
    box = max(bodies(stl), key=lambda b: b.volume)
    # A solid 40 x 25 x 60 block is 60,000 mm3; walls and a floor are a third of that.
    assert box.volume < 0.45 * 40 * 25 * 60


def test_a_lidded_box_lid_fits_inside_the_walls(tmp_path):
    body = 'lidded_box([50, 50, 25], layout="closed", part="body")'
    lid = 'lidded_box([50, 50, 25], layout="closed", part="lid")'
    stl, out = run(tmp_path, f"intersection() {{ {body}; {lid}; }}")
    assert overlap(stl) < 0.01, "the lid's lip runs into the walls"


def test_a_belt_too_wide_for_the_box_is_refused_in_words(tmp_path):
    stl, out = run(tmp_path, "pouch([40, 25, 40], belt=40);")
    assert "belt loop is too short" in out
