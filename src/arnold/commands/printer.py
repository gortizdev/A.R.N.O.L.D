"""printer.status: how the 3D printer is getting on. printer.make: a part for it.

Status is read from the agent's state file rather than the printer itself:
the Centauri Carbon 2 takes only a few clients, and the agent already holds
one (monitors/printer.py).

printer.make is parametric, not generative: the model writes OpenSCAD, which
gets dimensions exactly right and can be edited ("make it 5 mm wider") by
changing a number. OpenSCAD renders it to STL and Elegoo Slicer opens that
for a look. It stops there on purpose - nothing is sent to the printer, so a
person sees every part on the plate before it is printed. A render error is
handed back word for word, so the model can fix its code and try again.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import struct
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .. import process
from ..humanize import duration_speech
from ..parts import WORKING, Catalog
from ..monitors.printer import job_name
from ..platform_win import foreground
from ..state import read_state
from .registry import CommandContext, CommandError, CommandResult, Registry, arg_bool, arg_int, arg_str

log = logging.getLogger(__name__)

MAX_SCAD_BYTES = 200_000
KEEP = 100  # parts kept on disk before the oldest are pruned


def register_all(registry: Registry) -> None:
    registry.register(
        "printer.status", _status,
        "The 3D printer: what it is printing, how far along, time left and temperatures.",
    )
    registry.register(
        "printer.make", _make,
        "Design a 3D-printable part in OpenSCAD, render it and open it in Elegoo Slicer.",
        {
            "title": "what the part is",
            "scad": "OpenSCAD source, in millimetres, sitting on z=0",
            "open": "false to render it without opening the slicer",
        },
        needs_desktop=True,
    )
    registry.register(
        "printer.sculpt", _sculpt,
        "Sculpt an organic 3D-printable shape from a description or a picture with AI, "
        "then open it in Elegoo Slicer. Takes about a minute and says when it is done.",
        {
            "title": "what it is",
            "prompt": "what to sculpt, described visually",
            "image": "or a path to a picture of it on this PC",
            "height_mm": "how tall, default printer.sculpt_height_mm",
            "again": "a part's name: sculpt again from its picture",
            "views": "with a picture: front (default), front-back or front-side-back if it is a turnaround sheet",
            "open": "false to make it without opening the slicer",
        },
        needs_desktop=True,
    )
    registry.register(
        "printer.brief", _brief,
        "Before sculpting a short request: the questions whose answers would make it more "
        "precise, and the detailed description to sculpt from so far.",
        {
            "prompt": "what was asked for",
            "answers": 'the answers so far, as JSON: [{"question":"...","answer":"..."}]',
            "questions": "how many to ask at most, default 4; 0 for the final description only",
        },
    )
    registry.register(
        "printer.adjust", _adjust,
        "Make a new version of a part with one change - a sculpture's picture is edited and "
        "shaped again, a designed part's OpenSCAD is rewritten. The original is kept.",
        {
            "name": "the part's name or title, or omit for the newest",
            "change": "what to change, in plain words",
            "height_mm": "a new height (a sculpture only; alone, it just resizes)",
            "open": "false to make it without opening the slicer",
        },
        needs_desktop=True,
    )
    registry.register(
        "printer.settings", _settings,
        "The slicer settings worked out for one part from its shape - layer height, walls, "
        "infill, supports, brim, temperatures - each with the reason.",
        {"name": "the part's name or title, or omit for the newest"},
    )
    registry.register(
        "printer.parts", _parts,
        "The parts made for the printer so far, newest first, and any being made now.",
        {"limit": "how many, default 20"},
    )
    registry.register(
        "printer.open", _open_part,
        "Open a part made earlier in Elegoo Slicer.",
        {"name": "the part's name or title, or omit for the newest"},
        needs_desktop=True,
    )
    registry.register(
        "printer.forget", _forget_part,
        "Delete a part made earlier, with its picture and mesh.",
        {"name": "the part's name"},
    )


def printer_state(ctx: CommandContext) -> dict[str, Any]:
    if not ctx.config.printer.enabled:
        raise CommandError("I'm not set up to watch the printer. Switch on the printer section in my config.")
    state = read_state(Path(ctx.config.state_file))
    if state is None:
        raise CommandError("My background agent isn't running, so I can't see the printer right now.")
    printer = state.get("printer")
    if not printer:
        raise CommandError("The agent hasn't reported on the printer yet.")
    return printer


def describe(printer: dict[str, Any]) -> str:
    """One or two sentences, for speaking."""
    if not printer.get("connected"):
        why = printer.get("error") or "it may be switched off"
        return f"I can't reach the printer at the moment: {why}."
    state = printer.get("state") or "unknown"
    what = job_name(printer.get("file") or "")
    progress = printer.get("progress")
    left = printer.get("remaining_seconds")
    if state in ("printing", "heating", "paused"):
        parts = [
            f"The printer is {'heating up for' if state == 'heating' else state} {what}"
            + (f", {progress} percent done" if progress is not None and state != "heating" else "")
        ]
        if printer.get("layer") and printer.get("layers"):
            parts[0] += f", layer {printer['layer']} of {printer['layers']}"
        if left and state != "paused":
            parts.append(f"About {duration_speech(float(left))} to go.")
        temps = _temps(printer)
        if temps:
            parts.append(temps)
        return ". ".join(p.rstrip(".") for p in parts) + "."
    if state == "finished":
        return f"The printer has finished {what} and is waiting for you to take it off."
    if state == "idle":
        return "The printer is idle." + (f" Last job was {what}." if printer.get("file") else "")
    return f"The printer is {state}."


def _temps(printer: dict[str, Any]) -> str:
    nozzle, bed = printer.get("nozzle"), printer.get("bed")
    if nozzle is None and bed is None:
        return ""
    bits = []
    if nozzle is not None:
        bits.append(f"nozzle {nozzle:.0f}")
    if bed is not None:
        bits.append(f"bed {bed:.0f}")
    return "Temperatures: " + ", ".join(bits) + " degrees"


def _status(ctx: CommandContext, args: dict) -> CommandResult:
    printer = printer_state(ctx)
    return CommandResult(speech=describe(printer), result=printer)


# -- printer.make ------------------------------------------------------------


def prints_dir(ctx: CommandContext) -> Path:
    """Where parts live: beside the config, as artifacts do, and for the same
    reason - the agent's working directory is Task Scheduler's choice."""
    base = ctx.config.source_path.resolve().parent if ctx.config.source_path else Path.cwd()
    path = base / "prints"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _program_files(*parts: str) -> list[Path]:
    roots = [os.environ.get("ProgramFiles", r"C:\Program Files"),
             os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")]
    return [Path(root, *parts) for root in roots]


def find_openscad(configured: str = "") -> Path | None:
    # openscad.com before openscad.exe: the .com is the console build, the
    # one whose errors come back on stderr rather than in a dialog.
    return _find(
        configured,
        _program_files("OpenSCAD", "openscad.com") + _program_files("OpenSCAD", "openscad.exe"),
        "openscad",
    )


def find_slicer(configured: str = "") -> Path | None:
    return _find(configured, _program_files("ElegooSlicer", "elegoo-slicer.exe"), "elegoo-slicer")


def _find(configured: str, candidates: list[Path], on_path: str) -> Path | None:
    if configured:
        path = Path(os.path.expandvars(configured)).expanduser()
        return path if path.is_file() else None
    for path in candidates:
        if path.is_file():
            return path
    found = shutil.which(on_path)
    return Path(found) if found else None


def _slug(title: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    return (text or "part")[:48]


def _source(args: dict) -> str:
    """The OpenSCAD, with any markdown fence the model wrapped it in removed."""
    code = arg_str(args, "scad")
    fenced = re.match(r"^```[\w-]*[ \t]*\n(.*?)\n?```$", code, re.S)
    if fenced:
        code = fenced.group(1)
    if len(code.encode("utf-8")) > MAX_SCAD_BYTES:
        raise CommandError("That OpenSCAD is too long to render.")
    return code


def render(openscad: Path, scad: Path, stl: Path, timeout: int) -> None:
    """scad -> binary STL, or a CommandError carrying what OpenSCAD said."""
    try:
        done = process.run(
            [str(openscad), "-o", str(stl), "--export-format", "binstl", str(scad)],
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise CommandError(
            f"OpenSCAD was still rendering after {timeout} seconds, so I stopped it. "
            "Fewer facets or fewer boolean operations should help."
        ) from None
    output = f"{done.stdout}\n{done.stderr}"
    if done.returncode == 0 and stl.is_file() and stl.stat().st_size > 84:
        return
    if "top level object is empty" in output.lower():
        raise CommandError("The OpenSCAD ran but made no solid - nothing is left once it has run.")
    raise CommandError("OpenSCAD couldn't render that: " + _errors(output))


def _errors(output: str) -> str:
    lines = [ln.strip() for ln in output.splitlines() if ln.strip()]
    wanted = [ln for ln in lines if ln.startswith(("ERROR", "WARNING"))] or lines[-3:]
    text = " ".join(wanted[:6]) or "no error message."
    # "in file ../../x/part.scad, line 3" is only "line 3" to whoever wrote it.
    text = re.sub(r"in file .*?, (line \d+)", r"\1", text)
    return re.sub(r"Can't parse file '[^']*'!?", "", text).strip()


def stl_size(path: Path) -> tuple[float, float, float]:
    """Bounding box, x by y by z, of an STL - binary, or ASCII as a fallback."""
    data = path.read_bytes()
    points: list[tuple[float, ...]] = []
    count = struct.unpack_from("<I", data, 80)[0] if len(data) >= 84 else -1
    if count >= 0 and len(data) == 84 + 50 * count:
        for tri in struct.iter_unpack("<12fH", data[84:]):
            points += (tri[3:6], tri[6:9], tri[9:12])
    else:
        for match in re.finditer(rb"vertex\s+(\S+)\s+(\S+)\s+(\S+)", data):
            points.append(tuple(float(v) for v in match.groups()))
    if not points:
        raise CommandError("The part came out with no surfaces.")
    xs, ys, zs = zip(*points)
    return (max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs))


def _facet_count(path: Path) -> int:
    try:
        with open(path, "rb") as fh:
            head = fh.read(84)
        return struct.unpack_from("<I", head, 80)[0] if len(head) == 84 else 0
    except OSError:
        return 0


def size_speech(size: tuple[float, float, float]) -> str:
    return " by ".join(f"{round(v, 1):g}" for v in size) + " millimetres"


def _prune(directory: Path) -> None:
    for old in Catalog(directory).list()[KEEP:]:
        if old["state"] not in WORKING:
            Catalog(directory).delete(old["name"])


def _new_stem(title: str, directory: Path) -> str:
    """A name no part has yet. The second is usually enough; a version made
    straight after its parent, with the same title, is where it is not."""
    base = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{_slug(title)}"[:63]
    stem, n = base, 1
    while any(directory.glob(f"{stem}.*")):
        n += 1
        stem = f"{base[:60]}-{n}"
    return stem


def _open_in_slicer(slicer: Path, stl: Path) -> None:
    foreground.allow_foreground()
    process.launch([str(slicer), str(stl)])
    # The slicer takes a while to come up cold; give it longer than a browser.
    threading.Thread(
        target=foreground.raise_process_windows,
        args=({slicer.name.lower()},),
        kwargs={"timeout": 20.0},
        daemon=True,
        name="raise-slicer",
    ).start()


def _render_checked(openscad: Path, scad: Path, stl: Path, cfg: Any, title: str) -> tuple[float, float, float]:
    """Render, then refuse a part the printer cannot take or that was drawn
    in the wrong units. The CommandError says which, for the model to fix."""
    render(openscad, scad, stl, cfg.render_timeout)
    size = stl_size(stl)
    if max(size) > cfg.max_size_mm:
        raise CommandError(
            f"{title} comes out at {size_speech(size)}, bigger than the printer's "
            f"{cfg.max_size_mm:g} millimetre build volume. Scale it down."
        )
    if max(size) < 1:
        raise CommandError(
            f"{title} comes out under a millimetre across. OpenSCAD units are "
            "millimetres - it may have been drawn in centimetres or metres."
        )
    return size


def _make(ctx: CommandContext, args: dict) -> CommandResult:
    cfg = ctx.config.printer
    title = str(args.get("title") or "").strip() or "The part"
    code = _source(args)

    openscad = find_openscad(cfg.openscad)
    if openscad is None:
        raise CommandError("I can't find OpenSCAD on this PC, so I can't render parts. "
                           "Install it, or set printer.openscad in my config.")

    directory = prints_dir(ctx)
    stem = _new_stem(title, prints_dir(ctx))
    scad, stl = directory / f"{stem}.scad", directory / f"{stem}.stl"
    scad.write_text(code, encoding="utf-8")
    started = time.time()
    try:
        size = _render_checked(openscad, scad, stl, cfg, title)
    except CommandError:
        # Not kept as a failed part: a render error goes back to the model,
        # which fixes its code and calls again - a drafting step, not a part.
        for path in (scad, stl):
            path.unlink(missing_ok=True)
        raise
    # Only written once it rendered, for the same reason.
    Catalog(directory).write(
        stem, title=title, kind="make", state="done", created=started, finished=time.time(),
        stages={"rendering": [started, time.time()]},
        size_mm=[round(v, 2) for v in size], faces=_facet_count(stl),
    )
    _slicing(ctx, stem)
    _prune(directory)
    log.info("made part %s (%s)", stem, size_speech(size))

    result = {"name": stem, "stl": str(stl), "scad": str(scad),
              "size_mm": [round(v, 2) for v in size], "opened": False}
    if not arg_bool(args, "open", True):
        return CommandResult(speech=f"I've made {title}: {size_speech(size)}.", result=result)

    slicer = find_slicer(cfg.slicer)
    if slicer is None:
        return CommandResult(
            speech=f"I've made {title}, {size_speech(size)}, but I can't find Elegoo "
                   "Slicer to open it. It's in the prints folder.",
            result=result,
        )
    _open_in_slicer(slicer, stl)
    result["opened"] = True
    return CommandResult(speech=f"{title} is in the slicer: {size_speech(size)}.", result=result)


# -- printer.sculpt ----------------------------------------------------------

IMAGE_TYPES = (".png", ".jpg", ".jpeg", ".webp")
_sculpt_lock = threading.Lock()
_sculpting: list[str] = []  # the title being worked on, while one is
_replacing: dict[str, str] = {}  # new stem -> the failed part it supersedes
_mouth: Any = None


def _sculpt(ctx: CommandContext, args: dict) -> CommandResult:
    cfg = ctx.config.printer
    if not cfg.sculpt_enabled:
        raise CommandError("Sculpting is switched off. Set printer.sculpt_enabled in my config.")
    # `again`: another go from a part's own picture - after a failure, or for
    # a different take, since each shaping comes out a little differently.
    # Whatever is not given is taken from that part.
    again = str(args.get("again") or "").strip()
    previous: dict[str, Any] = {}
    if again:
        previous = next((p for p in catalog(ctx).list() if p["name"] == again), {})
        if not previous.get("picture"):
            raise CommandError("That part has no picture to sculpt from again.")
        args = {"title": previous.get("title"), "prompt": previous.get("prompt"),
                "height_mm": previous.get("height_mm"), **{k: v for k, v in args.items() if v not in (None, "")},
                "image": str(catalog(ctx).file(again, previous["picture"]))}
    title = str(args.get("title") or "").strip() or "The sculpture"
    prompt = str(args.get("prompt") or "").strip()
    image = _image_arg(args)
    if not prompt and image is None:
        raise CommandError("Tell me what to sculpt, or give me a picture of it.")
    height = _height_arg(args, cfg)
    shown = arg_bool(args, "open", True)

    _claim(title)
    # Named and recorded before any work starts, so whoever asked - the
    # Workshop tab especially - can follow it from the first step.
    stem = _new_stem(title, prints_dir(ctx))
    # A picture of the user's own is one view; one drawn here is whatever
    # the config asks for; another go from a part keeps that part's views.
    views = (previous.get("views") or "front") if again else "front" if image else cfg.sculpt_views
    # A picture of the user's own can be a turnaround sheet too, if they say so.
    given = str(args.get("views") or "").strip()
    if image is not None and not again and given:
        if given not in ("front", "front-back", "front-side-back"):
            raise CommandError("views should be front, front-back or front-side-back.")
        views = given
    catalog(ctx).write(
        stem, title=title, kind="sculpt", mode="sculpt", state="queued", created=time.time(),
        prompt=prompt, height_mm=height, views=views, side=previous.get("side"),
        source=("redrawn" if again else "picture") if image else "drawn",
    )
    if previous.get("state") == "failed":
        # Its picture is copied into the new part as the first step, so the
        # failed one is only removed once that copy exists - see _sculpt_now.
        _replacing[stem] = again
    return _start(
        ctx, stem, title,
        lambda: _sculpt_now(ctx, stem, title, prompt or title, image, height, shown, views),
        f"I'm sculpting {title}. It takes about a minute; I'll say when it's in the slicer.",
        {"height_mm": height},
    )


def _brief(ctx: CommandContext, args: dict) -> CommandResult:
    from .. import brief

    cfg = ctx.config.printer
    if not cfg.sculpt_enabled:
        raise CommandError("Sculpting is switched off. Set printer.sculpt_enabled in my config.")
    request = arg_str(args, "prompt")
    answers = args.get("answers") or []
    if isinstance(answers, str):
        # From the command line, the answers arrive as a JSON string.
        try:
            answers = json.loads(answers)
        except ValueError:
            raise CommandError("answers should be JSON: a list of question and answer pairs.") from None
    if not isinstance(answers, list) or not all(isinstance(a, dict) for a in answers):
        raise CommandError("answers should be a list of question and answer pairs.")
    limit = arg_int(args, "questions", brief.MAX_QUESTIONS, minimum=0, maximum=brief.MAX_QUESTIONS)
    try:
        found = brief.ask(request, answers, model=cfg.sculpt_ask_model, questions=limit)
    except brief.BriefError as exc:
        raise CommandError(str(exc)) from None
    if found.questions:
        speech = "A few questions first. " + " ".join(q["question"] for q in found.questions)
    else:
        speech = f"I'd sculpt {found.description}"
    return CommandResult(speech=speech, result={
        "title": found.title, "description": found.description,
        "height_mm": found.height_mm, "questions": found.questions,
    })


def _claim(title: str) -> None:
    """One AI job at a time in this process: they share an API allowance,
    and the Workshop follows one at a time."""
    with _sculpt_lock:
        if _sculpting:
            raise CommandError(f"I'm still working on {_sculpting[0]}. One at a time.")
        _sculpting.append(title)


def _start(ctx: CommandContext, stem: str, title: str, work: Any, starting: str,
           extra: dict[str, Any]) -> CommandResult:
    from .. import runtime

    if not runtime.IS_RESIDENT:
        # A one-shot exec would exit and take a background thread with it,
        # and whoever ran it is waiting for the answer anyway.
        try:
            return work()
        finally:
            _sculpting.clear()
    threading.Thread(target=_in_background, args=(ctx, stem, title, work), daemon=True,
                     name="sculpt").start()
    return CommandResult(speech=starting,
                         result={"state": "running", "name": stem, "title": title, **extra})


def _image_arg(args: dict) -> Path | None:
    raw = str(args.get("image") or "").strip().strip('"')
    if not raw:
        return None
    path = Path(os.path.expandvars(raw)).expanduser()
    if path.suffix.lower() not in IMAGE_TYPES:
        raise CommandError("The picture needs to be a PNG, JPEG or WebP.")
    if not path.is_file():
        raise CommandError(f"I can't find the picture {path.name}.")
    return path


def _height_arg(args: dict, cfg: Any) -> float:
    raw = args.get("height_mm")
    if raw in (None, ""):
        return float(cfg.sculpt_height_mm)
    try:
        height = float(raw)
    except (TypeError, ValueError):
        raise CommandError(f"height_mm should be a number, but I got {raw!r}.") from None
    if not 5 <= height <= cfg.max_size_mm:
        raise CommandError(f"The height should be between 5 and {cfg.max_size_mm:g} millimetres.")
    return height


def _fail(catalog: Catalog, stem: str, exc: Exception) -> CommandError:
    catalog.write(stem, error=str(exc))
    catalog.stage(stem, "failed")
    return CommandError(str(exc))


def _sculpt_now(ctx: CommandContext, stem: str, title: str, prompt: str, image: Path | None,
                height: float, shown: bool, views: str = "front") -> CommandResult:
    """Picture, then shape. Raises CommandError on any failure."""
    from .. import sculpt

    cfg = ctx.config.printer
    catalog = Catalog(prints_dir(ctx))
    picture = prints_dir(ctx) / f"{stem}.png"
    try:
        if image is not None:
            picture = picture.with_suffix(image.suffix.lower())
            shutil.copyfile(image, picture)
            replaced = _replacing.pop(stem, "")
            if replaced:
                catalog.delete(replaced)
            if catalog.read(stem).get("side") is None:
                _note_side(ctx, catalog, stem, picture, views)
        else:
            catalog.stage(stem, "drawing")
            sculpt.draw(prompt, picture, model=cfg.sculpt_image_model, views=views)
            _note_side(ctx, catalog, stem, picture, views)
    except (sculpt.SculptError, OSError) as exc:
        raise _fail(catalog, stem, exc) from None
    return _shape_now(ctx, stem, title, picture, height, shown, views)


def _note_side(ctx: CommandContext, catalog: Catalog, stem: str, picture: Path, views: str) -> None:
    """Ask which way a three-view sheet's side view faces, and keep the
    answer with the part: shaping it again then needs no second opinion."""
    from .. import sculpt

    if views == sculpt.THREE:
        catalog.write(stem, side=sculpt.side_facing(picture, model=ctx.config.printer.sculpt_check_model))


def local_generator(ctx: CommandContext) -> Any:
    """The local generator's settings; blank paths mean models/hy3d beside
    the config, where the README's setup puts it."""
    from .. import sculpt

    cfg = ctx.config.printer
    base = (ctx.config.source_path.resolve().parent if ctx.config.source_path else Path.cwd()) / "models" / "hy3d"
    python = Path(os.path.expandvars(cfg.sculpt_local_python)).expanduser() if cfg.sculpt_local_python \
        else base / ".venv" / "Scripts" / "python.exe"
    repo = Path(os.path.expandvars(cfg.sculpt_local_repo)).expanduser() if cfg.sculpt_local_repo \
        else base / "Hunyuan3D-2"
    return sculpt.Local(python=python, repo=repo, weights=base / "weights", model=cfg.sculpt_local_model,
                        subfolder=cfg.sculpt_local_subfolder, steps=cfg.sculpt_local_steps)


