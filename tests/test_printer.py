"""The Centauri Carbon 2 watch: status parsing, events, and what is said."""

from __future__ import annotations

from arnold.commands.printer import describe
from arnold.config import PrinterConfig
from arnold.monitors.printer import PrinterWatch, job_name, summarise


def frame(status=2, sub=2075, progress=40, **job):
    return {
        "machine_status": {"status": status, "sub_status": sub},
        "print_status": {"filename": "Benchy_PLA.gcode", "progress": progress,
                         "current_layer": 80, "total_layer": 200,
                         "print_duration": 1800, "remaining_time_sec": 2700, **job},
        "extruder": {"temperature": 219.6, "target": 220},
        "heater_bed": {"temperature": 60.2, "target": 60},
    }


def watch_with(first):
    watch = PrinterWatch(PrinterConfig(enabled=True, host="10.0.0.9", serial="SN"))
    watch._absorb(first, full=True)
    return watch


def test_summary_reads_the_nested_status():
    s = summarise(frame())
    assert s["state"] == "printing"
    assert s["progress"] == 40
    assert (s["layer"], s["layers"]) == (80, 200)
    assert s["nozzle"] == 219.6 and s["bed"] == 60.2
    assert summarise(frame(sub=2502))["state"] == "paused"
    assert summarise(frame(sub=1405))["state"] == "heating"
    assert summarise(frame(status=1, sub=0))["state"] == "idle"


def test_first_frame_is_a_baseline_not_news():
    assert watch_with(frame()).drain_new() == []


def test_deltas_merge_and_raise_events():
    watch = watch_with(frame(status=1, sub=0, progress=0))
    watch._absorb({"machine_status": {"status": 2, "sub_status": 2075}}, full=False)
    assert [e["kind"] for e in watch.drain_new()] == ["started"]
    # A delta carries only what changed; the filename survives the merge.
    watch._absorb({"print_status": {"progress": 100}, "machine_status": {"sub_status": 2077}}, full=False)
    events = watch.drain_new()
    assert [e["kind"] for e in events] == ["finished"]
    assert events[0]["file"] == "Benchy_PLA.gcode"


def test_pause_resume_and_straight_to_idle_finish():
    watch = watch_with(frame())
    watch._absorb({"machine_status": {"sub_status": 2502}}, full=False)
    watch._absorb({"machine_status": {"sub_status": 2075}}, full=False)
    watch._absorb({"print_status": {"progress": 100}}, full=False)
    watch._absorb({"machine_status": {"status": 1, "sub_status": 0}}, full=False)
    assert [e["kind"] for e in watch.drain_new()] == ["paused", "resumed", "finished"]


def test_error_code_is_reported_once():
    watch = watch_with(frame())
    watch._absorb({"error_code": 7}, full=False)
    watch._absorb({"print_status": {"progress": 41}}, full=False)
    assert [e["kind"] for e in watch.drain_new()] == ["error"]


def test_spoken_status():
    printing = {"connected": True, **summarise(frame())}
    said = describe(printing)
    assert "printing Benchy PLA, 40 percent done, layer 80 of 200" in said
    assert "45 minutes to go" in said
    assert "nozzle 220, bed 60" in said
    assert "can't reach" in describe({"connected": False, "error": "it is off"})
    assert describe({"connected": True, **summarise(frame(status=1, sub=0))}).startswith("The printer is idle")


def test_job_name():
    assert job_name("/local/Big_Bracket_v2.gcode.3mf") == "Big Bracket v2"
    assert job_name("") == "the print"


# -- printer.make ----------------------------------------------------------------

import struct

import pytest

from arnold.commands import build_registry
from arnold.commands import printer as printer_cmd
from arnold.commands.registry import CommandContext
from arnold.config import Config

needs_openscad = pytest.mark.skipif(
    printer_cmd.find_openscad() is None, reason="OpenSCAD is not installed"
)


