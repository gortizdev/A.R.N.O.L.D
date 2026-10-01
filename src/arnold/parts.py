"""The parts made for the printer, as a collection.

Every part is a stem in the prints folder - `20260929-145134-owl` - with the
files that share it: the STL, and for a sculpture the picture it started
from and the raw mesh, for a designed part its OpenSCAD. Beside them sits a
small JSON record: what was asked for, how it went, how long each step took.

The record is written as the work goes, not only at the end, because the
work happens in whichever process was asked - the voice session, the agent,
the dashboard - and the Workshop tab has to follow a sculpture through its
steps from any of them. A file is the one thing all three can see.

Parts made before records existed still count: an STL alone is a part.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

# What a stem looks like. Anything else asked for by name is refused, which
# is what keeps a name from the page or a voice from reaching outside the
# folder.
STEM_RE = re.compile(r"^\d{8}-\d{6}-[a-z0-9][a-z0-9-]{0,47}$")
FILE_TYPES = {
    "stl": "model/stl",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "glb": "model/gltf-binary",
    "scad": "text/plain; charset=utf-8",
    # A designed part's emblem, traced from a picture (emblem.py).
    "svg": "image/svg+xml",
}
PICTURES = ("png", "jpg", "jpeg", "webp")
WORKING = ("queued", "drawing", "editing", "tracing", "writing", "shaping", "cleaning", "choosing", "rendering", "checking")


def valid(stem: str) -> bool:
    return bool(STEM_RE.match(stem or ""))


class Catalog:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    # -- one part --------------------------------------------------------------

    def record_path(self, stem: str) -> Path:
        return self.directory / f"{stem}.json"

    def read(self, stem: str) -> dict[str, Any]:
        try:
            data = json.loads(self.record_path(stem).read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def write(self, stem: str, **fields: Any) -> dict[str, Any]:
        """Merge fields into the record. Written whole and renamed into
        place, so a reader in another process never sees half of it."""
        record = {**self.read(stem), **fields, "name": stem, "updated": time.time()}
        path = self.record_path(stem)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(record, indent=1), encoding="utf-8")
        os.replace(temp, path)
        return record

    def stage(self, stem: str, state: str) -> None:
        """Move on to `state`, closing the timing of the step before."""
        record = self.read(stem)
        now = time.time()
        stages = dict(record.get("stages") or {})
        current = record.get("state")
        if current in stages and len(stages[current]) == 1:
            stages[current] = [stages[current][0], now]
        if state in WORKING:
            stages[state] = [now]
        self.write(stem, state=state, stages=stages,
                   **({"finished": now} if state in ("done", "failed") else {}))

    def file(self, stem: str, kind: str) -> Path | None:
        """A part's file, if the stem and type are ones we serve and it exists."""
        kind = (kind or "").lower()
        if not valid(stem) or kind not in FILE_TYPES:
            return None
        path = self.directory / f"{stem}.{kind}"
        return path if path.is_file() else None

    def delete(self, stem: str) -> int:
        if not valid(stem):
            return 0
        gone = 0
        for kind in (*FILE_TYPES, "json"):
            try:
                (self.directory / f"{stem}.{kind}").unlink()
                gone += 1
            except OSError:
                pass
        return gone

    # -- all of them -------------------------------------------------------

    def list(self, stale_after: float = 900.0) -> list[dict[str, Any]]:
        """Newest first. A part still 'working' long after it should have
        finished belonged to a process that died; it is reported as such."""
        stems: set[str] = set()
        for path in self.directory.glob("*.*"):
            stem = path.name.split(".", 1)[0]
            if valid(stem) and path.suffix.lstrip(".") in (*FILE_TYPES, "json"):
                stems.add(stem)
        now = time.time()
        parts = []
        for stem in stems:
            record = self.read(stem)
            files = [k for k in FILE_TYPES if (self.directory / f"{stem}.{k}").is_file()]
            if not record and "stl" not in files:
                continue  # a stray picture, not a part
            part = {
                "name": stem,
                "title": record.get("title") or _title_from(stem),
                "kind": record.get("kind") or ("make" if "scad" in files else "sculpt"),
                "state": record.get("state") or "done",
                "created": record.get("created") or _created_from(stem),
                **{k: v for k, v in record.items() if k not in ("name",)},
                "files": files,
                "picture": next((k for k in PICTURES if k in files), None),
            }
            if part["state"] in WORKING and now - float(part.get("updated") or 0) > stale_after:
                part["state"] = "failed"
                part["error"] = part.get("error") or "It was interrupted - the process doing it stopped."
            parts.append(part)
        parts.sort(key=lambda p: (p["created"], p["name"]), reverse=True)
        return parts


def _title_from(stem: str) -> str:
    words = stem.split("-", 2)[-1].replace("-", " ").strip()
    return words[:1].upper() + words[1:] if words else "Part"


def _created_from(stem: str) -> float:
    try:
        return time.mktime(time.strptime(stem[:15], "%Y%m%d-%H%M%S"))
    except ValueError:
        return 0.0
