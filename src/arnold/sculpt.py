"""Sculpted parts: a description in, a printable STL out.

printer.make is for parts with dimensions; this is for things with a shape -
a dragon, a bust, a planter shaped like a frog. Three steps, each a service
that does one thing well:

1. **A picture.** OpenAI's image model draws the subject as a grey clay
   figurine on white - one object, whole, evenly lit - because that is the
   picture an image-to-3D model reconstructs best. It draws it three times on
   one sheet - front, side, back - because one picture of the front leaves
   the rest to be guessed: the back is where a tail goes missing, and the side
   is the only view that shows depth, like how far a belt loop stands off a
   pouch. Drawing them on one sheet is what keeps them the same object. The
   fourth side is left out on purpose: it is nearly always the mirror of the
   third, and asked for, image models tend to draw it inconsistently - a view
   that contradicts the others is worse than no view. A photo or screenshot
   the user already has can stand in for this step.
2. **A shape.** Hunyuan3D-2mv turns the views into an untextured mesh -
   on this PC's own GPU when it is set up (models/hy3d, run by
   hy3d_worker.py), otherwise on a Hugging Face Space. Untextured is the
   point: a printer has no use for colour, and shape-only is the fast half of
   the model.
3. **A part.** The raw mesh is not printable as it stands. It arrives Y-up,
   at an arbitrary scale, with roughly 40% of its triangles degenerate
   padding (which is what makes it look non-watertight), and at around a
   million faces. It is cleaned, decimated, turned Z-up, scaled to the height
   asked for and set down on z=0.

Nothing here talks to the printer. The slicer is where a person decides.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import subprocess
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

IMAGE_URL = "https://api.openai.com/v1/images/generations"

# The subject goes in the middle. Everything around it steers towards a
# picture that reconstructs well and prints well: one object, whole, matte,
# no background to mistake for geometry, chunky rather than spindly.
SHEET_PROMPT = (
    "A turnaround reference sheet of one object: {subject}, as a matte grey clay "
    "3D-printed figurine. Exactly two views side by side at the same scale and the "
    "same height: on the LEFT half the FRONT view, looking straight at it; on the "
    "RIGHT half the BACK view of the same figurine, seen from directly behind. "
    "Orthographic, eye level, each view whole and centred in its half with a wide "
    "empty margin all round - no view may touch or run off the edge of the picture, "
    "so draw a long object small enough to fit - sturdy proportions with no very "
    "thin parts, resting on a flat bottom, plain white background, soft even studio "
    "lighting, no shadows, no text, no labels, no dividing line. Every view shows "
    "exactly the same object, with or without the same base in all of them."
)
THREE_PROMPT = (
    "A turnaround reference sheet of one object: {subject}, as a matte grey clay "
    "3D-printed model. Exactly three views in one row, all at the same scale and the "
    "same height, orthographic, eye level: on the LEFT the FRONT view, looking "
    "straight at its front; in the MIDDLE the SIDE view in exact profile, turned 90 "
    "degrees; on the RIGHT the BACK view, seen from directly behind. Each view whole "
    "and centred in its third with a wide empty margin all round - no view may touch "
    "or run off the edge of the picture, so draw a long object small enough to fit - "
    "sturdy proportions with no very thin parts, resting on a flat bottom, plain "
    "white background, soft even studio lighting, no shadows, no text, no labels, no "
    "dividing lines. Every view shows exactly the same object, with or without the "
    "same base in all of them."
)
# Four views, one in each corner: both sides, so an object that is not the
# same on its left and right - a sword in one hand, a wheel arch cut away -
# is seen whole, and a long object's profiles get the wide cells they need.
FOUR_PROMPT = (
    "A turnaround reference sheet of one object: {subject}, as a matte grey clay "
    "3D-printed model. Exactly four views of the same object in a two-by-two grid, all "
    "at the same scale, orthographic, eye level: TOP LEFT the FRONT view, looking "
    "straight at its front; TOP RIGHT the BACK view, seen from directly behind; BOTTOM "
    "LEFT and BOTTOM RIGHT the two SIDE views in exact profile, one from its left side "
    "and one from its right side. Each view whole and centred in its quarter with a "
    "wide empty margin all round - no view may touch the edge of the picture or cross "
    "into another quarter, so draw a long object small enough to fit - sturdy "
    "proportions with no very thin parts, resting on a flat bottom, plain white "
    "background, soft even studio lighting, no shadows, no text, no labels, no "
    "dividing lines. Every view shows exactly the same object, with or without the "
    "same base in all of them."
)
# One view, for when views is "front": cheaper, and what older parts used.
IMAGE_PROMPT = (
    "{subject}. A single object shown as a matte grey clay 3D-printed figurine: "
    "full body, the whole object in frame and centred, three-quarter view, "
    "sturdy proportions with no very thin parts, resting on a flat bottom, "
    "plain white background, soft even studio lighting, no shadow, no text."
)


class SculptError(Exception):
    """A step failed. The message is spoken, so it is a sentence."""


@dataclass(slots=True)
class Part:
    stl: Path
    size: tuple[float, float, float]
    faces: int
    watertight: bool


# -- 1. a picture ------------------------------------------------------------


SHEET = "front-back"
THREE = "front-side-back"
FOUR = "front-back-sides"
# The order the views sit in on a sheet: left to right, row by row.
LAYOUTS = {SHEET: ("front", "back"), THREE: ("front", "side", "back"),
           FOUR: ("front", "back", "side", "side2")}
# Rows and columns; a layout not here is one row.
GRIDS = {FOUR: (2, 2)}


def grid(views: str) -> tuple[int, int]:
    return GRIDS.get(views, (1, len(LAYOUTS.get(views, ("front",)))))


def draw(subject: str, out: Path, *, model: str, views: str = SHEET, timeout: float = 180.0) -> Path:
    """The subject as a clay figurine, written to `out` as PNG: a front-and-
    back sheet, or with views="front" a single three-quarter view."""
    key = os.environ.get("OPENAI_API_KEY", "")
    if not key:
        raise SculptError("I need an OpenAI key to draw the reference picture, and OPENAI_API_KEY isn't set.")
    prompt = {SHEET: SHEET_PROMPT, THREE: THREE_PROMPT, FOUR: FOUR_PROMPT}.get(views, IMAGE_PROMPT)
    body = json.dumps({
        "model": model,
        "prompt": prompt.format(subject=subject.strip().rstrip(".")),
        "size": "1536x1024" if views in LAYOUTS else "1024x1024",
        "quality": "medium",
    }).encode()
    request = urllib.request.Request(
        IMAGE_URL, data=body, method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        data = base64.b64decode(payload["data"][0]["b64_json"])
    except urllib.error.HTTPError as exc:
        detail = _openai_error(exc)
        raise SculptError(f"The image model turned that down: {detail}") from None
    except (urllib.error.URLError, OSError, KeyError, IndexError, ValueError) as exc:
        raise SculptError(f"I couldn't get a picture drawn: {exc}") from None
    out.write_bytes(data)
    return out


EDIT_URL = "https://api.openai.com/v1/images/edits"

# The change goes in the middle; the rest holds on to everything the user
# did not ask to change, which is most of what makes an edit "slight".
EDIT_PROMPT = (
    "{change}. Change only that. Keep everything else exactly as it is: the same "
    "subject, pose, proportions, details and viewing angle, the same matte grey "
    "clay, one object whole and centred in frame, plain white background, soft "
    "even lighting, no shadow, no text."
)


# A sheet has the object twice; a change made to one view and not the other
# would give the shape model two different objects to reconcile.
SHEET_EDIT = {
    SHEET: (" The picture shows the same object twice - the front view on the left and "
            "the back view on the right. Make the change on both views, consistently, and "
            "keep them side by side at the same scale."),
    THREE: (" The picture shows the same object three times - the front view on the "
            "left, the side view in the middle and the back view on the right. Make the "
            "change on every view, consistently, and keep all three in one row at the "
            "same scale."),
    FOUR: (" The picture shows the same object four times in a two-by-two grid - the "
           "front view top left, the back view top right and its two side views along the "
           "bottom. Make the change on every view, consistently, and keep all four in "
           "their corners at the same scale."),
}


def edit(picture: Path, change: str, out: Path, *, model: str, views: str = "front",
         timeout: float = 240.0) -> Path:
    """The same picture with one change made, written to `out` as PNG.

    gpt-image-1 holds on to its input so hard that a real change - "move the
    belt loop", "make it industrial" - comes back as the same picture, even
    with its input_fidelity setting turned down; gpt-image-1.5 makes the change
    and keeps the rest, which is the point. The setting is only sent to the
    gpt-image-1 family, which alone takes it.
    """
    key = os.environ.get("OPENAI_API_KEY", "")
    if not key:
        raise SculptError("I need an OpenAI key to edit the picture, and OPENAI_API_KEY isn't set.")
    fields = {
        "model": model,
        "prompt": EDIT_PROMPT.format(change=change.strip().rstrip(".")) + SHEET_EDIT.get(views, ""),
        "size": "1536x1024" if views in LAYOUTS else "1024x1024",
        "quality": "medium",
    }
    if model in ("gpt-image-1", "gpt-image-1-mini"):
        fields["input_fidelity"] = "high"
    try:
        data = _post_edit(key, fields, picture, timeout)
    except urllib.error.HTTPError as exc:
        detail = _openai_error(exc)
        if "input_fidelity" not in detail:
            raise SculptError(f"The image model wouldn't make that change: {detail}") from None
        fields.pop("input_fidelity")
        try:
            data = _post_edit(key, fields, picture, timeout)
        except urllib.error.HTTPError as again:
            raise SculptError(f"The image model wouldn't make that change: {_openai_error(again)}") from None
    except (urllib.error.URLError, OSError, KeyError, IndexError, ValueError) as exc:
        raise SculptError(f"I couldn't get the picture edited: {exc}") from None
    out.write_bytes(data)
    return out


def _post_edit(key: str, fields: dict[str, str], picture: Path, timeout: float,
               mask: bytes | None = None) -> bytes:
    """One call to the edit endpoint. `mask` is a PNG the picture's size whose
    transparent pixels are where the model may paint."""
    boundary = uuid.uuid4().hex
    parts = [
        f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        for name, value in fields.items()
    ]
    kind = {".jpg": "jpeg", ".jpeg": "jpeg", ".webp": "webp"}.get(picture.suffix.lower(), "png")
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="image[]"; '
        f'filename="picture{picture.suffix.lower()}"\r\nContent-Type: image/{kind}\r\n\r\n'.encode()
        + picture.read_bytes() + b"\r\n"
    )
    if mask is not None:
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="mask"; '
            f'filename="mask.png"\r\nContent-Type: image/png\r\n\r\n'.encode() + mask + b"\r\n"
        )
    parts.append(f"--{boundary}--\r\n".encode())
    request = urllib.request.Request(
        EDIT_URL, data=b"".join(parts), method="POST",
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return base64.b64decode(payload["data"][0]["b64_json"])


def _openai_error(exc: urllib.error.HTTPError) -> str:
    try:
        return json.loads(exc.read().decode("utf-8"))["error"]["message"]
    except Exception:
        return f"error {exc.code}"


def split_views(sheet: Path, out_dir: Path, views: str = SHEET) -> dict[str, Path]:
    """A turnaround sheet -> {"front": png, "side": png, "back": png} (or
    whichever views its layout has), in the order LAYOUTS gives.

    Each cut goes along the emptiest line near where it is expected rather
    than exactly there, so a wing that strays over is not sliced off - rows
    first on a grid, then the columns within each row. Each view is padded
    out to a square on white without being rescaled: the multi-view model
    reads them as one object seen from several sides, and that only holds if
    they are all at the same scale.
    """
    import numpy as np
    from PIL import Image, ImageOps

    names = LAYOUTS.get(views, LAYOUTS[SHEET])
    rows, cols = grid(views) if views in LAYOUTS else (1, len(names))
    image = Image.open(sheet).convert("RGB")
    width, height = image.size
    ink = 255 - np.asarray(image.convert("L"), dtype=np.int64)

    def cuts(profile, length: int, n: int) -> list[int]:
        found = [0]
        for k in range(1, n):
            expected = length * k // n
            lo, hi = expected - length // (n * 4), expected + length // (n * 4)
            found.append(min(range(lo, hi), key=lambda x: (profile[x], abs(x - expected))))
        return found + [length]

    across = cuts(ink.sum(axis=1), height, rows)
    cells = []
    for top, bottom in zip(across, across[1:]):
        down = cuts(ink[top:bottom].sum(axis=0), width, cols)
        cells += [(left, top, right, bottom) for left, right in zip(down, down[1:])]

    out_dir.mkdir(parents=True, exist_ok=True)
    found = {}
    for name, box in zip(names, cells):
        part = image.crop(box)
        # Centre the object in its square: shift by where its ink sits.
        mask = ImageOps.invert(part.convert("L")).point(lambda v: 255 if v > 24 else 0)
        bounds = mask.getbbox() or (0, 0, part.width, part.height)
        side = max(part.height, part.width)
        square = Image.new("RGB", (side, side), (255, 255, 255))
        centre = (bounds[0] + bounds[2]) // 2
        square.paste(part, (side // 2 - centre, (side - part.height) // 2))
        found[name] = out_dir / f"{name}.png"
        square.save(found[name])
    return found


def cut_off(sheet: Path, views: str = SHEET) -> list[str]:
    """The views of a sheet whose object runs off the edge of the picture.

    The drawing is asked for every view whole and does not always oblige: a
    long object - a car, a sword - gets its front view's wheel sliced off by
    the frame. A view like that tells the shape model the object ends in a
    straight cut, and it builds one. Only the sheet's own edges count; the
    cuts between views are split_views' business.
    """
    import numpy as np
    from PIL import Image

    names = LAYOUTS.get(views)
    if not names:
        return []
    try:
        grey = np.asarray(Image.open(sheet).convert("L"), dtype=np.int16)
    except OSError:
        return []
    height, width = grey.shape
    border = np.concatenate([grey[0], grey[-1], grey[:, 0], grey[:, -1]])
    ink = grey < np.median(border) - 50  # the clay, well below any backdrop
    rows, cols = grid(views)
    cut = []
    for i, name in enumerate(names):
        r, c = divmod(i, cols)
        top, bottom = height * r // rows, height * (r + 1) // rows
        left, right = width * c // cols, width * (c + 1) // cols
        # Only the sheet's own edges this view's cell lies against.
        edges = []
        if r == 0:
            edges.append(ink[0, left:right])
        if r == rows - 1:
            edges.append(ink[-1, left:right])
        if c == 0:
            edges.append(ink[top:bottom, 0])
        if c == cols - 1:
            edges.append(ink[top:bottom, -1])
        if any(edge.sum() > max(4, 0.01 * len(edge)) for edge in edges):
            cut.append(name)
    return cut


UNCROP_PROMPT = (
    "This turnaround reference sheet was drawn with {which} cut off by the edge of the "
    "frame. It has been shrunk onto a larger canvas: paint the empty border so that every "
    "view is whole - finish the cut-off part of the object exactly as the rest of it looks, "
    "and carry on the plain background. Change nothing else: the same views in the same "
    "places at the same scale, the same matte grey clay and lighting. No text, no labels, "
    "no dividing lines."
)


def uncrop(sheet: Path, views: str, *, model: str, cut: list[str] | None = None,
           shrink: float = 0.8, timeout: float = 240.0) -> list[str]:
    """Mend a sheet whose views run off its edge, in place: shrunk onto a
    bigger canvas, with the border painted in by the image model so the cut
    views are finished. Keeps the views that were fine, which drawing it all
    again would not. Returns the views still cut off afterwards; the sheet is
    only replaced when the mending made it better."""
    import io

    import numpy as np
    from PIL import Image

    names = LAYOUTS.get(views)
    cut = cut_off(sheet, views) if cut is None else cut
    if not names or not cut:
        return []
    key = os.environ.get("OPENAI_API_KEY", "")
    if not key:
        raise SculptError("I need an OpenAI key to mend the picture, and OPENAI_API_KEY isn't set.")
    image = Image.open(sheet).convert("RGB")
    width, height = image.size
    pixels = np.asarray(image)
    border = np.concatenate([pixels[0], pixels[-1], pixels[:, 0], pixels[:, -1]])
    backdrop = tuple(int(v) for v in np.median(border, axis=0))
    small = image.resize((round(width * shrink), round(height * shrink)), Image.LANCZOS)
    x0, y0 = (width - small.width) // 2, (height - small.height) // 2
    canvas = Image.new("RGB", (width, height), backdrop)
    canvas.paste(small, (x0, y0))
    # Opaque where the old picture stays; the border, and a few pixels into
    # the old frame so the join is painted over too, are the model's.
    mask = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    inset = 8
    mask.paste((0, 0, 0, 255), (x0 + inset, y0 + inset, x0 + small.width - inset, y0 + small.height - inset))
    buffer = io.BytesIO()
    mask.save(buffer, "PNG")
    rows, cols = grid(views)

    def place(name: str) -> str:
        r, c = divmod(names.index(name), cols)
        if rows > 1:
            return f"the view in the {('top', 'bottom')[r]} {('left', 'right')[c]} corner"
        return "the view on the left" if c == 0 else "the view on the right" if c == cols - 1 \
            else f"the {name} view"

    which = " and ".join(place(n) for n in cut)
    fields = {"model": model, "prompt": UNCROP_PROMPT.format(which=which),
              "size": f"{width}x{height}", "quality": "medium"}
    if model in ("gpt-image-1", "gpt-image-1-mini"):
        fields["input_fidelity"] = "high"
    import tempfile

    with tempfile.TemporaryDirectory(prefix="arnold-uncrop-") as work:
        padded = Path(work) / "sheet.png"
        canvas.save(padded)
        try:
            data = _post_edit(key, fields, padded, timeout, mask=buffer.getvalue())
        except urllib.error.HTTPError as exc:
            raise SculptError(f"The image model wouldn't mend the picture: {_openai_error(exc)}") from None
        except (urllib.error.URLError, OSError, KeyError, IndexError, ValueError) as exc:
            raise SculptError(f"I couldn't get the picture mended: {exc}") from None
        mended = Path(work) / "mended.png"
        mended.write_bytes(data)
        left = cut_off(mended, views)
        if len(left) >= len(cut):
            log.info("mending %s left %s still cut off; keeping the original", sheet.name, left)
            return cut
        with Image.open(mended) as fixed:
            fixed.convert("RGB").resize((width, height)).save(sheet)
    return left


CHAT_URL = "https://api.openai.com/v1/chat/completions"
FACING_QUESTION = (
    "This reference sheet shows one object three times: the FRONT view on the left, a "
    "SIDE view in the middle, the BACK view on the right. In the middle side view, "
    "which edge of the picture does the object's front (the side seen in the "
    "left-hand view) face: left or right? "
    'Reply with JSON only: {"front_faces": "left" | "right" | "unsure"}'
)
FACINGS_QUESTION = (
    "This reference sheet shows one object four times: the FRONT view top left, the "
    "BACK view top right, and two SIDE views along the bottom. In each bottom side "
    "view, which edge of the picture does the object's front (the side seen in the "
    "top-left view) face: left or right? "
    'Reply with JSON only: {"bottom_left": "left" | "right" | "unsure", '
    '"bottom_right": "left" | "right" | "unsure"}'
)


def side_facing(sheet: Path, *, model: str, views: str = THREE,
                timeout: float = 60.0) -> str | list[str | None] | None:
    """Which way a sheet's side view faces: "left" or "right" - or, for a
    four-view sheet, a list with the answer for each bottom view.

    Asked, not assumed. The drawing is told which way to turn it and does
    not reliably listen, and the shape model has to know: a side view
    labelled the wrong way round puts the belt loop on the front. The answer
    is also the side's name to the shape model - an object whose front
    points left is seen from its own left. None when there is no telling,
    and that side view is then left out rather than guessed.
    """
    four = views == FOUR
    answer = _ask_about(sheet, FACINGS_QUESTION if four else FACING_QUESTION, model=model, timeout=timeout)
    if four:
        return [a if a in ("left", "right") else None
                for a in ((answer or {}).get("bottom_left"), (answer or {}).get("bottom_right"))]
    found = (answer or {}).get("front_faces")
    return found if found in ("left", "right") else None


def _ask_about(sheet: Path, question: str, *, model: str, timeout: float) -> dict | None:
    """A vision model's JSON answer to a question about a sheet, or None."""
    import io

    from PIL import Image

    key = os.environ.get("OPENAI_API_KEY", "")
    if not key:
        return None
    try:
        image = Image.open(sheet).convert("RGB")
    except OSError:
        return None
    image.thumbnail((1024, 1024))
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=85)
    body: dict[str, Any] = {
        "model": model,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": question},
            {"type": "image_url", "image_url": {
                "url": "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode(),
                "detail": "low"}},
        ]}],
    }
    if model.startswith(("gpt-5", "o")):
        body["reasoning_effort"] = "low"
    request = urllib.request.Request(
        CHAT_URL, data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        answer = json.loads(payload["choices"][0]["message"]["content"])
    except Exception as exc:  # no answer is an answer: leave the side out
        log.info("could not tell which way the side view faces: %s", exc)
        return None
    return answer if isinstance(answer, dict) else None


# -- 2. a shape --------------------------------------------------------------


@dataclass(slots=True)
class Local:
    """Where the local generator lives, and which model it runs."""

    python: Path
    repo: Path
    weights: Path
    model: str = "tencent/Hunyuan3D-2mv"
    subfolder: str = "hunyuan3d-dit-v2-mv-turbo"
    steps: int = 20

    @property
    def ready(self) -> bool:
        """Set up all the way: a half-finished download is not ready, and
        "auto" goes on using the Space until it is."""
        return (self.python.is_file() and (self.repo / "hy3dgen").is_dir()
                and (self.weights / self.model / self.subfolder / "model.fp16.safetensors").is_file())


def shape(picture: Path, out: Path, *, views: str = "front", side: str | list | None = None,
          backend: str = "auto", local: Local | None = None, space: str = "", mv_space: str = "",
          token: str = "", timeout: float = 600.0, candidates: int = 1,
          tencent_region: str = "ap-singapore", tencent_model: str = "3.1", faces: int = 150000) -> Path:
    """Picture -> untextured GLB at `out`. A turnaround sheet is split into
    its views first; a side view is named for the way it faces (`side`, from
    side_facing) and left out when that is not known. `backend` is auto
    (this PC when the local generator is set up, else the Space), local,
    space, or tencent (Hunyuan 3D 3.x on Tencent Cloud, see tencent.py). With `candidates` above one, this PC's generator makes that many
    from one load, the rest beside `out` (see candidates_of); the Space makes
    one whatever is asked."""
    import tempfile

    with tempfile.TemporaryDirectory(prefix="arnold-views-") as work:
        if views in LAYOUTS:
            files = split_views(picture, Path(work), views)
            # Each side view named for the way it faces; one whose facing is
            # unknown, or that repeats a side already named, is left out.
            facings = side if isinstance(side, (list, tuple)) else [side]
            for key, facing in zip(("side", "side2"), [*facings, None]):
                view = files.pop(key, None)
                if view is not None and facing in ("left", "right") and facing not in files:
                    files[facing] = view
            files.pop("side2", None)
        else:
            files = {"front": picture}
        if backend == "tencent":
            from . import tencent

            try:
                return tencent.shape(files, out, region=tencent_region, model=tencent_model,
                                     faces=faces, timeout=timeout)
            except tencent.TencentError as exc:
                raise SculptError(str(exc)) from None
        if where(backend, local) == "local":
            if local is None or not local.ready:
                raise SculptError("The local 3D generator isn't set up - see models/hy3d in the README.")
            return shape_local(files, out, local, timeout, candidates)
        return shape_space(files, out, space=mv_space if len(files) > 1 else space,
                           token=token, timeout=timeout)


def where(backend: str, local: Local | None) -> str:
    """"local", "space" or "tencent": which one `shape` will use."""
    if backend == "tencent":
        return "tencent"
    if backend == "local" or (backend == "auto" and local is not None and local.ready):
        return "local"
    return "space"


def shape_local(views: dict[str, Path], out: Path, local: Local, timeout: float,
                candidates: int = 1) -> Path:
    """On this PC's GPU, through hy3d_worker.py in the generator's own
    environment. A fresh process each time, so the GPU is handed back the
    moment it is done - a game started afterwards gets all of it."""
    from . import process

    argv = [str(local.python), str(Path(__file__).with_name("hy3d_worker.py")),
            "--repo", str(local.repo), "--weights", str(local.weights), "--out", str(out), "--model", local.model,
            "--subfolder", local.subfolder, "--steps", str(local.steps),
            "--candidates", str(max(1, candidates))]
    if "turbo" in local.subfolder:
        argv.append("--flashvdm")
    for name, path in views.items():
        argv += ["--view", f"{name}={path}"]
    try:
        done = process.run(argv, timeout=timeout, cwd=str(local.repo))
    except subprocess.TimeoutExpired:
        raise SculptError(f"The local 3D generator was still going after {timeout:.0f} seconds.") from None
    except OSError as exc:
        raise SculptError(f"I couldn't start the local 3D generator: {exc}") from None
    reply: dict[str, Any] = {}
    for line in reversed((done.stdout or "").splitlines()):
        if line.startswith("{"):
            try:
                reply = json.loads(line)
                break
            except ValueError:
                continue
    if not reply.get("ok") or not out.is_file():
        error = reply.get("error") or (done.stderr or "").strip().splitlines()[-1:] or ["no reply"]
        raise SculptError("The local 3D generator couldn't make that: " + _local_error(str(error if isinstance(error, str) else error[0])))
    log.info("shaped locally from %s in %s", ",".join(views), reply.get("seconds"))
    return out


def candidates_of(out: Path) -> list[Path]:
    """Every shape `shape` made for `out`: out itself, then <out>.1.glb..."""
    extra = sorted(out.parent.glob(f"{out.stem}.*{out.suffix}"),
                   key=lambda p: int(p.suffixes[-2].lstrip(".")) if p.suffixes[-2].lstrip(".").isdigit() else 0)
    return [out] + [p for p in extra if p.suffixes[-2].lstrip(".").isdigit()]


def _local_error(text: str) -> str:
    if "out of memory" in text.lower():
        return "the graphics card ran out of memory - is a game running?"
    return text[:200]


def shape_space(views: dict[str, Path], out: Path, *, space: str, token: str = "",
                timeout: float = 600.0) -> Path:
    """On a Hugging Face Space: the multi-view one for several views."""
    try:
        from gradio_client import Client, handle_file
    except ImportError:
        raise SculptError(
            "Sculpting needs the sculpt extras: pip install -e .[sculpt]"
        ) from None
    if len(views) > 1:
        pictures = {f"mv_image_{name}": handle_file(str(path)) for name, path in views.items()}
    else:
        pictures = {"image": handle_file(str(next(iter(views.values()))))}
    try:
        client = Client(space, token=token or None, verbose=False)
        job = client.submit(
            caption=None,
            **pictures,
            steps=30,
            guidance_scale=5.0,
            seed=1234,
            octree_resolution=256,
            check_box_rembg=True,
            num_chunks=8000,
            randomize_seed=True,
            api_name="/shape_generation",
        )
        result = job.result(timeout=timeout)
    except TimeoutError:
        raise SculptError(
            f"The 3D model was still working after {timeout:.0f} seconds; the Space may be busy."
        ) from None
    except Exception as exc:  # AppError, httpx errors, a Space that is asleep
        raise SculptError(f"The 3D model couldn't make that: {_space_error(exc)}") from None
    path = _result_path(result)
    if path is None:
        raise SculptError("The 3D model finished but sent back no mesh.")
    shutil.copyfile(path, out)
    return out


def _result_path(result: Any) -> str | None:
    """The mesh file out of a Gradio reply - a path, or {"value": path}."""
    first = result[0] if isinstance(result, (list, tuple)) and result else result
    if isinstance(first, dict):
        first = first.get("value") or first.get("path")
    return str(first) if first and Path(str(first)).is_file() else None


def _space_error(exc: Exception) -> str:
    text = str(exc).strip().strip("'\"") or type(exc).__name__
    if "quota" in text.lower():
        return ("the free GPU allowance on Hugging Face is used up for now. "
                "Signing in with `hf auth login` raises it.")
    return text[:200]


# -- 3. a part ---------------------------------------------------------------


def to_part(mesh_path: Path, stl: Path, *, height_mm: float, max_size_mm: float,
            max_faces: int) -> Part:
    """Raw generator mesh -> a clean, Z-up, grounded STL at the size asked for."""
    try:
        import numpy as np
        import trimesh
    except ImportError:
        raise SculptError("Sculpting needs the sculpt extras: pip install -e .[sculpt]") from None

    mesh = trimesh.load(str(mesh_path), force="mesh")
    if mesh.is_empty or len(mesh.faces) == 0:
        raise SculptError("The 3D model came back empty.")
    mesh = clean(mesh)
    if max_faces and len(mesh.faces) > max_faces:
        mesh = decimate(mesh, max_faces)

    # glTF is Y-up; a print bed is Z-up. A quarter turn about X keeps the
    # top on top.
    mesh.apply_transform(trimesh.transformations.rotation_matrix(np.pi / 2, [1, 0, 0]))
    extents = mesh.extents
    scale = height_mm / extents[2]
    if max(extents) * scale > max_size_mm:  # tall is fine; wider than the bed is not
        scale = max_size_mm / max(extents)
    mesh.apply_scale(scale)
    low, high = mesh.bounds
    mesh.apply_translation([-(low[0] + high[0]) / 2, -(low[1] + high[1]) / 2, -low[2]])

    mesh.export(str(stl), file_type="stl")
    return Part(stl=stl, size=tuple(float(v) for v in mesh.extents),
                faces=len(mesh.faces), watertight=bool(mesh.is_watertight))


def decimate(mesh, faces: int):
    """Fewer triangles without tearing the mesh open.

    The decimator's default aggression tears a million-face mesh (which the
    local generator makes) in a couple of places; gentler passes do not. A
    pass that opens up a mesh that was whole is thrown away - a dense part
    prints fine, a torn one does not.
    """
    whole = mesh.is_watertight
    for aggression in (3, 1):
        try:
            smaller = mesh.simplify_quadric_decimation(face_count=faces, aggression=aggression)
        except TypeError:  # an older trimesh without the setting
            smaller = mesh.simplify_quadric_decimation(face_count=faces)
        except Exception as exc:  # fast_simplification missing: print it dense
            log.info("not decimating (%s); keeping %d faces", exc, len(mesh.faces))
            return mesh
        if smaller.is_watertight or not whole:
            return smaller
    log.info("decimating tore the mesh; keeping all %d faces", len(mesh.faces))
    return mesh


def rescale(stl_in: Path, stl_out: Path, *, height_mm: float, max_size_mm: float) -> Part:
    """The same part at another height - no AI involved, only arithmetic."""
    try:
        import trimesh
    except ImportError:
        raise SculptError("Resizing needs the sculpt extras: pip install -e .[sculpt]") from None
    mesh = trimesh.load(str(stl_in), force="mesh")
    extents = mesh.extents
    scale = height_mm / extents[2]
    if max(extents) * scale > max_size_mm:
        scale = max_size_mm / max(extents)
    mesh.apply_scale(scale)
    low, high = mesh.bounds
    mesh.apply_translation([-(low[0] + high[0]) / 2, -(low[1] + high[1]) / 2, -low[2]])
    mesh.export(str(stl_out), file_type="stl")
    return Part(stl=stl_out, size=tuple(float(v) for v in mesh.extents),
                faces=len(mesh.faces), watertight=bool(mesh.is_watertight))


def clean(mesh):
    """Drop the padding and the crumbs; close small holes if any are left."""
    import trimesh

    mesh.merge_vertices()
    mesh.update_faces(mesh.nondegenerate_faces())
    mesh.update_faces(mesh.unique_faces())
    mesh.remove_unreferenced_vertices()
    # Floating crumbs from background removal: keep any body with a real
    # share of the surface, so a figure with a detached sword keeps both.
    bodies = mesh.split(only_watertight=False)
    if len(bodies) > 1:
        largest = max(len(b.faces) for b in bodies)
        kept = [b for b in bodies if len(b.faces) >= largest * 0.02]
        mesh = trimesh.util.concatenate(kept)
    if not mesh.is_watertight:
        trimesh.repair.fill_holes(mesh)
        trimesh.repair.fix_normals(mesh)
    return mesh
