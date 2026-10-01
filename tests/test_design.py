"""printer.design, and a sculpture asked for something only a design can do.

The design model is stubbed; OpenSCAD is real, because a part that does not
render is exactly what the retry loop is for.
"""

import pytest

pytest.importorskip("trimesh")

from arnold import design
from arnold.commands import printer as printer_cmd
from arnold.parts import Catalog

from test_adjust import bench, record  # noqa: F401 - bench is a fixture

needs_openscad = pytest.mark.skipif(
    printer_cmd.find_openscad() is None, reason="OpenSCAD is not installed"
)

BOX = (
    "wall = 2;\n"
    "difference() { cube([30, 20, 40]); translate([wall, wall, wall]) cube([30 - 2*wall, 20 - 2*wall, 40]); }\n"
)


class TestLooksFunctional:
    @pytest.mark.parametrize("text", [
        "make the inside hollow and give the flap a hinge so it can be actually usable",
        "a rectangular storage pouch with a snap-button flap and a belt loop",
        "walls 2 mm thicker",
        "a phone holder shaped like a frog",
    ])
    def test_working_parts(self, text):
        assert design.looks_functional(text)

    @pytest.mark.parametrize("text", [
        "a small cartoon dragon sitting with its mouth open",
        "give it a party hat",
        "an owl on a branch",
        "bigger eyes",
    ])
    def test_shapes(self, text):
        assert not design.looks_functional(text)


def test_the_brief_carries_the_change_and_the_old_size():
    text = design.brief_text("a belt pouch", change="make it hollow", size_mm=[30.3, 24.7, 60])
    assert "a belt pouch" in text and "make it hollow" in text and "30 x 25 x 60" in text


@needs_openscad
class TestDesign:
    def stub(self, monkeypatch, replies):
        calls = []

        def write(description, **kw):
            calls.append({"description": description, **kw})
            return replies[min(len(calls), len(replies)) - 1]

        monkeypatch.setattr(design, "write", write)
        return calls

    def test_a_described_part_is_designed(self, bench, monkeypatch):
        calls = self.stub(monkeypatch, [BOX])
        result = bench("printer.design", title="Box", prompt="a 30 by 20 box, 40 tall, open top")
        assert result.ok, result.speech
        part = record(bench, result.result["name"])
        assert part["kind"] == "make" and part["mode"] == "design"
        assert part["size_mm"] == [30, 20, 40]
        assert calls[0]["model"] == "gpt-5.4" and calls[0]["fallback_model"] == "gpt-5.4-mini"

    def test_a_render_error_goes_back_to_the_model(self, bench, monkeypatch):
        calls = self.stub(monkeypatch, ["cube([1,1,1]", BOX])
        result = bench("printer.design", title="Box", prompt="a box with a lid")
        assert result.ok, result.speech
        assert calls[0]["error"] == "" and calls[1]["error"]

    def test_a_working_part_asked_of_sculpt_is_designed(self, bench, monkeypatch):
        self.stub(monkeypatch, [BOX])
        result = bench("printer.sculpt", title="Pouch", prompt="a storage pouch with a belt loop")
        assert result.ok, result.speech
        assert "designing it properly" in result.speech or "is in the slicer" in result.speech
        assert record(bench, result.result["name"])["kind"] == "make"
        assert bench.edits == []

    def test_a_working_part_with_a_picture_is_designed_from_it(self, bench, monkeypatch, tmp_path):
        from arnold import emblem
        from test_emblem import stencil

        calls = self.stub(monkeypatch, [BOX])
        monkeypatch.setattr(emblem, "draw", lambda subject, out, **kw: stencil(out))
        picture = stencil(tmp_path / "hero.png")
        result = bench("printer.sculpt", title="Clip", prompt="a clip to join webbing from 3 points",
                       image=str(picture))
        assert result.ok, result.speech
        part = record(bench, result.result["name"])
        assert part["mode"] == "design" and "svg" in part["files"]  # the picture is its emblem
        assert calls[0]["picture"] is not None and calls[0]["emblem"]

    def test_organic_keeps_sculpting(self, bench, monkeypatch):
        self.stub(monkeypatch, [BOX])
        result = bench("printer.sculpt", title="Pouch", prompt="a storage pouch", organic=True)
        assert result.ok, result.speech
        assert record(bench, result.result["name"])["kind"] == "sculpt"

    def test_a_sculpture_asked_for_a_hinge_becomes_a_design(self, bench, monkeypatch):
        calls = self.stub(monkeypatch, [BOX])
        first = bench("printer.sculpt", title="Pouch", prompt="a cyclops belt pouch", organic=True)
        change = "make the inside hollow and give the flap a hinge"
        result = bench("printer.adjust", name=first.result["name"], change=change)
        assert result.ok, result.speech
        new = record(bench, result.result["name"])
        assert new["kind"] == "make" and new["mode"] == "design" and new["version"] == 2
        assert new["parent"] == first.result["name"]
        # Designed from the sculpture's own description, picture and size.
        assert calls[0]["description"] == "a cyclops belt pouch" and calls[0]["change"] == change
        assert calls[0]["picture"] is not None and calls[0]["picture"].name.startswith(new["name"])
        assert calls[0]["size_mm"] and new["picture"]  # the concept is kept with it
        assert bench.edits == []  # the picture was not edited
        # And its next change is a code edit, like any designed part.
        assert "scad" in new["files"]

    def test_design_false_keeps_editing_the_picture(self, bench, monkeypatch):
        self.stub(monkeypatch, [BOX])
        bench("printer.sculpt", title="Pouch", prompt="a pouch", organic=True)
        result = bench("printer.adjust", change="give the flap a hinge", design=False)
        assert result.ok, result.speech
        assert record(bench, result.result["name"])["mode"] == "picture"

    def test_a_looks_change_still_edits_the_picture(self, bench, monkeypatch):
        self.stub(monkeypatch, [BOX])
        bench("printer.sculpt", title="Owl", prompt="an owl")
        result = bench("printer.adjust", change="bigger eyes")
        assert record(bench, result.result["name"])["mode"] == "picture"
        assert len(Catalog(bench.dir).list()) == 2


