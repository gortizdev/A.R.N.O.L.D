import time

import pytest

from arnold.security import (
    AuthError,
    CommandVerifier,
    NonceCache,
    build_command,
    canonical_bytes,
    sign,
)

SECRET = "0123456789abcdef0123456789abcdef"


def verifier(**kwargs):
    defaults = dict(require_signature=True, max_skew_seconds=120, allow_destructive=False)
    defaults.update(kwargs)
    return CommandVerifier(SECRET, **defaults)


def test_canonical_bytes_is_key_order_independent():
    a = {"cmd": "query.cpu", "id": "1", "ts": 5}
    b = {"ts": 5, "id": "1", "cmd": "query.cpu"}
    assert canonical_bytes(a) == canonical_bytes(b)


def test_canonical_bytes_excludes_signature():
    payload = {"cmd": "query.cpu", "ts": 1}
    assert canonical_bytes(payload) == canonical_bytes({**payload, "sig": "deadbeef"})


def test_valid_command_passes():
    verifier().verify(build_command(SECRET, "query.cpu"))


def test_tampered_args_rejected():
    envelope = build_command(SECRET, "control.launch", {"name": "steam"})
    envelope["args"]["name"] = "evil.exe"
    with pytest.raises(AuthError, match="signature mismatch"):
        verifier().verify(envelope)


def test_tampered_command_rejected():
    envelope = build_command(SECRET, "query.cpu")
    envelope["cmd"] = "control.shutdown"
    with pytest.raises(AuthError, match="signature mismatch"):
        verifier(allow_destructive=True).verify(envelope)


def test_reply_to_is_covered_by_signature():
    """Otherwise an attacker could redirect a valid command's answer."""
    envelope = build_command(SECRET, "desktop.clipboard_get", reply_to="a/legit/topic")
    envelope["reply_to"] = "attacker/topic"
    with pytest.raises(AuthError, match="signature mismatch"):
        verifier().verify(envelope)


def test_speak_flag_is_covered_by_signature():
    envelope = build_command(SECRET, "query.cpu")
    envelope["speak"] = True
    with pytest.raises(AuthError, match="signature mismatch"):
        verifier().verify(envelope)


def test_wrong_secret_rejected():
    envelope = build_command("a-different-secret-entirely", "query.cpu")
    with pytest.raises(AuthError, match="signature mismatch"):
        verifier().verify(envelope)


def test_unsigned_command_rejected_when_signature_required():
    envelope = build_command("", "query.cpu")
    with pytest.raises(AuthError, match="unsigned"):
        verifier().verify(envelope)


def test_stale_timestamp_rejected():
    envelope = build_command(SECRET, "query.cpu")
    envelope["ts"] = time.time() - 3600
    envelope["sig"] = sign(SECRET, envelope)
    with pytest.raises(AuthError, match="outside the allowed"):
        verifier().verify(envelope)


def test_future_timestamp_rejected():
    envelope = build_command(SECRET, "query.cpu")
    envelope["ts"] = time.time() + 3600
    envelope["sig"] = sign(SECRET, envelope)
    with pytest.raises(AuthError, match="outside the allowed"):
        verifier().verify(envelope)


def test_replay_rejected():
    check = verifier()
    envelope = build_command(SECRET, "query.cpu")
    check.verify(envelope)
    with pytest.raises(AuthError, match="replayed"):
        check.verify(envelope)


def test_destructive_blocked_by_default():
    envelope = build_command(SECRET, "control.shutdown")
    with pytest.raises(AuthError, match="destructive"):
        verifier().verify(envelope)


def test_destructive_allowed_when_enabled():
    verifier(allow_destructive=True).verify(build_command(SECRET, "control.shutdown"))


def test_destructive_still_needs_valid_signature():
    envelope = build_command("wrong-secret", "control.shutdown")
    with pytest.raises(AuthError, match="signature mismatch"):
        verifier(allow_destructive=True).verify(envelope)


def test_unsigned_mode_still_gates_destructive():
    """Turning signatures off must not also open up shutdown."""
    check = CommandVerifier("", require_signature=False, allow_destructive=False)
    check.verify({"cmd": "query.cpu"})
    with pytest.raises(AuthError, match="destructive"):
        check.verify({"cmd": "control.shutdown"})


def test_missing_cmd_rejected():
    with pytest.raises(AuthError, match="missing a 'cmd'"):
        verifier().verify({"args": {}})


def test_non_dict_args_rejected():
    envelope = build_command(SECRET, "query.cpu")
    envelope["args"] = ["not", "a", "dict"]
    envelope["sig"] = sign(SECRET, envelope)
    with pytest.raises(AuthError, match="must be a JSON object"):
        verifier().verify(envelope)


class TestNonceCache:
    def test_first_use_accepted_repeat_rejected(self):
        cache = NonceCache(ttl_seconds=60)
        assert cache.check_and_add("abc") is True
        assert cache.check_and_add("abc") is False

    def test_distinct_nonces_independent(self):
        cache = NonceCache(ttl_seconds=60)
        assert cache.check_and_add("a") is True
        assert cache.check_and_add("b") is True

    def test_expired_nonce_forgotten(self):
        cache = NonceCache(ttl_seconds=10)
        now = 1000.0
        assert cache.check_and_add("abc", now=now) is True
        # Past the TTL the entry is pruned; the skew check is what stops a real
        # replay this late, not the cache.
        assert cache.check_and_add("abc", now=now + 100) is True

    def test_cache_is_bounded(self):
        cache = NonceCache(ttl_seconds=10_000, max_entries=50)
        for i in range(500):
            cache.check_and_add(f"nonce-{i}", now=1000.0)
        assert len(cache._seen) <= 51
