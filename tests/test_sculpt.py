"""printer.sculpt: an AI mesh made printable, and the job around it.

The two network steps (the picture, the Space) are stubbed. What is tested
for real is what makes a raw generator mesh printable - the padding, the
crumbs, Y-up, the scale - and the job's manners: one at a time, background
when resident, said aloud when done.
"""

import shutil
import threading

import numpy as np
import pytest

trimesh = pytest.importorskip("trimesh")

from arnold import runtime, sculpt
from arnold.commands import build_registry
from arnold.commands import printer as printer_cmd
from arnold.commands.registry import CommandContext
from arnold.config import Config
from arnold.parts import Catalog


def generator_mesh(path, size=(2.0, 4.0, 1.0)):
    """What Hunyuan3D sends: Y-up, unitless, degenerate padding, a crumb."""
    body = trimesh.creation.icosphere(subdivisions=3)  # 1280 faces
    body.apply_scale(np.array(size) / body.extents)
    crumb = trimesh.creation.box(extents=(0.05, 0.05, 0.05))
    crumb.apply_translation([3, 0, 0])
    mesh = trimesh.util.concatenate([body, crumb])
    padding = np.zeros((600, 3), dtype=np.int64)  # every corner vertex 0
    raw = trimesh.Trimesh(mesh.vertices, np.vstack([mesh.faces, padding]), process=False)
    raw.export(str(path))
    return path


class TestToPart:
    def test_raw_mesh_becomes_a_grounded_z_up_part(self, tmp_path):
        part = sculpt.to_part(generator_mesh(tmp_path / "raw.glb"), tmp_path / "p.stl",
                              height_mm=60, max_size_mm=256, max_faces=0)
        assert part.watertight
        assert part.faces == 1280  # padding and crumb gone
        x, y, z = part.size
        # Y (4 units) was up; it is Z now, and 60 mm.
        assert z == pytest.approx(60, abs=0.01)
        assert x == pytest.approx(30, abs=0.5) and y == pytest.approx(15, abs=0.5)
        stl = trimesh.load(str(tmp_path / "p.stl"))
        assert stl.bounds[0][2] == pytest.approx(0, abs=1e-4)
        assert abs(stl.bounds[0][0] + stl.bounds[1][0]) < 1e-3  # centred

    def test_wide_things_are_held_to_the_bed(self, tmp_path):
        raw = generator_mesh(tmp_path / "raw.glb", size=(10.0, 1.0, 1.0))
        part = sculpt.to_part(raw, tmp_path / "p.stl", height_mm=60, max_size_mm=200, max_faces=0)
        assert max(part.size) == pytest.approx(200, abs=0.01)

    def test_dense_meshes_are_decimated(self, tmp_path):
        pytest.importorskip("fast_simplification")
        part = sculpt.to_part(generator_mesh(tmp_path / "raw.glb"), tmp_path / "p.stl",
                              height_mm=60, max_size_mm=256, max_faces=400)
        # The gentle pass may stop a little short of the target; it must stay whole.
        assert part.faces < 500 and part.watertight