def _shape_now(ctx: CommandContext, stem: str, label: str, picture: Path, height: float,
               shown: bool, views: str = "front") -> CommandResult:
    """Picture -> mesh -> part -> slicer: the half every sculpture shares."""
    from .. import sculpt

    cfg = ctx.config.printer
    directory = prints_dir(ctx)
    catalog = Catalog(directory)
    raw, stl = directory / f"{stem}.glb", directory / f"{stem}.stl"
    local = local_generator(ctx)
    try:
        catalog.write(stem, shaper=sculpt.where(cfg.sculpt_backend, local))
        catalog.stage(stem, "shaping")
        sculpt.shape(picture, raw, views=views, side=catalog.read(stem).get("side"),
                     backend=cfg.sculpt_backend,
                     local=local, space=cfg.sculpt_space,
                     mv_space=cfg.sculpt_mv_space,
                     token=cfg.sculpt_hf_token or os.environ.get("HF_TOKEN", ""),
                     timeout=cfg.sculpt_timeout)
        catalog.stage(stem, "cleaning")
        part = sculpt.to_part(raw, stl, height_mm=height, max_size_mm=cfg.max_size_mm,
                              max_faces=cfg.sculpt_max_faces)
    except (sculpt.SculptError, OSError) as exc:
        # The picture stays: it cost something to draw, and the Workshop can
        # try the shape again from it.
        for path in (raw, stl):
            path.unlink(missing_ok=True)
        raise _fail(catalog, stem, exc) from None
    return _finish(ctx, stem, label, part.size, part.faces, part.watertight, shown,
                   {"image": str(picture), "mesh": str(raw)})


