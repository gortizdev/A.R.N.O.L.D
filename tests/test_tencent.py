"""tencent.py: Hunyuan 3D 3.x on Tencent Cloud, with the network stubbed.

The signature is checked against one Tencent's own Python SDK made for the
same request, so a change that breaks signing fails here rather than as an
AuthFailure on a real account.
"""

import io
import json
import zipfile

import pytest

from arnold import tencent


def test_the_signature_is_the_sdks():
    # Made by tencentcloud-sdk-python-common (CommonClient, TC3-HMAC-SHA256)
    # for QueryHunyuanTo3DProJob with these made-up keys.
    headers = tencent.sign("AKIDtest", "secretkey123", "QueryHunyuanTo3DProJob", '{"JobId": "abc"}',
                           region="ap-singapore", timestamp=1790795705)
    assert headers["Authorization"] == (
        "TC3-HMAC-SHA256 Credential=AKIDtest/2026-09-30/hunyuan/tc3_request, SignedHeaders=content-type;host, "
        "Signature=35fafd78dab81414521b2e6a1906165ced0b27276ab1858b215e061d47359bfb")
    assert headers["X-TC-Version"] == "2023-09-01" and headers["X-TC-Region"] == "ap-singapore"


def views(tmp_path):
    from PIL import Image

    found = {}
    for name in ("front", "left", "back"):
        Image.new("RGB", (64, 64), "white").save(tmp_path / f"{name}.png")
        found[name] = tmp_path / f"{name}.png"
    return found


def test_a_sheet_goes_as_front_image_and_extra_views(tmp_path):
    params = tencent.request_for(views(tmp_path), model="3.1", faces=10)
    assert params["Model"] == "3.1" and params["GenerateType"] == "Geometry"
    assert params["FaceCount"] == 40000  # the API's floor
    assert [v["ViewType"] for v in params["MultiViewImages"]] == ["left", "back"]
    assert params["ImageBase64"] and all(v["ViewImageBase64"] for v in params["MultiViewImages"])


def test_no_keys_is_said_plainly(monkeypatch):
    monkeypatch.delenv("TENCENTCLOUD_SECRET_ID", raising=False)
    monkeypatch.delenv("TENCENTCLOUD_SECRET_KEY", raising=False)
    assert not tencent.configured()
    with pytest.raises(tencent.TencentError, match="TENCENTCLOUD_SECRET_ID"):
        tencent.call("QueryHunyuanTo3DProJob", {"JobId": "x"}, region="ap-singapore")


class Download:
    def __init__(self, data):
        self.data = data

    def read(self):
        return self.data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.mark.parametrize("zipped", [False, True])
def test_a_job_is_submitted_waited_for_and_downloaded(tmp_path, monkeypatch, zipped):
    calls = []
    replies = iter([{"JobId": "j1"}, {"Status": "RUN"},
                    {"Status": "DONE", "ResultFile3Ds": [{"Type": "GLB", "Url": "https://x/model.glb"}]}])
    monkeypatch.setattr(tencent, "call", lambda action, params, **kw: calls.append(action) or next(replies))
    data = b"glTF-model"
    if zipped:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as bundle:
            bundle.writestr("result/model.glb", data)
        payload = buffer.getvalue()
    else:
        payload = data
    monkeypatch.setattr(tencent.urllib.request, "urlopen", lambda url, timeout=0: Download(payload))
    out = tencent.shape(views(tmp_path), tmp_path / "out.glb", poll=0)
    assert out.read_bytes() == data
    assert calls == ["SubmitHunyuanTo3DProJob", "QueryHunyuanTo3DProJob", "QueryHunyuanTo3DProJob"]


def test_a_failed_job_says_why(tmp_path, monkeypatch):
    replies = iter([{"JobId": "j1"}, {"Status": "FAIL", "ErrorMessage": "image unclear"}])
    monkeypatch.setattr(tencent, "call", lambda action, params, **kw: next(replies))
    with pytest.raises(tencent.TencentError, match="image unclear"):
        tencent.shape(views(tmp_path), tmp_path / "out.glb", poll=0)


def test_an_api_error_carries_tencents_words(monkeypatch):
    monkeypatch.setenv("TENCENTCLOUD_SECRET_ID", "id")
    monkeypatch.setenv("TENCENTCLOUD_SECRET_KEY", "key")
    reply = json.dumps({"Response": {"Error": {"Code": "AuthFailure.SignatureFailure", "Message": "bad sig"},
                                     "RequestId": "r"}}).encode()
    monkeypatch.setattr(tencent.urllib.request, "urlopen", lambda request, timeout=0: Download(reply))
    with pytest.raises(tencent.TencentError, match="AuthFailure.SignatureFailure: bad sig"):
        tencent.call("QueryHunyuanTo3DProJob", {"JobId": "x"}, region="ap-singapore")
