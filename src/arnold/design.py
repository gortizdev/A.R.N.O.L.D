"""Designing a working part from a description: OpenSCAD built on tested mechanisms.

Sculpting is a picture wrapped in a skin. Hunyuan3D sees the outside of an
object and reconstructs that outside; it has no idea that a pouch is a box
with a floor and four walls, that a flap turns on a pin, or that a belt loop
is a gap a belt has to pass through. Asked for any of those, it draws the
look of them - a shallow dent where the cavity goes, a flap fused to the body
- and the part does not work. Anything that has to open, hold, fit or attach
is geometry with clearances, and that is what OpenSCAD is for.

printer.make covers this when a conversation writes the code itself. This is
the same thing for the places with no conversation behind them - the
Workshop's box, and printer.adjust turning a sculpture that turned out to
need a working part into a designed version. A reasoning model writes the
file; OpenSCAD renders it; a render error goes back for another go, exactly
as adjust.py does for edits. A concept picture, when there is one, is shown
to the model so the design keeps the look that was asked for.

The model does not invent mechanisms. Left to draw a hinge from nothing it
gets the knuckle spacing or the clearances subtly wrong, and nothing notices
until the print. So it chooses from scad/arnold_parts.scad - a pouch, a
hinged box, a lidded box, a tray - whose geometry the test suite checks, and
its job is the numbers and the decoration. check.py then looks at what came
out before anyone prints it.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

from .adjust import unfence

log = logging.getLogger(__name__)

CHAT_URL = "https://api.openai.com/v1/chat/completions"

# The mechanisms the model builds on, copied beside every part that includes
# it (commands/printer.py render()), so the file also opens in OpenSCAD itself.
LIBRARY = Path(__file__).with_name("scad") / "arnold_parts.scad"

INSTRUCTIONS = (
    "You are a mechanical designer writing OpenSCAD 2021.01 for an FDM 3D printer "
    "(0.4 mm nozzle, PLA). You design parts that WORK, not models that look like them.\n\n"
    "Start the file with `include <arnold_parts.scad>`. It holds TESTED mechanisms - "
    "use them rather than inventing your own, because a hinge or a fit drawn from "
    "nothing is where designs fail:\n"
    "- pouch(size=[w,d,h], belt=40, belt_t=4, flap=0, wall=2, corner=4, loop_w=0) - a "
    "belt pouch: hollow body, lid on a filament-pin knuckle hinge along the top back, a "
    "flap folding down the front (flap=0 = 35% of h) with a snap ridge, and a loop on "
    "the back for a belt `belt` wide (0 = none). The box must be tall enough for the "
    "loop: h >= belt + 22 or so.\n"
    "- hinged_box(size=[w,d,h], wall=2, floor=2, lid=3, corner=3, flap=0, catch=true, "
    "knuckles=5, belt=0, belt_t=4, loop_w=20, loop_top=0) - the general hinged box the "
    "pouch is made from.\n"
    "- lidded_box(size=[w,d,h], wall=2, floor=2, lid=2.4, corner=3, lip=5, clearance=0.25) "
    "- a box with a press-on lid.\n"
    "- tray(size=[w,d,h], rows=2, cols=3, wall=1.6, floor=1.6, corner=3, divider=1.2) - "
    "an open organiser tray.\n"
    "- rounded_box(size, corner) - a solid block with rounded vertical edges, x "
    "centred, y from 0 to d, z from 0 to h.\n"
    "- relief(h) <2D> - raises 2D shapes or text by h; keyring_loop(hole, band, t); "
    "magnet_pocket(d, h) and screw_hole(d, head, depth) to difference() away.\n"
    "- emblem(file, w, h=0.8) - a traced picture, w wide and centred, raised by h: a "
    "child like relief(), and only with an emblem file you are given.\n"
    "size is the body's outside. Every template lays its pieces out flat for printing "
    "(lids upside down beside the box) - do not move or rotate them.\n\n"
    "Decoration goes in as CHILDREN, each placed on a face for you: local x across the "
    "face, local y up it (towards the back on a top face), local z out of it. For "
    "pouch/hinged_box: child 0 = the lid's top (engraved in, since the lid prints face "
    "down), child 1 = the front (the flap's face when there is one), child 2 = the box's "
    "front below the flap. lidded_box: 0 = lid top (engraved), 1 = front. tray: 0 = "
    "front. Pass union() {} to skip one. Build each from relief(1) with 2D shapes or "
    "text centred on the origin (halign=\"center\", valign=\"center\"), sized to fit "
    "the face with a few mm to spare. Example:\n"
    "include <arnold_parts.scad>\n"
    "pouch([40, 25, 60], belt=40) {\n"
    "  relief(1) text(\"X\", 14, halign=\"center\", valign=\"center\");\n"
    "  relief(1) difference() { circle(7); circle(5); }\n"
    "}\n\n"
    "Only when no template fits, write the part yourself: millimetres, on z=0, centred "
    "in X and Y, within {max_mm:g} mm on every side; named variables for every key "
    "dimension; containers genuinely hollow with walls and floor at least 2 mm; 0.2-0.3 "
    "mm clearance where things fit; overhangs no steeper than 45 degrees; the helpers "
    "above are still yours to use. Nothing newer than OpenSCAD 2021.01 and no other "
    "libraries.\n\n"
    "Keep the silhouette and character of any concept picture you are shown through "
    "the size, the proportions and the decoration - the picture guides the look, the "
    "templates make it work. Choose sizes from the request, the picture and any earlier "
    "size given.\n"
    "Reply with the complete OpenSCAD file only: no markdown fences, no commentary "
    "outside // comments."
)

# Words that mean an object has a job to do, rather than a shape to have. Used
# to catch a sculpture asked for something only a design can give it; the
# models get the same guidance in words, this is the backstop.
_FUNCTIONAL = re.compile(
    r"\b(hollow|hinge[sd]?|hinging|lid|flap|clasp|latch|snap[s-]?fit|snaps? (?:shut|closed|on)|"
    r"press[- ]fit|fits? (?:onto|over|into|around)|clearance|compartments?|storage|container|"
    r"pouch|holster|sheath|drawer|tray|box for|case for|holder|enclosure|organi[sz]er|"
    r"bracket|clip|belt loop|loop for|wall thickness|walls? \d|screw holes?|usable|"
    r"functional|actually work)\b",
    re.I,
)


TEMPLATES = ("pouch", "hinged_box", "lidded_box", "tray")

# What each template already guarantees, for whoever reviews a part built on
# it: these are tested, so a reviewer should not report them missing because
# a render is too small to show a 0.8 mm ridge or a pin that is not printed.
GUARANTEES = {
    "pouch": "a knuckle hinge whose pin is a length of 1.75 mm filament pushed through the "
             "knuckles after printing (so no printed pin is correct), a 0.8 mm snap ridge on the "
             "box front that a groove inside the flap clicks over, and an open belt loop on the "
             "back whose slot is the belt width plus 2 mm by the belt thickness plus 2 mm; the "
             "lid prints upside down beside the box",
    "hinged_box": "a knuckle hinge whose pin is a length of 1.75 mm filament pushed through the "
                  "knuckles after printing (so no printed pin is correct), a 0.3 mm gap all round "
                  "the lid, and a snap ridge when it has a flap; the lid prints upside down beside the box",
    "lidded_box": "a press-fit lid whose lip fits inside the walls with 0.25 mm clearance; the lid "
                  "prints upside down beside the box",
    "tray": "compartments divided by walls",
}


# The arguments that make each template work rather than look: a restyle
# must leave every one as it was (kept_mechanism).
MECHANISM = {
    "pouch": ("belt", "belt_t", "flap", "wall", "loop_w"),
    "hinged_box": ("wall", "floor", "lid", "flap", "catch", "knuckles", "belt", "belt_t",
                   "loop_w", "loop_top"),
    "lidded_box": ("wall", "floor", "lid", "lip", "clearance"),
    "tray": ("rows", "cols", "wall", "floor", "divider"),
}

# A change about how a part looks as a whole, which is the design model's
# work (a restyle) rather than a quick edit of the code.
_LOOKS = re.compile(r"\b(looks? like|look of|style[ds]?|stylish|themed?|in the shape of|shaped like|"
                    r"decorat\w*|ornate|engrav\w*|embellish\w*)\b", re.I)


def looks_change(text: str) -> bool:
    """Whether a change is about the look of a part as a whole."""
    return bool(_LOOKS.search(text or ""))


def template_used(code: str) -> str:
    """The parts-library template a file builds on, or ''."""
    if LIBRARY.name not in (code or ""):
        return ""
    for name in TEMPLATES:
        if re.search(rf"(?<![\w.]){name}\s*\(", code):
            return name
    return ""


def looks_functional(text: str) -> bool:
    """Whether a request or a change is about what a part does."""
    return bool(_FUNCTIONAL.search(text or ""))


def _template_args(code: str, name: str) -> dict[str, str] | None:
    """The named arguments of the file's call to a template, with top-level
    `x = 3;` variables put in, or None when there is no call."""
    found = re.search(rf"(?<![\w.]){name}\s*\(", code)
    if not found:
        return None
    depth, start = 0, found.end()
    for end in range(start, len(code)):
        if code[end] in "([{":
            depth += 1
        elif code[end] in ")]}":
            if depth == 0:
                break
            depth -= 1
    else:
        return None
    inside, pieces, depth, last = code[start:end], [], 0, 0
    for i, ch in enumerate(inside):
        depth += ch in "([{"
        depth -= ch in ")]}"
        if ch == "," and depth == 0:
            pieces.append(inside[last:i])
            last = i + 1
    pieces.append(inside[last:])
    variables = dict(re.findall(r"(?m)^\s*([A-Za-z_]\w*)\s*=\s*([^;\n]+?)\s*;", code))
    args: dict[str, str] = {}
    for piece in pieces:
        key, eq, value = piece.partition("=")
        if eq and re.fullmatch(r"\s*[A-Za-z_]\w*\s*", key):
            value = value.strip()
            args[key.strip()] = variables.get(value, value).strip()
    return args


def _same(a: str | None, b: str | None) -> bool:
    try:
        return float(a) == float(b)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return (a or "").replace(" ", "") == (b or "").replace(" ", "")


def kept_mechanism(before: str, after: str) -> str:
    """'' when `after` works the way `before` did - the same template, every
    mechanical argument unchanged - else what changed, for the model to fix.
    A file built on no template has nothing here to hold it to."""
    template = template_used(before)
    if not template:
        return ""
    if template_used(after) != template:
        return (f"It has to stay built on {template}() from arnold_parts.scad - the look can "
                "change, the mechanism cannot.")
    old, new = _template_args(before, template) or {}, _template_args(after, template) or {}
    moved = [k for k in MECHANISM.get(template, ()) if not _same(old.get(k), new.get(k))]
    if moved:
        was = ", ".join(f"{k}={old[k]}" if k in old else f"{k} left at its default" for k in moved)
        return (f"Those arguments set how it works, and a change of look must not touch them: "
                f"put back {was}.")
    return ""


class DesignError(Exception):
    """The design could not be had. The message is spoken."""


def _picture_url(picture: Path) -> str:
    from PIL import Image

    image = Image.open(picture).convert("RGB")
    image.thumbnail((1536, 1536))
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=88)
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode()


def brief_text(description: str, *, change: str = "", size_mm: list[float] | None = None,
               base: str = "", emblem: tuple[str, float] | None = None) -> str:
    if base:
        # A restyle: the part exists and works; only its look is changing.
        template = template_used(base)
        held = (f"the same {template}() call with the same values for "
                f"{', '.join(MECHANISM[template])}" if template in MECHANISM
                else "every opening, cavity, fit and clearance as it is")
        lines = [f"Restyle this part: {description.strip()}",
                 f"The new look: {change.strip()}" if change else "",
                 f"Keep its mechanism exactly - {held} - and its size unless the new look "
                 "asks for another. Change only the look: the decoration children, the "
                 "corner rounding, text and emblems. Keep every other line as it is.",
                 f"Current file:\n{base}"]
    else:
        lines = [f"Design this part: {description.strip()}",
                 f"It must also: {change.strip()}" if change else ""]
    if emblem:
        name, aspect = emblem
        lines.append(f'The emblem file is "{name}": emblem("{name}", w). It comes out {aspect:.2f} '
                     "times as tall as it is wide - choose w so it fits its face with a few mm to "
                     "spare. Put it where the request says, else on the most visible face (the "
                     "flap or the front); it replaces any emblem the file had.")
    if size_mm and len(size_mm) == 3 and not base:
        x, y, z = (float(v) for v in size_mm)
        lines.append(f"The earlier version was about {x:.0f} x {y:.0f} x {z:.0f} mm "
                     "(width x depth x height); keep it close to that unless asked otherwise.")
    return "\n".join(line for line in lines if line)


def write(description: str, *, model: str, fallback_model: str = "", change: str = "",
          size_mm: list[float] | None = None, picture: Path | None = None,
          max_mm: float = 256.0, error: str = "", previous: str = "", base: str = "",
          emblem: tuple[str, float] | None = None, timeout: float = 540.0) -> str:
    """The OpenSCAD for the part. `error` and `previous` are the last attempt
    and what OpenSCAD said about it, when there was one. `base` is the file
    of a working part to restyle rather than design afresh; `emblem` a traced
    picture beside the part, with its height over width. A model this key
    cannot use falls back to `fallback_model`."""
    key = os.environ.get("OPENAI_API_KEY", "")
    if not key:
        raise DesignError("I need an OpenAI key to design parts, and OPENAI_API_KEY isn't set.")
    content: list[dict] = [{"type": "text", "text": brief_text(description, change=change, size_mm=size_mm,
                                                               base=base, emblem=emblem)}]
    if picture is not None:
        try:
            content.append({"type": "image_url", "image_url": {"url": _picture_url(picture), "detail": "high"}})
            content[0]["text"] += ("\nThe picture is the concept it came from: views of the same "
                                   "object. Match its look; make the function real.")
        except Exception:  # a design without the picture is still a design
            pass
    messages: list[dict] = [
        {"role": "system", "content": INSTRUCTIONS.replace("{max_mm:g}", f"{max_mm:g}")},
        {"role": "user", "content": content},
    ]
    if error:
        # `error` says what was wrong: OpenSCAD's complaint, or what checking
        # the rendered part found, or a mechanism a restyle moved
        # (commands/printer.py words all three).
        messages += [
            {"role": "assistant", "content": previous},
            {"role": "user", "content": f"{error}\nFix it and reply with the whole file again."},
        ]
    tried: list[str] = []
    for name in [m for m in (model, fallback_model) if m]:
        if name in tried:
            continue
        tried.append(name)
        body: dict = {"model": name, "messages": messages}
        if name.startswith(("gpt-5", "o")):
            body["reasoning_effort"] = "medium"
        request = urllib.request.Request(
            CHAT_URL, data=json.dumps(body).encode(), method="POST",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            text = payload["choices"][0]["message"]["content"] or ""
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read().decode("utf-8"))["error"]["message"]
            except Exception:
                detail = f"error {exc.code}"
            if exc.code in (400, 403, 404) and "model" in detail.lower():
                continue  # this key cannot use that model: try the next
            raise DesignError(f"The design model turned that down: {detail}") from None
        except TimeoutError:
            # Reasoning through a hinge can outlast the wait; the quicker
            # model is a better answer than none.
            log.warning("design model %s timed out after %.0fs", name, timeout)
            continue
        except (urllib.error.URLError, OSError, KeyError, IndexError, ValueError) as exc:
            if isinstance(getattr(exc, "reason", None), TimeoutError):
                log.warning("design model %s timed out after %.0fs", name, timeout)
                continue
            raise DesignError(f"I couldn't get the part designed: {exc}") from None
        code = unfence(text)
        if not code.strip():
            raise DesignError("The design model sent back nothing.")
        return code
    raise DesignError(f"None of the design models would answer in time ({', '.join(tried)}).")