def _finish(ctx: CommandContext, stem: str, label: str, size: tuple[float, float, float],
            faces: int, watertight: bool | None, shown: bool, extra: dict[str, Any]) -> CommandResult:
    cfg = ctx.config.printer
    directory = prints_dir(ctx)
    catalog = Catalog(directory)
    stl = directory / f"{stem}.stl"
    catalog.write(stem, size_mm=[round(v, 2) for v in size], faces=faces,
                  **({"watertight": watertight} if watertight is not None else {}))
    slicing = _slicing(ctx, stem)
    catalog.stage(stem, "done")
    _prune(directory)
    log.info("finished %s (%s, %d faces)", stem, size_speech(size), faces)

    result = {"name": stem, "stl": str(stl), **extra, "size_mm": [round(v, 2) for v in size],
              "faces": faces, "watertight": watertight, "opened": False,
              "slicing": slicing.get("summary", "")}
    caveat = "" if watertight is not False else " The mesh has gaps, so let the slicer repair it."
    slicer = find_slicer(cfg.slicer) if shown else None
    if slicer is None:
        where = "" if not shown else " I can't find Elegoo Slicer, so it's in the prints folder."
        return CommandResult(speech=f"{label} is done: {size_speech(size)}.{where}{caveat}", result=result)
    _open_in_slicer(slicer, stl)
    result["opened"] = True
    return CommandResult(speech=f"{label} is in the slicer: {size_speech(size)}.{caveat}", result=result)