class TestReplies:
    def test_the_mesh_path_comes_out_of_either_shape(self, tmp_path):
        f = tmp_path / "white_mesh.glb"
        f.write_bytes(b"x")
        assert sculpt._result_path(({"value": str(f), "__type__": "update"}, "<html>", {}, 1)) == str(f)
        assert sculpt._result_path((str(f), "", {}, 1)) == str(f)
        assert sculpt._result_path(({"value": None},)) is None

    def test_quota_is_explained(self):
        assert "hf auth login" in sculpt._space_error(Exception("You have exceeded your GPU quota"))

    def test_no_key_no_picture(self, tmp_path, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        with pytest.raises(sculpt.SculptError, match="OPENAI_API_KEY"):
            sculpt.draw("a frog", tmp_path / "f.png", model="gpt-image-1")


@pytest.fixture
def job(tmp_path, monkeypatch):
    """printer.sculpt with the picture and the Space stubbed out."""
    config = Config()
    config.source_path = tmp_path / "config.yaml"
    config.source_path.write_text("")
    said = []

    class Mouth:
        def say(self, text):
            said.append(text)

    ctx = CommandContext(config=config, collector=None, alerts=None, jarvis=None, speech=Mouth())
    fixture = generator_mesh(tmp_path / "fixture.glb")
    calls = {"draw": [], "shape": []}

    def draw(subject, out, *, model, **kw):
        calls["draw"].append(subject)
        out.write_bytes(b"png")
        return out

    def shape(image, out, **kw):
        calls["shape"].append(image)
        shutil.copyfile(fixture, out)
        return out

    monkeypatch.setattr(sculpt, "draw", draw)
    monkeypatch.setattr(sculpt, "shape", shape)
    monkeypatch.setattr(sculpt, "side_facing", lambda sheet, **kw: "left")
    opened = []
    monkeypatch.setattr(printer_cmd, "_open_in_slicer", lambda slicer, stl: opened.append(stl))
    monkeypatch.setattr(printer_cmd, "find_slicer", lambda configured="": tmp_path / "slicer.exe")
    monkeypatch.setattr(runtime, "IS_RESIDENT", False)
    printer_cmd._sculpting.clear()
    registry = build_registry()

    def go(**args):
        return registry.dispatch("printer.sculpt", args, ctx)

    go.ctx, go.said, go.opened, go.calls, go.dir = ctx, said, opened, calls, tmp_path / "prints"
    return go


class TestCommand:
    def test_one_shot_runs_to_the_slicer_and_answers(self, job):
        result = job(title="Frog", prompt="a sitting frog", height_mm=40)
        assert result.ok, result.speech
        assert result.speech.startswith("Frog is in the slicer: ")
        assert result.result["size_mm"][2] == pytest.approx(40, abs=0.01)
        assert job.calls["draw"] == ["a sitting frog"]
        assert len(job.opened) == 1
        names = sorted(p.suffix for p in job.dir.iterdir())
        assert names == [".glb", ".json", ".png", ".stl"]
        record = Catalog(job.dir).list()[0]
        assert record["state"] == "done" and record["prompt"] == "a sitting frog"
        assert set(record["stages"]) == {"drawing", "shaping", "cleaning", "checking"}
        assert all(len(span) == 2 for span in record["stages"].values())

    def test_a_picture_skips_the_drawing(self, job, tmp_path):
        photo = tmp_path / "Photo.JPG"
        photo.write_bytes(b"jpg")
        assert job(title="cat", image=str(photo)).ok
        assert job.calls["draw"] == []
        assert job.calls["shape"][0].suffix == ".jpg"

    def test_bad_requests_are_refused(self, job, tmp_path):
        assert "what to sculpt" in job(title="x").speech
        assert "PNG, JPEG" in job(image=str(tmp_path / "notes.txt")).speech
        assert "can't find" in job(image=str(tmp_path / "missing.png")).speech
        assert "between 5 and" in job(prompt="x", height_mm=1000).speech

    def test_a_failure_keeps_the_picture_and_says_why(self, job, monkeypatch):
        def broken(image, out, **kw):
            out.write_bytes(b"half")
            raise sculpt.SculptError("The 3D model couldn't make that: busy")
        monkeypatch.setattr(sculpt, "shape", broken)
        result = job(prompt="a frog")
        assert not result.ok and "busy" in result.speech
        # The half-made mesh is gone; the picture it cost to draw is kept.
        assert sorted(p.suffix for p in job.dir.iterdir()) == [".json", ".png"]
        record = Catalog(job.dir).list()[0]
        assert record["state"] == "failed" and "busy" in record["error"]
        assert record["picture"] == "png"
        assert printer_cmd._sculpting == []

    def test_resident_runs_in_the_background_and_says_when_done(self, job, monkeypatch):
        monkeypatch.setattr(runtime, "IS_RESIDENT", True)
        done = threading.Event()
        monkeypatch.setattr(printer_cmd, "_say", lambda ctx, text: (job.said.append(text), done.set()))
        result = job(title="Dragon", prompt="a dragon")
        assert result.ok and result.result["state"] == "running"
        assert "I'll say when" in result.speech
        assert done.wait(10)
        assert job.said[0].startswith("Dragon is in the slicer")

    def test_one_at_a_time(self, job):
        printer_cmd._sculpting.append("the dragon")
        result = job(prompt="a frog")
        assert not result.ok and "still working on the dragon" in result.speech
        printer_cmd._sculpting.clear()

    def test_it_can_be_switched_off(self, job):
        job.ctx.config.printer.sculpt_enabled = False
        assert "switched off" in job(prompt="a frog").speech


class TestCollection:
    def test_parts_open_and_forget(self, job):
        job(title="Frog", prompt="a frog")
        listed = job.ctx and build_registry().dispatch("printer.parts", {}, job.ctx)
        assert listed.ok and listed.result["count"] == 1
        name = listed.result["parts"][0]["name"]
        assert "the newest is Frog" in listed.speech
        opened = build_registry().dispatch("printer.open", {"name": "frog"}, job.ctx)
        assert opened.ok and job.opened[-1].name == name + ".stl"
        gone = build_registry().dispatch("printer.forget", {"name": name}, job.ctx)
        assert gone.ok and list(job.dir.iterdir()) == []

    def test_forget_refuses_a_part_in_the_making_and_strange_names(self, job):
        Catalog(job.dir).write("20260929-120000-frog", title="Frog", state="shaping")
        assert "still being made" in build_registry().dispatch(
            "printer.forget", {"name": "20260929-120000-frog"}, job.ctx).speech
        assert not build_registry().dispatch("printer.forget", {"name": "../config"}, job.ctx).ok


class TestCatalog:
    def test_stems_outside_the_pattern_are_never_served(self, tmp_path):
        catalog = Catalog(tmp_path)
        (tmp_path / "20260929-120000-owl.stl").write_bytes(b"x")
        assert catalog.file("20260929-120000-owl", "stl") is not None
        assert catalog.file("20260929-120000-owl", "exe") is None
        assert catalog.file("../20260929-120000-owl", "stl") is None
        assert catalog.file("20260929-120000-owl/../../x", "stl") is None

    def test_an_old_stl_without_a_record_still_counts(self, tmp_path):
        (tmp_path / "20260929-142339-cable-clip.stl").write_bytes(b"x")
        (tmp_path / "20260929-142339-cable-clip.scad").write_text("cube(1);")
        (tmp_path / "stray.png").write_bytes(b"x")
        [part] = Catalog(tmp_path).list()
        assert part["title"] == "Cable clip" and part["kind"] == "make" and part["state"] == "done"

    def test_stages_are_timed_and_a_dead_job_is_called_interrupted(self, tmp_path):
        catalog = Catalog(tmp_path)
        catalog.write("20260929-120000-owl", title="Owl", state="queued")
        catalog.stage("20260929-120000-owl", "drawing")
        catalog.stage("20260929-120000-owl", "shaping")
        record = catalog.read("20260929-120000-owl")
        assert len(record["stages"]["drawing"]) == 2 and len(record["stages"]["shaping"]) == 1
        assert catalog.list()[0]["state"] == "shaping"
        assert catalog.list(stale_after=-1)[0]["state"] == "failed"


class TestViews:
    def sheet(self, path):
        from PIL import Image, ImageDraw

        image = Image.new("RGB", (1536, 1024), (250, 250, 248))
        draw = ImageDraw.Draw(image)
        draw.rectangle((150, 200, 450, 900), fill=(90, 90, 90))    # front, left of its half's middle
        draw.rectangle((1000, 250, 1250, 900), fill=(90, 90, 90))  # back
        image.save(path)
        return path

    def test_a_sheet_splits_into_two_centred_squares_at_one_scale(self, tmp_path):
        from PIL import Image, ImageOps

        views = sculpt.split_views(self.sheet(tmp_path / "sheet.png"), tmp_path / "views")
        assert set(views) == {"front", "back"}
        for name, width in (("front", 301), ("back", 251)):
            image = Image.open(views[name])
            assert image.size == (1024, 1024)
            box = ImageOps.invert(image.convert("L")).point(lambda v: 255 if v > 24 else 0).getbbox()
            assert box[2] - box[0] == width  # not rescaled
            assert abs((box[0] + box[2]) / 2 - 512) <= 2  # centred

    def test_auto_uses_this_pc_only_once_it_is_all_there(self, tmp_path):
        local = sculpt.Local(python=tmp_path / "python.exe", repo=tmp_path / "repo", weights=tmp_path / "w")
        assert sculpt.where("auto", local) == "space"
        local.python.write_text("")
        (local.repo / "hy3dgen").mkdir(parents=True)
        assert sculpt.where("auto", local) == "space"  # no weights yet
        weights = local.weights / local.model / local.subfolder
        weights.mkdir(parents=True)
        (weights / "model.fp16.safetensors").write_bytes(b"x")
        assert sculpt.where("auto", local) == "local"
        assert sculpt.where("space", local) == "space"
        assert sculpt.where("local", None) == "local"

    def test_the_workers_answer_is_read_from_its_last_line(self, tmp_path, monkeypatch):
        import subprocess

        from arnold import process

        local = sculpt.Local(python=tmp_path / "python.exe", repo=tmp_path, weights=tmp_path)
        out = tmp_path / "mesh.glb"
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            out.write_bytes(b"glb")
            return subprocess.CompletedProcess(argv, 0, stdout='loading...\n{"ok": true, "faces": 9}\n', stderr="")

        monkeypatch.setattr(process, "run", run)
        views = {"front": tmp_path / "f.png", "back": tmp_path / "b.png"}
        assert sculpt.shape_local(views, out, local, 60) == out
        argv = calls[0]
        assert argv[argv.index("--view") + 1].startswith("front=") and "--flashvdm" in argv
        assert argv[argv.index("--weights") + 1] == str(tmp_path)

        def oom(argv, **kw):
            return subprocess.CompletedProcess(argv, 1, stdout='{"ok": false, "error": "OutOfMemoryError: CUDA out of memory"}\n', stderr="")

        out.unlink()
        monkeypatch.setattr(process, "run", oom)
        with pytest.raises(sculpt.SculptError, match="is a game running"):
            sculpt.shape_local(views, out, local, 60)

    def test_several_views_go_to_the_multiview_space(self, tmp_path, monkeypatch):
        import gradio_client

        sent = {}
        mesh = tmp_path / "white_mesh.glb"
        mesh.write_bytes(b"glb")

        class Job:
            def result(self, timeout):
                return ({"value": str(mesh)}, "", {}, 1)

        class Client:
            def __init__(self, space, **kw):
                sent["space"] = space

            def submit(self, **kw):
                sent.update(kw)
                return Job()

        monkeypatch.setattr(gradio_client, "Client", Client)
        monkeypatch.setattr(gradio_client, "handle_file", lambda p: p)
        views = {"front": tmp_path / "f.png", "back": tmp_path / "b.png"}
        sculpt.shape_space(views, tmp_path / "out.glb", space="tencent/Hunyuan3D-2mv")
        assert sent["space"] == "tencent/Hunyuan3D-2mv"
        assert sent["mv_image_front"].endswith("f.png") and sent["mv_image_back"].endswith("b.png")
        assert "image" not in sent

    def test_a_drawn_sculpture_records_its_views_and_which_way_the_side_faces(self, job):
        result = job(title="Frog", prompt="a frog")
        record = Catalog(job.dir).list()[0]
        assert record["views"] == "front-side-back" and record["side"] == "left"
        assert result.ok

    def test_another_take_keeps_the_answer(self, job, monkeypatch):
        first = job(title="Frog", prompt="a frog").result["name"]
        asked = []
        monkeypatch.setattr(sculpt, "side_facing", lambda sheet, **kw: asked.append(sheet) or "right")
        again = job(again=first).result["name"]
        assert Catalog(job.dir).read(again)["side"] == "left" and asked == []

    def test_three_views_split_in_order(self, tmp_path):
        from PIL import Image, ImageDraw

        image = Image.new("RGB", (1536, 1024), (250, 250, 248))
        draw = ImageDraw.Draw(image)
        for left, width in ((180, 150), (700, 90), (1200, 170)):
            draw.rectangle((left, 200, left + width - 1, 900), fill=(90, 90, 90))
        image.save(tmp_path / "sheet.png")
        views = sculpt.split_views(tmp_path / "sheet.png", tmp_path / "v", sculpt.THREE)
        assert list(views) == ["front", "side", "back"]
        from PIL import ImageOps

        widths = []
        for path in views.values():
            box = ImageOps.invert(Image.open(path).convert("L")).point(lambda v: 255 if v > 24 else 0).getbbox()
            widths.append(box[2] - box[0])
        assert widths == [150, 90, 170]

    @pytest.mark.parametrize("left, right, cut", [
        (180, 1200, []),              # every view whole
        (0, 1200, ["front"]),         # the front runs off the left edge
        (180, 1400, ["back"]),        # the back runs off the right edge
    ])
    def test_a_view_the_drawing_cropped_is_found(self, tmp_path, left, right, cut):
        from PIL import Image, ImageDraw

        image = Image.new("RGB", (1536, 1024), (238, 231, 226))  # a cream backdrop, as drawn
        draw = ImageDraw.Draw(image)
        draw.rectangle((left, 300, left + 250, 700), fill=(120, 120, 120))
        draw.rectangle((650, 300, 900, 700), fill=(120, 120, 120))
        draw.rectangle((right, 300, right + 250, 700), fill=(120, 120, 120))
        image.save(tmp_path / "sheet.png")
        assert sculpt.cut_off(tmp_path / "sheet.png", sculpt.THREE) == cut

    @pytest.mark.parametrize("left_after, draws", [([], 1), (["front"], 2)])
    def test_a_cropped_sheet_is_mended_else_drawn_again(self, job, monkeypatch, left_after, draws):
        mended = []
        monkeypatch.setattr(sculpt, "cut_off", lambda sheet, views: ["front"])
        monkeypatch.setattr(sculpt, "uncrop", lambda sheet, views, **kw: mended.append(kw["cut"]) or left_after)
        result = job(title="Car", prompt="an F1 car", views="front-side-back")
        assert result.ok, result.speech
        assert mended == [["front"]] and len(job.calls["draw"]) == draws

    def test_another_take_mends_the_old_sheet(self, job, monkeypatch):
        first = job(title="Car", prompt="an F1 car", views="front-side-back").result["name"]
        mended = []
        monkeypatch.setattr(sculpt, "cut_off", lambda sheet, views: ["front"])
        monkeypatch.setattr(sculpt, "uncrop", lambda sheet, views, **kw: mended.append(sheet.name) or [])
        again = job(again=first).result["name"]
        assert mended == [f"{again}.png"]  # the new part's copy, not the original

    def test_a_mend_that_cannot_be_had_still_makes_the_part(self, job, monkeypatch):
        monkeypatch.setattr(sculpt, "cut_off", lambda sheet, views: ["front"])

        def fail(sheet, views, **kw):
            raise sculpt.SculptError("no key")

        monkeypatch.setattr(sculpt, "uncrop", fail)
        assert job(title="Car", prompt="an F1 car", views="front-side-back").ok

    def cropped_sheet(self, path, left=0):
        from PIL import Image, ImageDraw

        image = Image.new("RGB", (1536, 1024), (238, 231, 226))
        draw = ImageDraw.Draw(image)
        for x in (left, 650, 1100):
            draw.rectangle((x, 300, x + 250, 700), fill=(120, 120, 120))
        image.save(path)
        return path

    def test_uncrop_paints_only_the_border_and_keeps_a_better_sheet(self, tmp_path, monkeypatch):
        import io

        from PIL import Image

        monkeypatch.setenv("OPENAI_API_KEY", "k")
        sent = {}

        def post(key, fields, picture, timeout, mask=None):
            sent.update(fields, mask=Image.open(io.BytesIO(mask)))
            return self.cropped_sheet(tmp_path / "whole.png", left=150).read_bytes()

        monkeypatch.setattr(sculpt, "_post_edit", post)
        sheet = self.cropped_sheet(tmp_path / "sheet.png")
        assert sculpt.uncrop(sheet, sculpt.THREE, model="gpt-image-1.5") == []
        assert "the view on the left" in sent["prompt"] and sent["size"] == "1536x1024"
        alpha = sent["mask"].getchannel("A")
        assert alpha.getpixel((5, 5)) == 0 and alpha.getpixel((768, 512)) == 255
        assert sculpt.cut_off(sheet, sculpt.THREE) == []  # replaced by the mended one

    def test_uncrop_keeps_the_original_when_it_is_no_better(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        before = self.cropped_sheet(tmp_path / "sheet.png").read_bytes()
        monkeypatch.setattr(sculpt, "_post_edit", lambda *a, **kw: before)
        assert sculpt.uncrop(tmp_path / "sheet.png", sculpt.THREE, model="m") == ["front"]
        assert (tmp_path / "sheet.png").read_bytes() == before

    def test_gaps_that_show_the_backdrop_are_not_filled(self):
        pytest.importorskip("cv2")
        import numpy as np
        from PIL import Image

        from arnold.hy3d_worker import plain_matte

        # A grey frame on cream: the window in it shows the backdrop, so it
        # is air - but a light patch of clay, far below the backdrop, is not.
        rgb = np.full((400, 400, 3), (238, 231, 226), np.uint8)
        rgb[100:300, 100:300] = 120
        rgb[140:260, 140:190] = (238, 231, 226)  # the window
        rgb[140:260, 220:260] = 200              # a highlight on the clay
        alpha = np.asarray(plain_matte(Image.fromarray(rgb)))[:, :, 3]
        assert alpha[200, 165] < 64 and alpha[200, 240] > 192 and alpha[120, 120] > 192

    @pytest.mark.parametrize("facing, expected", [("left", {"front", "left", "back"}),
                                                  ("right", {"front", "right", "back"}),
                                                  (None, {"front", "back"})])
    def test_the_side_view_is_named_for_its_facing_or_left_out(self, tmp_path, monkeypatch, facing, expected):
        from PIL import Image

        Image.new("RGB", (1536, 1024), "white").save(tmp_path / "sheet.png")
        seen = {}
        monkeypatch.setattr(sculpt, "shape_space", lambda views, out, **kw: seen.update(views) or out)
        sculpt.shape(tmp_path / "sheet.png", tmp_path / "o.glb", views=sculpt.THREE, side=facing,
                     backend="space")
        assert set(seen) == expected

    def test_no_key_means_no_guess_about_the_side(self, tmp_path, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        assert sculpt.side_facing(tmp_path / "sheet.png", model="gpt-5.4-mini") is None

    def test_a_users_picture_is_one_view(self, job, tmp_path):
        photo = tmp_path / "cat.png"
        photo.write_bytes(b"png")
        job(title="Cat", image=str(photo))
        assert Catalog(job.dir).list()[0]["views"] == "front"


class TestCandidates:
    def test_the_candidates_are_found_in_order(self, tmp_path):
        out = tmp_path / "20260930-120000-car.glb"
        for name in ("20260930-120000-car.glb", "20260930-120000-car.10.glb", "20260930-120000-car.2.glb",
                     "20260930-120000-car.1.glb", "20260930-120000-cart.glb"):
            (tmp_path / name).write_bytes(b"x")
        assert [p.name for p in sculpt.candidates_of(out)] == [
            "20260930-120000-car.glb", "20260930-120000-car.1.glb", "20260930-120000-car.2.glb",
            "20260930-120000-car.10.glb"]

    def test_the_best_looking_shape_is_kept(self, job, monkeypatch, tmp_path):
        from arnold import check

        fixture = generator_mesh(tmp_path / "cand.glb")

        def shape(image, out, *, candidates=1, **kw):
            for i in range(candidates):
                shutil.copyfile(fixture, out if i == 0 else out.with_name(f"{out.stem}.{i}.glb"))
            return out

        ranked = []
        monkeypatch.setattr(sculpt, "shape", shape)
        monkeypatch.setattr(sculpt, "where", lambda backend, local: "local")
        monkeypatch.setattr(printer_cmd, "find_openscad", lambda configured="": tmp_path / "openscad.exe")
        monkeypatch.setattr(check, "rank", lambda ref, stls, **kw: ranked.append(len(stls)) or [2, 0, 1])
        result = job(title="Car", prompt="an F1 car")
        assert result.ok, result.speech
        record = Catalog(job.dir).list()[0]
        assert ranked == [3] and record["chosen"] == 3 and record["ranking"] == [3, 1, 2]
        assert "choosing" in record["stages"]
        assert sorted(p.name for p in job.dir.glob("*.glb")) == [f"{record['name']}.glb"]  # the rest gone
        assert not list(job.dir.glob(".choose-*"))

    def test_without_a_ranking_the_first_is_kept(self, job, monkeypatch, tmp_path):
        from arnold import check

        fixture = generator_mesh(tmp_path / "cand.glb")

        def shape(image, out, *, candidates=1, **kw):
            for i in range(candidates):
                shutil.copyfile(fixture, out if i == 0 else out.with_name(f"{out.stem}.{i}.glb"))
            return out

        monkeypatch.setattr(sculpt, "shape", shape)
        monkeypatch.setattr(sculpt, "where", lambda backend, local: "local")
        monkeypatch.setattr(check, "rank", lambda ref, stls, **kw: None)
        assert job(title="Car", prompt="an F1 car").ok
        record = Catalog(job.dir).list()[0]
        assert record["chosen"] == 1 and record["ranking"] is None


class TestCloudCandidate:
    def stub(self, job, monkeypatch, tmp_path, cloud_ok=True):
        from arnold import check, tencent

        fixture = generator_mesh(tmp_path / "cand.glb")
        shaped = []

        def shape(image, out, *, candidates=1, backend="auto", **kw):
            shaped.append(backend)
            if backend == "tencent":
                if not cloud_ok:
                    raise sculpt.SculptError("Tencent Cloud said LimitExceeded")
                shutil.copyfile(fixture, out)
                return out
            for i in range(candidates):
                shutil.copyfile(fixture, out if i == 0 else out.with_name(f"{out.stem}.{i}.glb"))
            return out

        monkeypatch.setattr(sculpt, "shape", shape)
        monkeypatch.setattr(sculpt, "where", lambda backend, local: "local")
        monkeypatch.setattr(tencent, "configured", lambda: True)
        monkeypatch.setattr(printer_cmd, "find_openscad", lambda configured="": tmp_path / "openscad.exe")
        ranked = []
        # The last candidate first: with the cloud's shape, that is the cloud's.
        monkeypatch.setattr(check, "rank", lambda ref, stls, **kw: ranked.append(len(stls))
                            or list(range(len(stls)))[::-1])
        job.ctx.config.printer.sculpt_tencent_extra = True
        return shaped, ranked

    def test_a_tencent_shape_joins_the_choosing(self, job, monkeypatch, tmp_path):
        shaped, ranked = self.stub(job, monkeypatch, tmp_path)
        assert job(title="Car", prompt="an F1 car").ok
        record = Catalog(job.dir).list()[0]
        assert sorted(shaped) == ["auto", "tencent"] and ranked == [4]
        assert record["cloud"] == 4 and record["chosen"] == 4  # the cloud shape won here

    def test_a_cloud_failure_leaves_this_pcs_shapes(self, job, monkeypatch, tmp_path):
        shaped, ranked = self.stub(job, monkeypatch, tmp_path, cloud_ok=False)
        from arnold import check

        monkeypatch.setattr(check, "rank", lambda ref, stls, **kw: ranked.append(len(stls)) or [1, 0, 2])
        assert job(title="Car", prompt="an F1 car").ok
        record = Catalog(job.dir).list()[0]
        assert ranked == [3] and "cloud" not in record and record["chosen"] == 2

    def test_off_by_default(self, job, monkeypatch, tmp_path):
        shaped, _ = self.stub(job, monkeypatch, tmp_path)
        job.ctx.config.printer.sculpt_tencent_extra = False
        assert job(title="Car", prompt="an F1 car").ok
        assert shaped == ["auto"]


class TestFourViews:
    def sheet(self, path, *, crop_bottom_right=False):
        from PIL import Image, ImageDraw

        image = Image.new("RGB", (1536, 1024), (238, 231, 226))
        draw = ImageDraw.Draw(image)
        # front and back narrow in the top row, the two long profiles below
        draw.rectangle((250, 150, 450, 380), fill=(120, 120, 120))
        draw.rectangle((1050, 150, 1250, 380), fill=(120, 120, 120))
        draw.rectangle((80, 640, 700, 880), fill=(120, 120, 120))
        right = 1536 if crop_bottom_right else 1450
        draw.rectangle((850, 640, right, 880), fill=(120, 120, 120))
        image.save(path)
        return path

    def test_a_grid_splits_row_by_row_at_one_scale(self, tmp_path):
        from PIL import Image, ImageOps

        views = sculpt.split_views(self.sheet(tmp_path / "s.png"), tmp_path / "v", sculpt.FOUR)
        assert list(views) == ["front", "back", "side", "side2"]
        widths = []
        for path in views.values():
            box = ImageOps.invert(Image.open(path).convert("L")).point(lambda v: 255 if v > 40 else 0).getbbox()
            widths.append(box[2] - box[0])
        assert widths == [201, 201, 621, 601]  # nothing rescaled or sliced

    def test_only_the_view_against_the_edge_is_cut_off(self, tmp_path):
        assert sculpt.cut_off(self.sheet(tmp_path / "s.png"), sculpt.FOUR) == []
        assert sculpt.cut_off(self.sheet(tmp_path / "c.png", crop_bottom_right=True), sculpt.FOUR) == ["side2"]

    @pytest.mark.parametrize("facings, expected", [
        (["left", "right"], {"front", "back", "left", "right"}),
        (["right", "right"], {"front", "back", "right"}),     # the same side twice: once
        ([None, "left"], {"front", "back", "left"}),           # an unclear one left out
    ])
    def test_both_sides_are_named_for_their_facing(self, tmp_path, monkeypatch, facings, expected):
        seen = {}
        monkeypatch.setattr(sculpt, "shape_space", lambda views, out, **kw: seen.update(views) or out)
        sculpt.shape(self.sheet(tmp_path / "s.png"), tmp_path / "o.glb", views=sculpt.FOUR, side=facings,
                     backend="space")
        assert set(seen) == expected

    def test_the_facings_are_asked_of_both_bottom_views(self, tmp_path, monkeypatch):
        asked = []
        monkeypatch.setattr(sculpt, "_ask_about", lambda sheet, question, **kw: asked.append(question)
                            or {"bottom_left": "right", "bottom_right": "unsure"})
        assert sculpt.side_facing(tmp_path / "s.png", model="m", views=sculpt.FOUR) == ["right", None]
        assert "bottom_left" in asked[0]

    def test_a_drawn_four_view_sculpture_keeps_both_facings(self, job, monkeypatch):
        monkeypatch.setattr(sculpt, "side_facing", lambda sheet, **kw: ["left", "right"]
                            if kw.get("views") == sculpt.FOUR else "left")
        job.ctx.config.printer.sculpt_views = sculpt.FOUR
        result = job(title="Car", prompt="an F1 car")
        assert result.ok, result.speech
        record = Catalog(job.dir).list()[0]
        assert record["views"] == sculpt.FOUR and record["side"] == ["left", "right"]
