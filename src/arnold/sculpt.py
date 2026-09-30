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
    "Orthographic, eye level, each view whole and centred in its half with space "
    "around it, sturdy proportions with no very thin parts, resting on a flat "
    "bottom, plain white background, soft even studio lighting, no shadows, no "
    "text, no labels, no dividing line."
)
THREE_PROMPT = (
    "A turnaround reference sheet of one object: {subject}, as a matte grey clay "
    "3D-printed model. Exactly three views in one row, all at the same scale and the "
    "same height, orthographic, eye level: on the LEFT the FRONT view, looking "
    "straight at its front; in the MIDDLE the SIDE view in exact profile, turned 90 "
    "degrees; on the RIGHT the BACK view, seen from directly behind. Each view whole "
    "and centred in its third with space around it, sturdy proportions with no very "
    "thin parts, resting on a flat bottom, plain white background, soft even studio "
    "lighting, no shadows, no text, no labels, no dividing lines."
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
# The order the views sit in across a sheet, left to right.
LAYOUTS = {SHEET: ("front", "back"), THREE: ("front", "side", "back")}


def draw(subject: str, out: Path, *, model: str, views: str = SHEET, timeout: float = 180.0) -> Path:
    """The subject as a clay figurine, written to `out` as PNG: a front-and-
    back sheet, or with views="front" a single three-quarter view."""
    key = os.environ.get("OPENAI_API_KEY", "")
    if not key:
        raise SculptError("I need an OpenAI key to draw the reference picture, and OPENAI_API_KEY isn't set.")
    prompt = {SHEET: SHEET_PROMPT, THREE: THREE_PROMPT}.get(views, IMAGE_PROMPT)
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


def _post_edit(key: str, fields: dict[str, str], picture: Path, timeout: float) -> bytes:
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
    front and back only), in the order LAYOUTS gives.

    Each cut goes down the emptiest column near where it is expected rather
    than exactly there, so a wing that strays over is not sliced off. Each
    view is padded out to a square on white without being rescaled: the
    multi-view model reads them as one object seen from several sides, and
    that only holds if they are all at the same scale.
    """
    import numpy as np
    from PIL import Image, ImageOps

    names = LAYOUTS.get(views, LAYOUTS[SHEET])
    image = Image.open(sheet).convert("RGB")
    width, height = image.size
    columns = (255 - np.asarray(image.convert("L"), dtype=np.int64)).sum(axis=0)  # ink per column
    n = len(names)
    cuts = [0]
    for k in range(1, n):
        expected = width * k // n
        lo, hi = expected - width // (n * 4), expected + width // (n * 4)
        cuts.append(min(range(lo, hi), key=lambda x: (columns[x], abs(x - expected))))
    cuts.append(width)

    out_dir.mkdir(parents=True, exist_ok=True)
    found = {}
    for name, left, right in zip(names, cuts, cuts[1:]):
        part = image.crop((left, 0, right, height))
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


CHAT_URL = "https://api.openai.com/v1/chat/completions"
FACING_QUESTION = (
    "This reference sheet shows one object three times: the FRONT view on the left, a "
    "SIDE view in the middle, the BACK view on the right. In the middle side view, "
    "which edge of the picture does the object's front (the side seen in the "
    "left-hand view) face: left or right? "
    'Reply with JSON only: {"front_faces": "left" | "right" | "unsure"}'
)


def side_facing(sheet: Path, *, model: str, timeout: float = 60.0) -> str | None:
    """Which way the side view on a three-view sheet faces: "left" or "right".

    Asked, not assumed. The drawing is told which way to turn it and does
    not reliably listen, and the shape model has to know: a side view
    labelled the wrong way round puts the belt loop on the front. The answer
    is also the side's name to the shape model - an object whose front
    points left is seen from its own left. None when there is no telling,
    and the side view is then left out rather than guessed.
    """
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
            {"type": "text", "text": FACING_QUESTION},
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
        answer = json.loads(payload["choices"][0]["message"]["content"]).get("front_faces")
    except Exception as exc:  # no answer is an answer: leave the side out
        log.info("could not tell which way the side view faces: %s", exc)
        return None
    return answer if answer in ("left", "right") else None


# -- 2. a shape --------------------------------------------------------------


@dataclass(slots=True)
class Local:
    """Where the local generator lives, and which model it runs."""

    python: Path
    repo: Path
    weights: Path
    model: str = "tencent/Hunyuan3D-2mv"
    subfolder: str = "hunyuan3d-dit-v2-mv-turbo"
    steps: int = 5

    @property
    def ready(self) -> bool:
        """Set up all the way: a half-finished download is not ready, and
        "auto" goes on using the Space until it is."""
        return (self.python.is_file() and (self.repo / "hy3dgen").is_dir()
                and (self.weights / self.model / self.subfolder / "model.fp16.safetensors").is_file())


def shape(picture: Path, out: Path, *, views: str = "front", side: str | None = None,
          backend: str = "auto", local: Local | None = None, space: str = "", mv_space: str = "",
          token: str = "", timeout: float = 600.0) -> Path:
    """Picture -> untextured GLB at `out`. A turnaround sheet is split into
    its views first; a side view is named for the way it faces (`side`, from
    side_facing) and left out when that is not known. `backend` is auto
    (this PC when the local generator is set up, else the Space), local, or
    space."""
    import tempfile

    with tempfile.TemporaryDirectory(prefix="arnold-views-") as work:
        if views in LAYOUTS:
            files = split_views(picture, Path(work), views)
            view = files.pop("side", None)
            if view is not None and side in ("left", "right"):
                files[side] = view
        else:
            files = {"front": picture}
        if where(backend, local) == "local":
            if local is None or not local.ready:
                raise SculptError("The local 3D generator isn't set up - see models/hy3d in the README.")
            return shape_local(files, out, local, timeout)
        return shape_space(files, out, space=mv_space if len(files) > 1 else space,
                           token=token, timeout=timeout)


def where(backend: str, local: Local | None) -> str:
    """"local" or "space": which one `shape` will use."""
    if backend == "local" or (backend == "auto" and local is not None and local.ready):
        return "local"
    return "space"


def shape_local(views: dict[str, Path], out: Path, local: Local, timeout: float) -> Path:
    """On this PC's GPU, through hy3d_worker.py in the generator's own
    environment. A fresh process each time, so the GPU is handed back the
    moment it is done - a game started afterwards gets all of it."""
    from . import process

    argv = [str(local.python), str(Path(__file__).with_name("hy3d_worker.py")),
            "--repo", str(local.repo), "--weights", str(local.weights), "--out", str(out), "--model", local.model,
            "--subfolder", local.subfolder, "--steps", str(local.steps)]
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