def _in_background(ctx: CommandContext, stem: str, title: str, work: Any) -> None:
    try:
        speech = work().speech
    except CommandError as exc:
        speech = f"I couldn't finish {title}. {exc}"
    except Exception as exc:  # a bug must not die silently on a thread
        log.exception("working on %s failed", title)
        speech = f"I couldn't finish {title}: {exc}"
        try:
            _fail(Catalog(prints_dir(ctx)), stem, exc)
        except OSError:
            pass
    finally:
        _sculpting.clear()
    _say(ctx, speech)


# -- printer.adjust ----------------------------------------------------------


def _adjust(ctx: CommandContext, args: dict) -> CommandResult:
    """A new version of a part with one change made; the original is kept.

    A sculpture's picture is edited and shaped again; a designed part's
    OpenSCAD is rewritten and rendered again; a height alone is only
    arithmetic and needs neither.
    """
    cfg = ctx.config.printer
    part = _find_part(ctx, str(args.get("name") or ""))
    change = str(args.get("change") or "").strip()
    resizing = args.get("height_mm") not in (None, "")
    shown = arg_bool(args, "open", True)
    if part["kind"] == "make":
        if not change:
            raise CommandError(f"Say what to change about {part['title']} - "
                               "a designed part's size lives in its code.")
        if "scad" not in part["files"]:
            raise CommandError(f"I don't have the code for {part['title']} any more.")
        mode = "code"
    elif change:
        if not cfg.sculpt_enabled:
            raise CommandError("Sculpting is switched off. Set printer.sculpt_enabled in my config.")
        if not part.get("picture"):
            raise CommandError(f"{part['title']} has no picture to change, only a mesh - I can only resize it.")
        mode = "picture"
    elif resizing:
        mode = "resize"
    else:
        raise CommandError("Tell me what to change, or give it a new height.")
    height = (_height_arg(args, cfg) if resizing
              else float((part.get("size_mm") or stl_size(prints_dir(ctx) / f"{part['name']}.stl"))[2]))

    title = part["title"]
    version = int(part.get("version") or 1) + 1
    label = f"{title}, version {version},"
    _claim(title)
    stem = _new_stem(title, prints_dir(ctx))
    catalog(ctx).write(
        stem, title=title, kind=part["kind"], mode=mode, state="queued", created=time.time(),
        prompt=part.get("prompt") or "", height_mm=height if part["kind"] == "sculpt" else None,
        views=part.get("views") or ("front" if part["kind"] == "sculpt" else None),
        source="edited" if mode == "picture" else part.get("source"),
        parent=part["name"], root=part.get("root") or part["name"], version=version,
        change=change or f"resized to {height:g} mm tall",
    )
    if mode == "code":
        work = lambda: _adjust_code_now(ctx, stem, label, part, change, shown)  # noqa: E731
        starting = f"Changing {title}: {change}. I'll say when it's in the slicer."
    elif mode == "picture":
        work = lambda: _adjust_picture_now(ctx, stem, label, part, change, height, shown)  # noqa: E731
        starting = f"Changing {title}: {change}. It takes about a minute; I'll say when it's in the slicer."
    else:
        work = lambda: _resize_now(ctx, stem, label, part, height, shown)  # noqa: E731
        starting = f"Resizing {title} to {height:g} millimetres tall."
    return _start(ctx, stem, title, work, starting, {"parent": part["name"], "version": version})


