"""Slicer settings from a part's shape.

The shapes are boxes written as STL here, so each rule is tested against a
shape whose answer is known: a cube needs nothing, a tower needs a brim, a
table top on a post needs supports, a plate on its edge wants laying down.
"""

import struct

import pytest

from arnold import slicing
from arnold.commands import build_registry
from arnold.commands import printer as printer_cmd
from arnold.commands.registry import CommandContext
from arnold.config import Config
from arnold.parts import Catalog


def box(x0, y0, z0, x1, y1, z1):
    """Twelve outward-facing triangles."""
    p = {(i, j, k): ((x0, x1)[i], (y0, y1)[j], (z0, z1)[k]) for i in (0, 1) for j in (0, 1) for k in (0, 1)}
    quads = [((0, 0, 0), (0, 1, 0), (1, 1, 0), (1, 0, 0)), ((0, 0, 1), (1, 0, 1), (1, 1, 1), (0, 1, 1)),
             ((0, 0, 0), (1, 0, 0), (1, 0, 1), (0, 0, 1)), ((0, 1, 0), (0, 1, 1), (1, 1, 1), (1, 1, 0)),
             ((0, 0, 0), (0, 0, 1), (0, 1, 1), (0, 1, 0)), ((1, 0, 0), (1, 1, 0), (1, 1, 1), (1, 0, 1))]
    tris = []
    for a, b, c, d in quads:
        tris += [(p[a], p[b], p[c]), (p[a], p[c], p[d])]
    return tris


def stl(path, *boxes):
    tris = [t for b in boxes for t in b]
    data = bytearray(80) + struct.pack("<I", len(tris))
    for tri in tris:
        data += struct.pack("<12fH", 0, 0, 0, *tri[0], *tri[1], *tri[2], 0)
    path.write_bytes(bytes(data))
    return path


def setting(found, name):
    return slicing.settings_value(found["settings"], name)


def test_a_cube_needs_nothing_special(tmp_path):
    shape = slicing.measure(stl(tmp_path / "cube.stl", box(0, 0, 0, 20, 20, 20)))
    assert shape.size == pytest.approx((20, 20, 20))
    assert shape.volume == pytest.approx(8000)
    assert shape.contact == pytest.approx(400)
    assert shape.overhang == 0 and shape.pieces == 1 and shape.lifted == 0
    found = slicing.recommend(shape, kind="make")
    assert setting(found, "Enable support") == "Off"
    assert setting(found, "Brim") == "None (auto)"
    assert setting(found, "Layer height") == "0.12 mm"  # small
    assert not setting(found, "Lay on face")
    assert found["summary"].endswith("g of PLA.")


def test_a_tower_gets_a_brim(tmp_path):
    found = slicing.for_part(stl(tmp_path / "t.stl", box(0, 0, 0, 10, 10, 60)), kind="sculpt")
    assert setting(found, "Brim").startswith("Outer brim")
    assert "60 mm tall on a base 10 mm across" in next(s["why"] for s in found["settings"] if s["name"] == "Brim")


def test_a_table_top_on_a_post_needs_supports(tmp_path):
    path = stl(tmp_path / "t.stl", box(15, 15, 0, 25, 25, 30), box(0, 0, 30, 40, 40, 35))
    shape = slicing.measure(path, faces=False)
    assert shape.overhang == pytest.approx(1600)
    found = slicing.recommend(shape, kind="sculpt")
    assert setting(found, "Enable support").startswith("On - Tree")
    assert setting(found, "Seam position") == "Back"


def test_a_plate_on_its_edge_is_laid_down(tmp_path):
    found = slicing.for_part(stl(tmp_path / "p.stl", box(0, 0, 0, 60, 3, 40)), kind="make")
    assert setting(found, "Lay on face").startswith("its front") or setting(found, "Lay on face").startswith("its back")
    assert found["summary"].startswith("laid on its")


def test_pieces_drawn_at_different_heights_are_split(tmp_path):
    path = stl(tmp_path / "two.stl", box(0, 0, 0, 30, 30, 20), box(40, 0, 12, 70, 30, 14))
    found = slicing.for_part(path, kind="make")
    assert found["shape"]["pieces"] == 2 and found["shape"]["lifted"] == pytest.approx(12)
    assert setting(found, "Split").startswith("To objects")
    assert any("separate pieces" in n for n in found["notes"])
    # Measured as split: the lid sits on the bed, so nothing overhangs.
    assert setting(found, "Enable support") == "Off"


def test_the_filament_sets_temperatures_and_unknown_is_pla(tmp_path):
    path = stl(tmp_path / "c.stl", box(0, 0, 0, 30, 30, 30))
    assert setting(slicing.for_part(path, kind="make", material="petg"), "Nozzle / bed") == "245 / 75 °C"
    assert slicing.for_part(path, kind="make", material="unobtainium")["material"] == "PLA"
    assert slicing.for_part(path, kind="make", watertight=False)["notes"]


class TestCommand:
    @pytest.fixture
    def part(self, tmp_path):
        config = Config()
        config.source_path = tmp_path / "config.yaml"
        config.source_path.write_text("")
        ctx = CommandContext(config=config, collector=None, alerts=None, jarvis=None)
        stem = "20260929-120000-tower"
        prints = printer_cmd.prints_dir(ctx)
        stl(prints / f"{stem}.stl", box(0, 0, 0, 10, 10, 60))
        Catalog(prints).write(stem, title="Tower", kind="sculpt", state="done", created=1.0)
        return ctx, stem, prints

    def test_worked_out_once_and_kept(self, part, monkeypatch):
        ctx, stem, prints = part
        registry = build_registry()
        result = registry.dispatch("printer.settings", {"name": "tower"}, ctx)
        assert result.ok, result.speech
        assert result.speech.startswith("For Tower: ")
        assert Catalog(prints).read(stem)["slicing"]["summary"] == result.result["summary"]
        monkeypatch.setattr(slicing, "measure", lambda *a, **k: pytest.fail("measured again"))
        assert registry.dispatch("printer.settings", {"name": "tower"}, ctx).ok

    def test_a_new_filament_works_them_out_again(self, part):
        ctx, stem, prints = part
        registry = build_registry()
        registry.dispatch("printer.settings", {}, ctx)
        ctx.config.printer.filament = "PETG"
        result = registry.dispatch("printer.settings", {}, ctx)
        assert result.result["material"] == "PETG"
        assert Catalog(prints).read(stem)["slicing"]["material"] == "PETG"
