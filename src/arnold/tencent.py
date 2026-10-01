"""Tencent Hunyuan 3D (international): the cloud shape model.

Hunyuan3D 3.x - the generation after the 2mv model this PC runs - is not
downloadable; it is served as an API on Tencent Cloud's international site,
billed in credits. It takes the same views the local model does (a front,
and left/right/back), so a turnaround sheet goes to it unchanged, and it
can return a bare "Geometry" mesh with no texture, which is all a print
needs.

Plain HTTPS with Tencent Cloud's TC3-HMAC-SHA256 signature, done here rather
than through the SDK: two calls (submit, then query until done) and a
download. The keys come from the TENCENTCLOUD_SECRET_ID and
TENCENTCLOUD_SECRET_KEY environment variables (Tencent's own SDK reads the
same names) and never from the config file, which the dashboard shows.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import logging
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

HOST = "hunyuan.intl.tencentcloudapi.com"
SERVICE = "hunyuan"
VERSION = "2023-09-01"
# The views the API takes beside the front one (3.1 also takes top, bottom
# and the 45-degree ones, which no sheet here draws).
VIEW_TYPES = ("left", "right", "back")


class TencentError(Exception):
    """The cloud shape could not be had. The message is spoken."""


def keys() -> tuple[str, str]:
    return (os.environ.get("TENCENTCLOUD_SECRET_ID", "").strip(),
            os.environ.get("TENCENTCLOUD_SECRET_KEY", "").strip())


def configured() -> bool:
    return all(keys())


def sign(secret_id: str, secret_key: str, action: str, payload: str, *, region: str,
         timestamp: int | None = None, host: str = HOST, service: str = SERVICE,
         version: str = VERSION) -> dict[str, str]:
    """The headers for one signed call (TC3-HMAC-SHA256), signed exactly as
    Tencent's own Python SDK signs them (tests/test_tencent.py holds a
    signature the SDK made)."""
    timestamp = int(time.time()) if timestamp is None else timestamp
    date = datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d")
    content_type = "application/json"
    canonical = (f"POST\n/\n\ncontent-type:{content_type}\nhost:{host}\n\n"
                 f"content-type;host\n{hashlib.sha256(payload.encode()).hexdigest()}")
    scope = f"{date}/{service}/tc3_request"
    to_sign = f"TC3-HMAC-SHA256\n{timestamp}\n{scope}\n{hashlib.sha256(canonical.encode()).hexdigest()}"

    def _hmac(key: bytes, text: str) -> bytes:
        return hmac.new(key, text.encode(), hashlib.sha256).digest()

    signing = _hmac(_hmac(_hmac(("TC3" + secret_key).encode(), date), service), "tc3_request")
    signature = hmac.new(signing, to_sign.encode(), hashlib.sha256).hexdigest()
    return {
        "Authorization": (f"TC3-HMAC-SHA256 Credential={secret_id}/{scope}, "
                          f"SignedHeaders=content-type;host, Signature={signature}"),
        "Content-Type": content_type,
        "Host": host,
        "X-TC-Action": action,
        "X-TC-Timestamp": str(timestamp),
        "X-TC-Version": version,
        "X-TC-Region": region,
    }


def call(action: str, params: dict, *, region: str, timeout: float = 60.0) -> dict:
    """One API call; the Response object, or TencentError with Tencent's own
    words for what was wrong."""
    secret_id, secret_key = keys()
    if not (secret_id and secret_key):
        raise TencentError("I need Tencent Cloud keys for that - set TENCENTCLOUD_SECRET_ID and "
                           "TENCENTCLOUD_SECRET_KEY.")
    payload = json.dumps(params)
    request = urllib.request.Request(f"https://{HOST}", data=payload.encode(), method="POST",
                                     headers=sign(secret_id, secret_key, action, payload, region=region))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            reply = json.loads(response.read().decode("utf-8")).get("Response") or {}
    except urllib.error.HTTPError as exc:
        raise TencentError(f"Tencent Cloud turned that down (HTTP {exc.code}).") from None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise TencentError(f"I couldn't reach Tencent Cloud: {exc}") from None
    error = reply.get("Error")
    if error:
        raise TencentError(f"Tencent Cloud said {error.get('Code')}: {error.get('Message')}")
    return reply


def _encoded(path: Path, side: int = 1024) -> str:
    """A view as base64 JPEG, small enough that four fit the API's 6 MB."""
    from PIL import Image

    with Image.open(path) as image:
        image = image.convert("RGB")
        image.thumbnail((side, side))
        buffer = io.BytesIO()
        image.save(buffer, "JPEG", quality=90)
    return base64.b64encode(buffer.getvalue()).decode()


def request_for(views: dict[str, Path], *, model: str, faces: int) -> dict:
    """The submit parameters for a set of views: front as the image, the
    rest as extra views, geometry only."""
    front = views.get("front") or next(iter(views.values()))
    params: dict = {"Model": model, "ImageBase64": _encoded(front), "GenerateType": "Geometry",
                    "FaceCount": max(40000, min(int(faces), 1500000))}
    extra = [{"ViewType": name, "ViewImageBase64": _encoded(path)}
             for name, path in views.items() if name in VIEW_TYPES]
    if extra:
        params["MultiViewImages"] = extra
    return params


def shape(views: dict[str, Path], out: Path, *, region: str = "ap-singapore", model: str = "3.1",
          faces: int = 150000, timeout: float = 600.0, poll: float = 5.0) -> Path:
    """Views -> untextured GLB at `out`, made on Tencent Cloud."""
    job = call("SubmitHunyuanTo3DProJob", request_for(views, model=model, faces=faces), region=region)
    job_id = job.get("JobId")
    if not job_id:
        raise TencentError("Tencent Cloud took the job but gave no job number back.")
    log.info("tencent job %s submitted (%s, %s)", job_id, model, ",".join(views))
    deadline = time.time() + timeout
    while True:
        status = call("QueryHunyuanTo3DProJob", {"JobId": job_id}, region=region)
        state = status.get("Status")
        if state == "DONE":
            break
        if state == "FAIL":
            raise TencentError("Tencent Cloud couldn't make that: "
                               f"{status.get('ErrorMessage') or status.get('ErrorCode') or 'no reason given'}")
        if time.time() > deadline:
            raise TencentError(f"Tencent Cloud was still working after {timeout:.0f} seconds.")
        time.sleep(poll)
    files = status.get("ResultFile3Ds") or []
    chosen = next((f for f in files if str(f.get("Type", "")).upper() == "GLB"), files[0] if files else None)
    if not chosen or not chosen.get("Url"):
        raise TencentError("Tencent Cloud finished but sent no model back.")
    try:
        with urllib.request.urlopen(chosen["Url"], timeout=120) as response:
            data = response.read()
    except (urllib.error.URLError, OSError) as exc:
        raise TencentError(f"I couldn't download the model from Tencent Cloud: {exc}") from None
    if data[:2] == b"PK":
        # Some result types come zipped: the model is the .glb inside.
        import zipfile

        with zipfile.ZipFile(io.BytesIO(data)) as bundle:
            inside = next((n for n in bundle.namelist() if n.lower().endswith(".glb")), None)
            if inside is None:
                raise TencentError("Tencent Cloud's download had no GLB model in it.")
            data = bundle.read(inside)
    out.write_bytes(data)
    return out