def _adjust_picture_now(ctx: CommandContext, stem: str, label: str, part: dict[str, Any],
                        change: str, height: float, shown: bool) -> CommandResult:
    from .. import sculpt

    directory = prints_dir(ctx)
    catalog = Catalog(directory)
    picture = directory / f"{stem}.png"
    try:
        catalog.stage(stem, "editing")
        views = part.get("views") or "front"
        sculpt.edit(directory / f"{part['name']}.{part['picture']}", change, picture,
                    model=ctx.config.printer.adjust_image_model, views=views)
        _note_side(ctx, catalog, stem, picture, views)
    except (sculpt.SculptError, OSError) as exc:
        raise _fail(catalog, stem, exc) from None
    return _shape_now(ctx, stem, label, picture, height, shown, views)


def _adjust_code_now(ctx: CommandContext, stem: str, label: str, part: dict[str, Any],
                     change: str, shown: bool) -> CommandResult:
    from .. import adjust

    cfg = ctx.config.printer
    directory = prints_dir(ctx)
    catalog = Catalog(directory)
    scad, stl = directory / f"{stem}.scad", directory / f"{stem}.stl"
    openscad = find_openscad(cfg.openscad)
    if openscad is None:
        raise _fail(catalog, stem, CommandError("I can't find OpenSCAD on this PC."))
    original = (directory / f"{part['name']}.scad").read_text(encoding="utf-8")
    error, attempt = "", ""
    # Three goes: OpenSCAD's complaint about one attempt is what the next
    # one is told, which is usually all it takes.
    for _ in range(3):
        try:
            catalog.stage(stem, "writing")
            attempt = adjust.rewrite(original, change, model=cfg.adjust_code_model,
                                     error=error, previous=attempt)
            scad.write_text(attempt, encoding="utf-8")
            catalog.stage(stem, "rendering")
            size = _render_checked(openscad, scad, stl, cfg, part["title"])
            break
        except adjust.AdjustError as exc:
            raise _fail(catalog, stem, exc) from None
        except CommandError as exc:
            error = str(exc)
    else:
        for path in (scad, stl):
            path.unlink(missing_ok=True)
        raise _fail(catalog, stem, CommandError(f"The changed code wouldn't render: {error}")) from None
    return _finish(ctx, stem, label, size, _facet_count(stl), None, shown, {"scad": str(scad)})


