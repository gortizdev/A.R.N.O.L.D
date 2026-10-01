import pytest


@pytest.fixture(autouse=True)
def _no_real_api_key(monkeypatch):
    """No test reaches a paid API by accident: the key this PC has in its
    environment is taken away, and a test that wants one sets its own."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
