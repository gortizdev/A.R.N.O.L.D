"""emblem.py: a stencil traced into an SVG that OpenSCAD embosses.

The image model is not called here; the tracing and the rendering are real,
because an emblem that comes out as noise or will not import is the failure
that matters.
"""

import shutil
import subprocess

import pytest

trimesh = pytest.importorskip("trimesh")
pytest.importorskip("skimage")

from PIL import Image, ImageDraw

from arnold import emblem
from arnold.commands import printer as printer_cmd
from arnold.design import LIBRARY


def stencil(path, *, inverted=False, speck=True, alpha=False):
    """A ring 2:1 wide beside a solid block, and a speck too small to print."""
    ink, paper = ("white", "black") if inverted else ("black", "white")
    image = Image.new("RGBA" if alpha else "RGB", (600, 400), (0, 0, 0, 0) if alpha else paper)
    draw = ImageDraw.Draw(image)
    draw.ellipse([100, 100, 300, 300], fill=ink)
    draw.ellipse([160, 160, 240, 240], fill=(0, 0, 0, 0) if alpha else paper)
    draw.rectangle([340, 150, 500, 250], fill=ink)
    if speck:
        draw.point([(550, 50)], fill=ink)
    image.save(path)
    return path


class TestTrace:
    def test_outlines_with_their_holes_and_no_specks(self, tmp_path):
        svg = tmp_path / "e.svg"
        aspect = emblem.trace(stencil(tmp_path / "s.png"), svg)
        text = svg.read_text()
        assert 'fill-rule="evenodd"' in text
        # The ring's outside, its hole and the block - the speck is gone.
        assert text.count("M") == 3
        assert aspect == pytest.approx(200 / 400, abs=0.05)  # cropped to the ink

    def test_an_inverted_stencil_traces_the_same(self, tmp_path):
        a = emblem.trace(stencil(tmp_path / "a.png"), tmp_path / "a.svg")
        b = emblem.trace(stencil(tmp_path / "b.png", inverted=True), tmp_path / "b.svg")
        assert a == pytest.approx(b, abs=0.02)
        assert (tmp_path / "b.svg").read_text().count("M") == 3

    def test_transparency_is_background(self, tmp_path):
        emblem.trace(stencil(tmp_path / "t.png", alpha=True), tmp_path / "t.svg")
        assert (tmp_path / "t.svg").read_text().count("M") == 3

    def test_a_blank_picture_is_refused(self, tmp_path):
        Image.new("RGB", (100, 100), "white").save(tmp_path / "blank.png")
        with pytest.raises(emblem.EmblemError, match="blank"):
            emblem.trace(tmp_path / "blank.png", tmp_path / "blank.svg")


@pytest.mark.skipif(printer_cmd.find_openscad() is None, reason="OpenSCAD is not installed")
def test_openscad_embosses_it_at_the_width_asked(tmp_path):
    emblem.trace(stencil(tmp_path / "s.png"), tmp_path / "part.svg")
    shutil.copyfile(LIBRARY, tmp_path / LIBRARY.name)
    scad = tmp_path / "t.scad"
    scad.write_text(f'include <{LIBRARY.name}>\nemblem("part.svg", 40, 1);\n', encoding="utf-8")
    done = subprocess.run([str(printer_cmd.find_openscad()), "-o", str(tmp_path / "t.stl"), str(scad)],
                          capture_output=True, text=True, timeout=120)
    assert (tmp_path / "t.stl").is_file(), done.stdout + done.stderr
    mesh = trimesh.load(tmp_path / "t.stl")
    width, height, depth = mesh.extents
    assert width == pytest.approx(40, abs=0.2) and height == pytest.approx(20, abs=1)
    assert depth == pytest.approx(1, abs=0.01)
    assert abs(mesh.bounds.mean(axis=0)[0]) < 0.5  # centred
    # 0.1 mm a pixel: a 10 mm ring with a 4 mm hole in it, and a 16 x 10 block.
    assert mesh.volume == pytest.approx(3.1416 * (10 ** 2 - 4 ** 2) + 16 * 10, rel=0.04)


@pytest.mark.parametrize("text, wanted", [
    ("put a wolf emblem on the flap", True),
    ("add the Cyclops logo to the lid", True),
    ("a crest on the front", True),
    ("walls 2 mm thicker", False),
    ("rounder corners", False),
])
def test_words_that_ask_for_a_picture(text, wanted):
    assert emblem.wanted(text) is wanted