def _resize_now(ctx: CommandContext, stem: str, label: str, part: dict[str, Any],
                height: float, shown: bool) -> CommandResult:
    from .. import sculpt

    cfg = ctx.config.printer
    directory = prints_dir(ctx)
    catalog = Catalog(directory)
    try:
        catalog.stage(stem, "cleaning")
        if part.get("picture"):
            shutil.copyfile(directory / f"{part['name']}.{part['picture']}",
                            directory / f"{stem}.{part['picture']}")
        resized = sculpt.rescale(directory / f"{part['name']}.stl", directory / f"{stem}.stl",
                                 height_mm=height, max_size_mm=cfg.max_size_mm)
    except (sculpt.SculptError, OSError) as exc:
        raise _fail(catalog, stem, exc) from None
    return _finish(ctx, stem, label, resized.size, resized.faces, resized.watertight, shown, {})


def _say(ctx: CommandContext, text: str) -> None:
    """Out loud, from whichever process the job ran in. A voice session's
    context has no speaker of its own, so it borrows one."""
    global _mouth
    mouth = ctx.speech
    if mouth is None:
        if _mouth is None:
            from ..speech import Voice

            _mouth = Voice(ctx.config)
        mouth = _mouth
    try:
        mouth.say(text)
    except Exception as exc:
        log.debug("could not announce the sculpture: %s", exc)


