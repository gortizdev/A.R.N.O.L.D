"""check.py: what can be told about a part before it is printed.

Shapes with a known flaw go in; the flaw has to come out, in words, and a
sound part has to come out clean. The review model is stubbed.
"""

import pytest

trimesh = pytest.importorskip("trimesh")

from arnold import check, design
from arnold.commands import printer as printer_cmd

from test_adjust import bench, record  # noqa: F401 - bench is a fixture


def save(mesh, path):
    mesh.export(str(path))
    return path


def box(x, y, z, at=(0, 0, 0)):
    m = trimesh.creation.box(extents=[x, y, z])
    m.apply_translation([at[0], at[1], at[2] + z / 2])
    return m


def cup(tmp_path):
    outer = box(40, 30, 50)
    inner = box(36, 26, 50, at=(0, 0, 2))
    return save(outer.difference(inner), tmp_path / "cup.stl")


def test_a_sound_hollow_part_is_clean(tmp_path):
    report = check.geometry(cup(tmp_path), kind="make", request="a storage cup")
    assert report.ok, report.problems
    assert report.facts["pieces"] == 1 and report.facts["fill"] < 0.5


def test_a_container_with_no_cavity_is_caught(tmp_path):
    report = check.geometry(save(box(40, 30, 50), tmp_path / "solid.stl"), kind="make", request="a hollow pouch")
    assert any("no cavity" in p for p in report.problems)


def test_a_floating_piece_and_a_loose_crumb_are_caught(tmp_path):
    parts = [box(40, 30, 10), box(20, 20, 5, at=(40, 0, 6)), box(0.8, 0.8, 0.8, at=(-30, 0, 0))]
    report = check.geometry(save(trimesh.util.concatenate(parts), tmp_path / "bits.stl"), kind="make")
    joined = " ".join(report.problems)
    assert "floating" in joined and "loose fragment" in joined


def test_a_thin_fin_on_a_sculpture_is_caught(tmp_path):
    body = box(20, 20, 20)
    fin = box(0.6, 20, 30, at=(0, 0, 20))  # a 0.6 mm fin standing on it
    report = check.geometry(save(trimesh.boolean.union([body, fin]), tmp_path / "fin.stl"), kind="sculpt")
    assert any("thinner than 1.2 mm" in p for p in report.problems)


def test_a_figure_that_leans_off_its_base_is_caught(tmp_path):
    foot = box(4, 4, 2)
    arm = box(60, 6, 6, at=(29, 0, 1))  # a long arm reaching out past the foot, off the plate
    report = check.geometry(save(trimesh.boolean.union([foot, arm]), tmp_path / "lean.stl"), kind="sculpt")
    assert any("tip over" in p for p in report.problems)


def test_the_report_speaks_and_feeds_back():
    report = check.Report(problems=["the lid has no gap", "it is solid"], review={"fix": "cut a cavity"})
    assert report.spoken().startswith(" One thing to check: the lid has no gap.")
    assert "1 more" in report.spoken()
    assert "- it is solid" in report.feedback() and "cut a cavity" in report.feedback()


def test_the_review_joins_the_measurements(tmp_path, monkeypatch):
    monkeypatch.setattr(check, "views", lambda stl, out, openscad, **kw: [tmp_path / "v.png"])
    monkeypatch.setattr(check, "review", lambda *a, **kw: {"ok": False, "problems": ["the loop is closed"], "fix": "open it"})
    report = check.check(cup(tmp_path), kind="make", request="a cup", openscad=tmp_path / "openscad.exe",
                         work_dir=tmp_path / "w", model="m")
    assert report.problems == ["the loop is closed"] and report.review["fix"] == "open it"


@pytest.mark.skipif(printer_cmd.find_openscad() is None, reason="OpenSCAD is not installed")
class TestDesignLoop:
    BOX = "difference() { cube([30, 20, 40]); translate([2, 2, 2]) cube([26, 16, 40]); }\n"

    def test_findings_go_back_to_the_designer(self, bench, monkeypatch):
        calls = []

        def write(description, **kw):
            calls.append(kw.get("error", ""))
            return self.BOX

        verdicts = iter([["the flap has no hinge"], []])

        def fake_check(stl, **kw):
            return check.Report(problems=next(verdicts))

        monkeypatch.setattr(design, "write", write)
        monkeypatch.setattr(check, "check", fake_check)
        result = bench("printer.design", title="Box", prompt="a hinged box")
        assert result.ok, result.speech
        assert calls[0] == "" and "the flap has no hinge" in calls[1]
        part = record(bench, result.result["name"])
        assert part["reviews"] == 1 and part["check"]["problems"] == []

    def test_a_part_still_flawed_after_its_rounds_is_kept_with_a_warning(self, bench, monkeypatch):
        monkeypatch.setattr(design, "write", lambda description, **kw: self.BOX)
        monkeypatch.setattr(check, "check", lambda stl, **kw: check.Report(problems=["the lid is fused on"]))
        bench.ctx.config.printer.check_rounds = 1
        result = bench("printer.design", title="Box", prompt="a hinged box")
        assert result.ok and "One thing to check: the lid is fused on" in result.speech
        assert record(bench, result.result["name"])["check"]["problems"] == ["the lid is fused on"]

    def test_checking_can_be_switched_off(self, bench, monkeypatch):
        monkeypatch.setattr(design, "write", lambda description, **kw: self.BOX)
        bench.ctx.config.printer.check_enabled = False
        result = bench("printer.design", title="Box", prompt="a box")
        assert result.ok and "check" not in record(bench, result.result["name"])