POUCH = "include <arnold_parts.scad>\nbelt_w = 40;\npouch([40, 25, 60], belt=belt_w, corner=4);\n"


@needs_openscad
class TestRestyle:
    """A designed part's new look: an emblem traced for it, the mechanism held."""

    def stub(self, monkeypatch, replies):
        from arnold import emblem
        from test_emblem import stencil

        calls, draws = [], []

        def write(description, **kw):
            calls.append({"description": description, **kw})
            reply = replies[min(len(calls), len(replies)) - 1]
            return reply(kw) if callable(reply) else reply

        def draw(subject, out, *, model, picture=None):
            draws.append({"subject": subject, "picture": picture})
            return stencil(out)

        monkeypatch.setattr(design, "write", write)
        monkeypatch.setattr(emblem, "draw", draw)
        return calls, draws

    @staticmethod
    def marked(kw, belt=40):
        return ("include <arnold_parts.scad>\n"
                f"pouch([40, 25, 60], belt={belt}, corner=6) {{\n"
                "  union() {}\n"
                f'  emblem("{kw["emblem"][0]}", 20);\n'
                "}\n")

    def test_an_emblem_in_words_restyles_and_keeps_the_mechanism(self, bench, monkeypatch):
        calls, draws = self.stub(monkeypatch, [POUCH, self.marked])
        first = bench("printer.design", title="Pouch", prompt="a belt pouch")
        assert first.ok, first.speech
        result = bench("printer.adjust", change="put a wolf emblem on the flap")
        assert result.ok, result.speech
        new = record(bench, result.result["name"])
        assert new["mode"] == "restyle" and new["emblem"] and new["parent"] == first.result["name"]
        assert "svg" in new["files"] and "tracing" in new["stages"]
        assert calls[1]["base"] == POUCH and calls[1]["emblem"][0] == f"{new['name']}.svg"
        assert draws == [{"subject": "put a wolf emblem on the flap", "picture": None}]

    def test_a_restyle_that_moves_the_mechanism_goes_back(self, bench, monkeypatch):
        calls, _ = self.stub(monkeypatch, [POUCH, lambda kw: self.marked(kw, belt=50), self.marked])
        bench("printer.design", title="Pouch", prompt="a belt pouch")
        result = bench("printer.adjust", change="put a wolf emblem on the flap")
        assert result.ok, result.speech
        assert "belt=40" in calls[2]["error"]
        assert "belt=40" in (bench.dir / f"{result.result['name']}.scad").read_text()

    def test_a_restyle_that_keeps_moving_it_is_given_up(self, bench, monkeypatch):
        self.stub(monkeypatch, [POUCH, lambda kw: self.marked(kw, belt=50)])
        bench("printer.design", title="Pouch", prompt="a belt pouch")
        result = bench("printer.adjust", change="put a wolf emblem on the flap")
        assert not result.ok and "kept changing how the part works" in (result.error or result.speech)

    def test_a_picture_alone_becomes_the_emblem(self, bench, monkeypatch, tmp_path):
        from test_emblem import stencil

        calls, draws = self.stub(monkeypatch, [POUCH, self.marked])
        bench("printer.design", title="Pouch", prompt="a belt pouch")
        crest = stencil(tmp_path / "crest.png")
        result = bench("printer.adjust", image=str(crest))
        assert result.ok, result.speech
        new = record(bench, result.result["name"])
        assert new["change"] == "put the picture on it as an emblem"
        assert draws[0]["picture"] == crest
        assert new["picture"] == "png"  # the picture is kept as its concept

    def test_a_code_change_carries_the_emblem(self, bench, monkeypatch):
        from arnold import adjust

        self.stub(monkeypatch, [POUCH, self.marked])
        monkeypatch.setattr(adjust, "rewrite", lambda code, change, **kw: code)
        bench("printer.design", title="Pouch", prompt="a belt pouch")
        styled = bench("printer.adjust", change="put a wolf emblem on the flap").result["name"]
        result = bench("printer.adjust", change="walls 2 mm thicker")
        assert result.ok, result.speech
        new = record(bench, result.result["name"])
        assert new["mode"] == "code" and "svg" in new["files"]
        code = (bench.dir / f"{new['name']}.scad").read_text()
        assert f"{new['name']}.svg" in code and styled not in code

    def test_a_design_from_a_picture_carries_it_as_an_emblem(self, bench, monkeypatch, tmp_path):
        from test_emblem import stencil

        calls, draws = self.stub(monkeypatch, [self.marked])
        hero = stencil(tmp_path / "hero.png")
        result = bench("printer.design", title="Clip", prompt="a belt pouch", image=str(hero))
        assert result.ok, result.speech
        assert draws[0]["picture"] == hero and calls[0]["emblem"]
        assert calls[0]["picture"] is not None  # and the picture is still the concept


