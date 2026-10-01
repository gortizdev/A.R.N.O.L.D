"""A picture on a designed part: a stencil drawn by OpenAI, traced to SVG.

A designed part's look is otherwise what a code model can write by hand -
text, circles, a few polygons - which is fine for a monogram and hopeless
for a wolf's head or a hero's crest. So the picture is turned into
something OpenSCAD can emboss: the image model redraws it as a flat,
bold, one-colour stencil (from the user's picture, or from words), and it
is traced here into an SVG of filled outlines that `emblem()` in
scad/arnold_parts.scad raises off a face like any other decoration.

Tracing is plain thresholding and contour following (scikit-image). Specks
and hairlines narrower than a nozzle's worth at the size it will be printed
are opened away first, since a 0.2 mm island on a 30 mm emblem is not a
detail, it is stringing.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)

STENCIL_PROMPT = (
    "A bold flat stencil emblem of {subject}: solid pure black shapes on a pure white "
    "background, like a vinyl-cut decal or a rubber stamp. One recognisable silhouette "
    "with a few strong interior cut-outs for the key features, thick lines, no thin "
    "strokes, no gradients, no shading, no outline frame, no text, centred with white "
    "space around it."
)
STENCIL_EDIT = (
    "Redraw the main subject of this picture as a bold flat stencil emblem{about}: solid "
    "pure black shapes on a pure white background, like a vinyl-cut decal or a rubber "
    "stamp. Keep what makes it recognisable - its silhouette and a few strong interior "
    "cut-outs for the key features - and drop everything else: no thin strokes, no "
    "gradients, no shading, no background, no outline frame, no text. Centred, with "
    "white space around it."
)

# A request that wants a picture on the part rather than a shape change.
_WANTS = re.compile(
    r"\b(emblems?|logos?|symbols?|badges?|crests?|insignia|icons?|pictures?|images?|"
    r"silhouettes?|portraits?|motifs?|decals?|stickers?|stencils?|artwork|mascot)\b",
    re.I,
)


class EmblemError(Exception):
    """No emblem could be had. The message is spoken."""


def wanted(text: str) -> bool:
    """Whether words ask for a picture on the part."""
    return bool(_WANTS.search(text or ""))


def draw(subject: str, out: Path, *, model: str, picture: Path | None = None,
         timeout: float = 240.0) -> Path:
    """A black-on-white stencil of the subject, written to `out` as PNG:
    redrawn from `picture` when there is one, else drawn from the words."""
    from . import sculpt

    key = os.environ.get("OPENAI_API_KEY", "")
    if not key:
        raise EmblemError("I need an OpenAI key to draw the emblem, and OPENAI_API_KEY isn't set.")
    subject = subject.strip().rstrip(".")
    try:
        if picture is not None:
            about = f" for {subject}" if subject else ""
            fields = {"model": model, "prompt": STENCIL_EDIT.format(about=about),
                      "size": "1024x1024", "quality": "medium"}
            data = sculpt._post_edit(key, fields, picture, timeout)
        else:
            body = json.dumps({"model": model, "prompt": STENCIL_PROMPT.format(subject=subject),
                               "size": "1024x1024", "quality": "medium"}).encode()
            request = urllib.request.Request(
                sculpt.IMAGE_URL, data=body, method="POST",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            data = base64.b64decode(payload["data"][0]["b64_json"])
    except urllib.error.HTTPError as exc:
        raise EmblemError(f"The image model wouldn't draw the emblem: {sculpt._openai_error(exc)}") from None
    except (urllib.error.URLError, OSError, KeyError, IndexError, ValueError) as exc:
        raise EmblemError(f"I couldn't get the emblem drawn: {exc}") from None
    out.write_bytes(data)
    return out


def trace(image: Path, svg: Path, *, detail: float = 0.012, size: int = 512) -> float:
    """The dark shapes of `image` as filled SVG outlines, written to `svg`.

    `detail` is the smallest feature kept, as a fraction of the emblem's
    width. Returns the traced emblem's height over its width, which is all
    whoever places it needs to know to make it fit a face.
    """
    import numpy as np
    from PIL import Image
    from scipy import ndimage
    from skimage import filters, measure

    try:
        picture = Image.open(image)
        picture.load()
    except OSError as exc:
        raise EmblemError(f"I couldn't read the emblem picture: {exc}") from None
    # Transparency counts as background, whatever colour sits under it.
    if picture.mode in ("RGBA", "LA", "P"):
        picture = picture.convert("RGBA")
        backdrop = Image.new("RGBA", picture.size, (255, 255, 255, 255))
        picture = Image.alpha_composite(backdrop, picture)
    grey = picture.convert("L")
    grey.thumbnail((size, size))
    pixels = np.asarray(grey, dtype=float)
    if pixels.max() - pixels.min() < 16:
        raise EmblemError("The emblem came out blank - there was nothing to trace.")
    ink = pixels < filters.threshold_otsu(pixels)
    # Black-on-white is asked for; a picture that came back inverted still
    # has its subject in the middle and its background around the edge.
    edge = np.concatenate([ink[0], ink[-1], ink[:, 0], ink[:, -1]])
    if edge.mean() > 0.5:
        ink = ~ink
    rows, cols = np.nonzero(ink)
    if rows.size == 0:
        raise EmblemError("The emblem came out blank - there was nothing to trace.")
    ink = ink[rows.min():rows.max() + 1, cols.min():cols.max() + 1]
    height, width = ink.shape
    # Anything narrower than `detail` of the width goes: specks, hairlines,
    # slivers of background between two shapes.
    radius = max(1, round(detail * width / 2))
    disk = _disk(radius)
    ink = ndimage.binary_opening(ink, structure=disk)
    ink = ndimage.binary_closing(np.pad(ink, radius + 1), structure=disk)[radius + 1:-radius - 1, radius + 1:-radius - 1]
    if not ink.any():
        raise EmblemError("The emblem was too fine to print - nothing survived at that size.")
    padded = np.pad(ink, 2).astype(float)
    smallest = (detail * width) ** 2
    paths = []
    for contour in measure.find_contours(padded, 0.5):
        outline = measure.approximate_polygon(contour, tolerance=0.8)
        if len(outline) < 4 or _area(outline) < smallest:
            continue
        points = " L".join(f"{c - 2:.1f} {r - 2:.1f}" for r, c in outline[:-1])
        paths.append(f"M{points}Z")
    if not paths:
        raise EmblemError("The emblem was too fine to print - nothing survived at that size.")
    svg.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{width}" height="{height}" viewBox="0 0 {width} {height}">'
        f'<path fill="black" fill-rule="evenodd" d="{" ".join(paths)}"/></svg>\n',
        encoding="utf-8",
    )
    return height / width


def _disk(radius: int):
    import numpy as np

    y, x = np.ogrid[-radius:radius + 1, -radius:radius + 1]
    return x * x + y * y <= radius * radius


def _area(outline) -> float:
    import numpy as np

    r, c = outline[:, 0], outline[:, 1]
    return abs(float((c * np.roll(r, -1) - r * np.roll(c, -1)).sum())) / 2