class TestRank:
    """Choosing among a sculpture's candidate shapes: the vision model stubbed."""

    def stub(self, tmp_path, monkeypatch, answers):
        import io
        import json as json_

        from PIL import Image

        monkeypatch.setenv("OPENAI_API_KEY", "k")

        def views(stl, out_dir, openscad, **kw):
            out_dir.mkdir(parents=True, exist_ok=True)
            shots = []
            for name in ("three-quarter", "back"):
                Image.new("RGB", (64, 64), "white").save(out_dir / f"{name}.png")
                shots.append(out_dir / f"{name}.png")
            return shots

        asked = []

        class Reply:
            def __init__(self, text):
                self.text = text

            def read(self):
                return json_.dumps({"choices": [{"message": {"content": self.text}}]}).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def urlopen(request, timeout=0):
            body = json_.loads(request.data)
            # The letters as shown in this trial map back to candidate indexes
            # through the shuffle; answer by index so the test reads plainly.
            asked.append(body)
            return Reply(json_.dumps({"ranking": answers[len(asked) - 1]}))

        monkeypatch.setattr(check, "views", views)
        monkeypatch.setattr(check.urllib.request, "urlopen", urlopen)
        Image.new("RGB", (64, 64), "white").save(tmp_path / "ref.png")
        return asked

    def test_two_shuffled_looks_are_added_up(self, tmp_path, monkeypatch):
        import random

        # The shuffles rank() makes, so the answers can be written per candidate.
        letters = "ABC"
        orders = []
        for trial in range(2):
            order = [0, 1, 2]
            random.Random(trial).shuffle(order)
            orders.append(order)
        # Candidate 2 first both times; 0 and 1 swap places.
        want = [[2, 0, 1], [2, 1, 0]]
        answers = [[letters[orders[t].index(i)] for i in want[t]] for t in range(2)]
        asked = self.stub(tmp_path, monkeypatch, answers)
        stls = [tmp_path / f"{i}.stl" for i in range(3)]
        order = check.rank(tmp_path / "ref.png", stls, openscad=tmp_path / "openscad", work_dir=tmp_path / "w",
                           model="gpt-5.4-mini")
        assert len(asked) == 2 and order[0] == 2 and sorted(order) == [0, 1, 2]

    def test_no_key_means_no_choice(self, tmp_path, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        assert check.rank(tmp_path / "ref.png", [tmp_path / "a.stl", tmp_path / "b.stl"],
                          openscad=tmp_path / "o", work_dir=tmp_path / "w", model="m") is None

    def test_a_useless_answer_means_no_choice(self, tmp_path, monkeypatch):
        self.stub(tmp_path, monkeypatch, [["Z"], ["Q"]])
        assert check.rank(tmp_path / "ref.png", [tmp_path / "a.stl", tmp_path / "b.stl"],
                          openscad=tmp_path / "o", work_dir=tmp_path / "w", model="m") is None


def test_a_sculpture_is_reviewed_for_its_look(monkeypatch, tmp_path):
    import json as json_

    from PIL import Image

    monkeypatch.setenv("OPENAI_API_KEY", "k")
    sent = {}

    class Reply:
        def read(self):
            return json_.dumps({"choices": [{"message": {"content": '{"ok": true, "problems": []}'}}]}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(check.urllib.request, "urlopen", lambda r, timeout=0: sent.update(json_.loads(r.data)) or Reply())
    Image.new("RGB", (8, 8)).save(tmp_path / "r.png")
    check.review("an owl", check.Report(), [tmp_path / "r.png"], model="m", kind="sculpt")
    assert "LOOK" in sent["messages"][0]["content"]
    check.review("a box", check.Report(), [tmp_path / "r.png"], model="m", kind="make")
    assert "LOOK" not in sent["messages"][0]["content"]