class TestKeptMechanism:
    def test_decoration_and_corners_may_change(self):
        after = ('include <arnold_parts.scad>\npouch([40, 25, 60], belt = 40, corner=8) {\n'
                 '  relief(1) text("W", 12, halign="center", valign="center");\n}\n')
        assert design.kept_mechanism(POUCH, after) == ""

    def test_a_moved_mechanism_is_named_with_its_old_value(self):
        after = "include <arnold_parts.scad>\npouch([40, 25, 60], belt=50);\n"
        assert "belt=40" in design.kept_mechanism(POUCH, after)

    def test_a_default_given_a_value_is_moved(self):
        after = "include <arnold_parts.scad>\npouch([40, 25, 60], belt=40, wall=3);\n"
        assert "wall left at its default" in design.kept_mechanism(POUCH, after)

    def test_the_template_must_stay(self):
        after = "include <arnold_parts.scad>\nhinged_box([40, 25, 60]);\n"
        assert "pouch()" in design.kept_mechanism(POUCH, after)

    def test_a_file_on_no_template_is_not_held(self):
        assert design.kept_mechanism(BOX, "cube(1);") == ""


def test_the_template_a_file_builds_on_is_named():
    assert design.template_used("include <arnold_parts.scad>\npouch([40, 25, 60], belt=40);") == "pouch"
    assert design.template_used("include <arnold_parts.scad>\nhinged_box([60, 40, 30]);") == "hinged_box"
    assert design.template_used("include <arnold_parts.scad>\nrounded_box([1, 1, 1]);") == ""
    assert design.template_used("pouch([40, 25, 60]);") == ""  # not the library's without the include
    assert set(design.GUARANTEES) == set(design.TEMPLATES)


def test_a_template_layout_is_advised_to_print_without_supports(tmp_path):
    trimesh = pytest.importorskip("trimesh")
    from arnold import slicing

    pytest.importorskip("manifold3d")
    # A block with a small ledge half way up - the size of overhang a template's
    # bridges make, which the plain rules would still support.
    block = trimesh.creation.box(extents=[20, 20, 20])
    block.apply_translation([0, 0, 10])
    ledge = trimesh.creation.box(extents=[12, 9, 2])
    ledge.apply_translation([0, 14, 11])
    stl = tmp_path / "ledge.stl"
    trimesh.boolean.union([block, ledge], engine="manifold").export(str(stl))
    plain = slicing.for_part(stl, kind="make")
    laid_out = slicing.for_part(stl, kind="make", unsupported=True)
    assert slicing.settings_value(plain["settings"], "Enable support") != "Off"
    assert slicing.settings_value(laid_out["settings"], "Enable support") == "Off"
