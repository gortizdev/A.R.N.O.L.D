"""printer.adjust: a new version of a part with one change, the original kept.

The models are stubbed; OpenSCAD and trimesh are real, because whether the
changed part renders and lands at the right size is the point.
"""

import shutil

import pytest

trimesh = pytest.importorskip("trimesh")

from arnold import adjust, runtime, sculpt
from arnold.commands import build_registry
from arnold.commands import printer as printer_cmd
from arnold.commands.registry import CommandContext
from arnold.config import Config
from arnold.parts import Catalog

from test_sculpt import generator_mesh

needs_openscad = pytest.mark.skipif(
    printer_cmd.find_openscad() is None, reason="OpenSCAD is not installed"
)


@pytest.fixture
def bench(tmp_path, monkeypatch):
    config = Config()
    config.source_path = tmp_path / "config.yaml"
    config.source_path.write_text("")
    ctx = CommandContext(config=config, collector=None, alerts=None, jarvis=None)
    registry = build_registry()
    fixture = generator_mesh(tmp_path / "fixture.glb")

    monkeypatch.setattr(sculpt, "draw", lambda subject, out, *, model, **kw: out.write_bytes(b"png") or out)
    monkeypatch.setattr(sculpt, "shape", lambda image, out, **kw: shutil.copyfile(fixture, out) or out)
    edits = []

    def edit(picture, change, out, *, model, **kw):
        edits.append((picture.name, change, model))
        out.write_bytes(b"edited png")
        return out

    monkeypatch.setattr(sculpt, "edit", edit)
    monkeypatch.setattr(sculpt, "side_facing", lambda sheet, **kw: "left")
    monkeypatch.setattr(printer_cmd, "_open_in_slicer", lambda slicer, stl: None)
    monkeypatch.setattr(printer_cmd, "find_slicer", lambda configured="": tmp_path / "slicer.exe")
    monkeypatch.setattr(runtime, "IS_RESIDENT", False)
    printer_cmd._sculpting.clear()

    def run(command, **args):
        return registry.dispatch(command, args, ctx)

    run.ctx, run.edits, run.dir = ctx, edits, tmp_path / "prints"
    return run


def record(run, name):
    return next(p for p in Catalog(run.dir).list() if p["name"] == name)


class TestSculpture:
    def test_a_change_edits_the_picture_and_keeps_the_original(self, bench):
        first = bench("printer.sculpt", title="Turtle", prompt="a turtle", height_mm=40)
        result = bench("printer.adjust", name="turtle", change="give it a party hat")
        assert result.ok, result.speech
        assert result.speech.startswith("Turtle, version 2, is in the slicer")
        new = record(bench, result.result["name"])
        assert new["parent"] == first.result["name"] and new["version"] == 2
        assert new["mode"] == "picture" and new["change"] == "give it a party hat"
        assert set(new["stages"]) == {"editing", "shaping", "cleaning", "checking"}
        assert new["size_mm"][2] == pytest.approx(40, abs=0.01)  # same height as before
        assert bench.edits == [(first.result["name"] + ".png", "give it a party hat", "gpt-image-1.5")]
        assert (bench.dir / (first.result["name"] + ".stl")).exists()  # original kept

    def test_a_height_alone_only_resizes(self, bench):
        first = bench("printer.sculpt", title="Owl", prompt="an owl", height_mm=60)
        result = bench("printer.adjust", name=first.result["name"], height_mm=90)
        assert result.ok, result.speech
        new = record(bench, result.result["name"])
        assert new["mode"] == "resize" and new["size_mm"][2] == pytest.approx(90, abs=0.01)
        assert "picture" in new and bench.edits == []
        assert new["change"] == "resized to 90 mm tall"

    def test_versions_count_up_and_share_a_root(self, bench):
        first = bench("printer.sculpt", title="Owl", prompt="an owl").result["name"]
        second = bench("printer.adjust", name=first, change="bigger eyes").result["name"]
        third = bench("printer.adjust", name=second, change="a hat").result["name"]
        assert record(bench, third)["version"] == 3
        assert record(bench, third)["root"] == first

    def test_a_failed_edit_is_a_failed_version(self, bench, monkeypatch):
        bench("printer.sculpt", title="Owl", prompt="an owl")

        def refuse(*a, **kw):
            raise sculpt.SculptError("The image model wouldn't make that change: nope")

        monkeypatch.setattr(sculpt, "edit", refuse)
        result = bench("printer.adjust", change="a hat")
        assert not result.ok and "nope" in result.speech
        failed = Catalog(bench.dir).list()[0]
        assert failed["state"] == "failed" and failed["version"] == 2

    def test_nothing_to_do_is_refused(self, bench):
        bench("printer.sculpt", title="Owl", prompt="an owl")
        assert "what to change" in bench("printer.adjust").speech


@needs_openscad
class TestDesignedPart:
    CODE = "w = 20;\ncube([w, 10, 5]);\n"

    def make(self, bench):
        return bench("printer.make", title="Block", scad=self.CODE, open=False).result["name"]

    def test_the_code_is_rewritten_and_rendered(self, bench, monkeypatch):
        seen = []

        def rewrite(code, change, *, model, error="", previous=""):
            seen.append((change, model, error))
            return code.replace("w = 20", "w = 30")

        monkeypatch.setattr(adjust, "rewrite", rewrite)
        first = self.make(bench)
        result = bench("printer.adjust", name="block", change="make it 30 mm wide")
        assert result.ok, result.speech
        new = record(bench, result.result["name"])
        assert new["size_mm"] == [30, 10, 5] and new["parent"] == first
        assert (bench.dir / (new["name"] + ".scad")).read_text().startswith("w = 30")
        assert seen == [("make it 30 mm wide", "gpt-5.4-mini", "")]

    def test_a_render_error_goes_back_to_the_model(self, bench, monkeypatch):
        attempts = []

        def rewrite(code, change, *, model, error="", previous=""):
            attempts.append(error)
            return "cube([30, 10, 5]" if not error else "cube([30, 10, 5]);\n"

        monkeypatch.setattr(adjust, "rewrite", rewrite)
        self.make(bench)
        result = bench("printer.adjust", change="wider")
        assert result.ok, result.speech
        assert attempts[0] == "" and "line" in attempts[1]

    def test_three_bad_attempts_fail_cleanly(self, bench, monkeypatch):
        monkeypatch.setattr(adjust, "rewrite", lambda *a, **kw: "cube([1,1,1]")
        self.make(bench)
        result = bench("printer.adjust", change="wider")
        assert not result.ok and "wouldn't render" in result.speech
        failed = Catalog(bench.dir).list()[0]
        assert failed["state"] == "failed" and "stl" not in failed["files"]

    def test_a_designed_part_needs_words_not_a_height(self, bench):
        self.make(bench)
        assert "size lives in its code" in bench("printer.adjust", height_mm=40).speech


def test_a_fenced_reply_is_unwrapped():
    assert adjust.unfence("Here you go:\n```openscad\ncube(1);\n```\n") == "cube(1);\n"
    assert adjust.unfence("cube(1);") == "cube(1);\n"