# -- printer.settings ----------------------------------------------------------


def _slicing(ctx: CommandContext, stem: str, *, fresh: bool = False) -> dict[str, Any]:
    """The slicer settings for a part, kept in its record: worked out once,
    and again only when the rules or the filament have changed since. Never
    fails a part - settings are advice, and a part without them is still a
    part."""
    from .. import slicing

    catalog = Catalog(prints_dir(ctx))
    record = catalog.read(stem)
    material = (ctx.config.printer.filament or "PLA").upper()
    kept = record.get("slicing") or {}
    if not fresh and kept.get("v") == slicing.VERSION and kept.get("material") == material:
        return kept
    stl = prints_dir(ctx) / f"{stem}.stl"
    kind = record.get("kind") or ("make" if (prints_dir(ctx) / f"{stem}.scad").is_file() else "sculpt")
    try:
        found = slicing.for_part(stl, kind=kind, material=material, watertight=record.get("watertight"))
    except (OSError, ValueError, struct.error) as exc:
        log.info("no slicer settings for %s: %s", stem, exc)
        return {}
    catalog.write(stem, slicing=found)
    return found


def _settings(ctx: CommandContext, args: dict) -> CommandResult:
    part = _find_part(ctx, str(args.get("name") or ""))
    if part["state"] in WORKING:
        raise CommandError(f"{part['title']} isn't finished yet.")
    found = _slicing(ctx, part["name"])
    if not found:
        raise CommandError(f"I couldn't read the shape of {part['title']}.")
    notes = " ".join(found["notes"])
    return CommandResult(speech=f"For {part['title']}: {found['summary']}" + (f" {notes}" if notes else ""),
                         result={"name": part["name"], "title": part["title"], **found})


