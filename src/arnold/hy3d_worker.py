"""Views of an object in, an untextured mesh out, on this PC's own GPU.

This runs in its own environment (models/hy3d/.venv), not A.R.N.O.L.D.'s:
PyTorch built for CUDA is several gigabytes and pins versions nothing else
here should have to live with. So it imports nothing from the package; the
agent runs it as a child process and reads one line of JSON from the end
of its output. See sculpt.shape_local.

    python hy3d_worker.py --repo models/hy3d/Hunyuan3D-2 --out mesh.glb \
        --view front=front.png --view back=back.png

The multi-view model (Hunyuan3D-2mv) takes any of front, back, left and
right; a single front view works too, it just has to guess the back.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True, help="the Hunyuan3D-2 checkout, for hy3dgen")
    parser.add_argument("--weights", default="", help="folder of <model>/<subfolder> weights")
    parser.add_argument("--out", required=True)
    parser.add_argument("--view", action="append", default=[], help="front=path, back=path, ...")
    parser.add_argument("--model", default="tencent/Hunyuan3D-2mv")
    parser.add_argument("--subfolder", default="hunyuan3d-dit-v2-mv-turbo")
    parser.add_argument("--steps", type=int, default=5)
    parser.add_argument("--octree", type=int, default=380)
    parser.add_argument("--seed", type=int, default=-1)
    parser.add_argument("--flashvdm", action="store_true")
    args = parser.parse_args()

    timings: dict[str, float] = {}
    try:
        import os

        if args.weights:
            # hy3dgen looks here before it goes to the Hub.
            os.environ["HY3DGEN_MODELS"] = args.weights
        os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
        sys.path.insert(0, args.repo)
        import random

        import torch
        from PIL import Image

        from hy3dgen.rembg import BackgroundRemover
        from hy3dgen.shapegen import Hunyuan3DDiTFlowMatchingPipeline

        if not torch.cuda.is_available():
            raise RuntimeError("PyTorch can't see the GPU")

        started = time.time()
        views = {}
        remover = None
        for item in args.view:
            name, _, path = item.partition("=")
            image = Image.open(path)
            if image.mode == "RGBA" and image.getextrema()[3][0] < 255:
                views[name] = image  # already cut out
                continue
            cut = plain_matte(image.convert("RGB"))
            if cut is None:
                # A real photo: the AI matting model, which is slow on the CPU
                # but copes with any background.
                remover = remover or BackgroundRemover()
                cut = remover(image.convert("RGB"))
            views[name] = cut
        if not views:
            raise ValueError("no views were given")
        timings["background"] = time.time() - started

        started = time.time()
        pipeline = Hunyuan3DDiTFlowMatchingPipeline.from_pretrained(
            args.model, subfolder=args.subfolder, variant="fp16",
        )
        if args.flashvdm:
            pipeline.enable_flashvdm()
        timings["load"] = time.time() - started

        started = time.time()
        seed = args.seed if args.seed >= 0 else random.randrange(1 << 31)
        mesh = pipeline(
            # The multi-view model takes a dict of views; a plain model one image.
            image=views if "mv" in args.subfolder else next(iter(views.values())),
            num_inference_steps=args.steps,
            octree_resolution=args.octree,
            num_chunks=20000,
            generator=torch.manual_seed(seed),
            output_type="trimesh",
        )[0]
        timings["shape"] = time.time() - started
        mesh.export(args.out)
        print(json.dumps({"ok": True, "faces": int(len(mesh.faces)), "seed": seed,
                          "views": sorted(views), "seconds": timings,
                          "gpu": torch.cuda.get_device_name(0)}))
        return 0
    except Exception as exc:  # the parent reads this line, whatever went wrong
        traceback.print_exc(file=sys.stderr)
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}", "seconds": timings}))
        return 1


def plain_matte(image):
    """Cut the object out of a plain, light background - which is what every
    picture drawn for a sculpture has - or None if the background is not
    plain enough to trust it. Background is whatever light region touches
    the edge of the picture; everything else is the object, holes and all.
    Milliseconds, where the AI matting model takes most of a minute."""
    import cv2
    import numpy as np
    from PIL import Image

    rgb = np.asarray(image)
    grey = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    border = np.concatenate([grey[0], grey[-1], grey[:, 0], grey[:, -1]])
    spread = rgb.max(axis=2).astype(int) - rgb.min(axis=2)
    if (border > 225).mean() < 0.97:
        return None  # not a plain light backdrop: a photo, most likely
    # 185, not nearer white: the faint contact shadow under the figure sits
    # at 185-210 and the clay tops out around 170. Counted as object, that
    # shadow comes back from the shape model as a thin plate under the feet.
    light = ((grey > 185) & (spread < 30)).astype(np.uint8)
    count, labels = cv2.connectedComponents(light, connectivity=4)
    edge = np.unique(np.concatenate([labels[0], labels[-1], labels[:, 0], labels[:, -1]]))
    background = np.isin(labels, edge[edge > 0]) if count > 1 else np.zeros_like(light, bool)
    solid = np.where(background, 0, 255).astype(np.uint8)
    # Whatever is left that is only a few pixels thick - the edge of a
    # tabletop, a streak of shadow - is not part of the figure.
    solid = cv2.morphologyEx(solid, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9)))
    count, labels, stats, _ = cv2.connectedComponentsWithStats(solid, connectivity=8)
    if count > 1:
        areas = stats[1:, cv2.CC_STAT_AREA]
        keep = 1 + np.flatnonzero(areas >= areas.max() * 0.02)
        solid = np.where(np.isin(labels, keep), 255, 0).astype(np.uint8)
    alpha = cv2.GaussianBlur(solid, (3, 3), 0)  # a soft edge, not a staircase
    if (alpha > 128).mean() < 0.01:
        return None  # found nothing: let the real matting model look
    return Image.fromarray(np.dstack([rgb, alpha]), "RGBA")


if __name__ == "__main__":
    sys.exit(main())