@pytest.fixture
def make(tmp_path, monkeypatch):
    config = Config()
    config.source_path = tmp_path / "config.yaml"
    config.source_path.write_text("")
    registry = build_registry()
    ctx = CommandContext(config=config, collector=None, alerts=None, jarvis=None)
    opened = []
    monkeypatch.setattr(printer_cmd, "_open_in_slicer", lambda slicer, stl: opened.append(stl))
    monkeypatch.setattr(printer_cmd, "find_slicer", lambda configured="": tmp_path / "slicer.exe")

    def go(**args):
        return registry.dispatch("printer.make", args, ctx)

    go.ctx, go.opened, go.dir = ctx, opened, tmp_path / "prints"
    return go


def binary_stl(path, triangles):
    body = b"".join(struct.pack("<12fH", 0, 0, 1, *a, *b, *c, 0) for a, b, c in triangles)
    path.write_bytes(b"\0" * 80 + struct.pack("<I", len(triangles)) + body)


def test_stl_size_reads_binary_and_ascii(tmp_path):
    binary_stl(tmp_path / "b.stl", [((0, 0, 0), (20, 0, 0), (0, 10, 5)), ((-1, 0, 0), (0, 0, 0), (0, 0, 0))])
    assert printer_cmd.stl_size(tmp_path / "b.stl") == (21, 10, 5)
    (tmp_path / "a.stl").write_text(
        "solid x\nfacet normal 0 0 1\nouter loop\nvertex 0 0 0\nvertex 3 0 0\n"
        "vertex 0 4 2.5\nendloop\nendfacet\nendsolid x\n"
    )
    assert printer_cmd.stl_size(tmp_path / "a.stl") == (3, 4, 2.5)
    assert printer_cmd.size_speech((20, 10.04, 5.25)) == "20 by 10 by 5.2 millimetres"


def test_openscad_errors_lose_the_file_path():
    out = ("ERROR: Parser error: syntax error in file ../../x/20260929-clip.scad, line 3\n"
           "Can't parse file 'C:/Users/x/20260929-clip.scad'!\n")
    assert printer_cmd._errors(out) == "ERROR: Parser error: syntax error line 3"


def test_a_fenced_answer_is_unwrapped():
    assert printer_cmd._source({"scad": "```openscad\ncube(5);\n```"}) == "cube(5);"
    assert printer_cmd._source({"scad": "cube(5);"}) == "cube(5);"


def test_make_needs_the_desktop():
    assert build_registry().get("printer.make").needs_desktop


def test_no_openscad_is_said_plainly(make, monkeypatch):
    monkeypatch.setattr(printer_cmd, "find_openscad", lambda configured="": None)
    result = make(title="clip", scad="cube(5);")
    assert not result.ok and "OpenSCAD" in result.speech


@needs_openscad
def test_a_part_is_rendered_measured_and_opened(make):
    result = make(title="Spacer!", scad="difference(){cube([20,10,5]);translate([10,5,-1])cylinder(d=4,h=7,$fn=32);}")
    assert result.ok, result.speech
    assert result.result["size_mm"] == [20, 10, 5]
    assert "20 by 10 by 5 millimetres" in result.speech
    assert len(make.opened) == 1 and make.opened[0].suffix == ".stl"
    assert make.opened[0].with_suffix(".scad").read_text() .startswith("difference")
    assert make.opened[0].parent == make.dir and "spacer" in make.opened[0].name


@needs_openscad
def test_open_false_leaves_the_slicer_alone(make):
    result = make(title="cube", scad="cube(8);", open=False)
    assert result.ok and not result.result["opened"] and make.opened == []


@needs_openscad
def test_a_syntax_error_comes_back_to_be_fixed_and_leaves_nothing(make):
    result = make(title="broken", scad="cube([1,1,1]")
    assert not result.ok
    assert "couldn't render" in result.speech and "line" in result.speech
    assert "scad" not in result.speech.lower().replace("openscad", "")
    assert list(make.dir.iterdir()) == []


@needs_openscad
def test_an_empty_result_and_a_part_too_big_are_refused(make):
    assert "no solid" in make(title="nothing", scad='echo("hi");').speech
    big = make(title="table", scad="cube([300,20,20]);")
    assert not big.ok and "build volume" in big.speech and "300 by 20 by 20" in big.speech
    tiny = make(title="speck", scad="cube(0.5);")
    assert not tiny.ok and "millimetre" in tiny.speech
    assert list(make.dir.iterdir()) == [] and make.opened == []