# -- the collection ------------------------------------------------------------


def catalog(ctx: CommandContext) -> Catalog:
    return Catalog(prints_dir(ctx))


def _stale_after(ctx: CommandContext) -> float:
    return float(ctx.config.printer.sculpt_timeout) + 300.0


def _parts(ctx: CommandContext, args: dict) -> CommandResult:
    limit = max(1, min(int(args.get("limit") or 20), 200))
    parts = catalog(ctx).list(_stale_after(ctx))
    working = [p for p in parts if p["state"] in WORKING]
    shown = parts[:limit]
    if not parts:
        speech = "I haven't made any parts yet."
    else:
        newest = parts[0]
        speech = f"I've made {len(parts)} part{'s' if len(parts) != 1 else ''}; the newest is {newest['title']}."
        if working:
            speech += f" I'm working on {working[0]['title']} now."
    return CommandResult(speech=speech, result={"parts": shown, "count": len(parts),
                                                "working": len(working)})


def _find_part(ctx: CommandContext, wanted: str) -> dict[str, Any]:
    parts = [p for p in catalog(ctx).list(_stale_after(ctx)) if "stl" in p["files"]]
    if not parts:
        raise CommandError("I haven't made any parts yet.")
    wanted = wanted.strip().lower()
    if not wanted:
        return parts[0]
    for part in parts:
        if part["name"] == wanted:
            return part
    matches = [p for p in parts if wanted in p["title"].lower() or wanted in p["name"]]
    if not matches:
        raise CommandError(f"I haven't made anything called {wanted}.")
    return matches[0]


def _open_part(ctx: CommandContext, args: dict) -> CommandResult:
    part = _find_part(ctx, str(args.get("name") or ""))
    slicer = find_slicer(ctx.config.printer.slicer)
    if slicer is None:
        raise CommandError("I can't find Elegoo Slicer on this PC.")
    _open_in_slicer(slicer, prints_dir(ctx) / f"{part['name']}.stl")
    return CommandResult(speech=f"{part['title']} is in the slicer.",
                         result={"name": part["name"], "opened": True})


def _forget_part(ctx: CommandContext, args: dict) -> CommandResult:
    name = arg_str(args, "name")
    record = next((p for p in catalog(ctx).list(_stale_after(ctx)) if p["name"] == name), None)
    if record is None:
        raise CommandError(f"There's no part called {name}.")
    if record["state"] in WORKING:
        raise CommandError(f"{record['title']} is still being made.")
    gone = catalog(ctx).delete(name)
    return CommandResult(speech=f"I've deleted {record['title']}.", result={"name": name, "files": gone})
