"""GPU telemetry via nvidia-smi.

Deliberately shells out rather than depending on NVML bindings: nvidia-smi ships
with every NVIDIA driver, and a machine without one simply reports no GPUs. The
first failed probe disables further attempts so a non-NVIDIA box does not pay a
subprocess spawn on every tick.
"""

from __future__ import annotations

import logging
import subprocess

from .. import process

log = logging.getLogger(__name__)

_QUERY = "name,utilization.gpu,utilization.memory,memory.used,memory.total,temperature.gpu,power.draw"
_FIELDS = (
    "name",
    "utilization_percent",
    "memory_utilization_percent",
    "memory_used_mb",
    "memory_total_mb",
    "temperature_c",
    "power_watts",
)

_available: bool | None = None


def _coerce(field: str, raw: str) -> object:
    raw = raw.strip()
    if not raw or raw in ("[N/A]", "[Not Supported]", "N/A"):
        return None
    if field == "name":
        return raw
    try:
        return float(raw) if "." in raw else int(raw)
    except ValueError:
        return None


def probe() -> list[dict[str, object]]:
    """Return one dict per GPU. Empty list when nvidia-smi is absent or fails."""
    global _available
    if _available is False:
        return []

    try:
        proc = process.run(
            ["nvidia-smi", f"--query-gpu={_QUERY}", "--format=csv,noheader,nounits"],
            timeout=6,
        )
    except (FileNotFoundError, OSError):
        if _available is None:
            log.info("nvidia-smi not found - GPU telemetry disabled")
        _available = False
        return []
    except subprocess.TimeoutExpired:
        log.warning("nvidia-smi timed out; skipping GPU telemetry this tick")
        return []

    if proc.returncode != 0:
        if _available is None:
            log.info("nvidia-smi returned %s - GPU telemetry disabled", proc.returncode)
        _available = False
        return []

    gpus: list[dict[str, object]] = []
    for index, line in enumerate(proc.stdout.strip().splitlines()):
        if not line.strip():
            continue
        cells = [c.strip() for c in line.split(",")]
        if len(cells) < len(_FIELDS):
            cells += [""] * (len(_FIELDS) - len(cells))
        entry: dict[str, object] = {"index": index}
        for field, raw in zip(_FIELDS, cells):
            entry[field] = _coerce(field, raw)
        gpus.append(entry)

    _available = True
    return gpus
