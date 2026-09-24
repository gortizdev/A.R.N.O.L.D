"""Authentication for inbound commands.

Anything on the LAN can publish to a Mosquitto topic once it holds the broker
password, and Jarvis's own :8765 API has no auth at all. So commands carry
their own HMAC-SHA256 signature over a canonical encoding of the envelope,
plus a timestamp and a nonce, giving tamper detection and replay protection
independent of the transport.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
import uuid
from collections import OrderedDict
from typing import Any

# Commands that can lose work or take the machine off the network. Gated behind
# security.allow_destructive in addition to a valid signature.
DESTRUCTIVE_COMMANDS = frozenset(
    {
        "control.shutdown",
        "control.restart",
        "control.sleep",
        "control.hibernate",
        "control.logoff",
        "control.kill_process",
        "control.run_script",
    }
)


class AuthError(Exception):
    """Raised when an inbound command fails authentication."""


def generate_secret(nbytes: int = 32) -> str:
    return secrets.token_hex(nbytes)


def canonical_bytes(payload: dict[str, Any]) -> bytes:
    """Deterministic encoding of an envelope, so both ends sign the same bytes.

    Sorted keys, no insignificant whitespace, UTF-8. The ``sig`` field is
    excluded - it is the output, not an input.
    """
    body = {k: v for k, v in payload.items() if k != "sig"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def sign(secret: str, payload: dict[str, Any]) -> str:
    return hmac.new(secret.encode("utf-8"), canonical_bytes(payload), hashlib.sha256).hexdigest()


def build_command(
    secret: str,
    cmd: str,
    args: dict[str, Any] | None = None,
    *,
    reply_to: str = "",
    speak: bool = False,
) -> dict[str, Any]:
    """Build a signed command envelope ready to publish.

    Every field is added before signing, so `reply_to` and `speak` are covered
    too - otherwise an attacker could redirect a reply or force speech on an
    otherwise-authentic command.
    """
    envelope: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "ts": time.time(),
        "cmd": cmd,
        "args": args or {},
        "nonce": secrets.token_hex(8),
    }
    if reply_to:
        envelope["reply_to"] = reply_to
    if speak:
        envelope["speak"] = True
    if secret:
        envelope["sig"] = sign(secret, envelope)
    return envelope


class NonceCache:
    """Remembers recently seen nonces so a captured command cannot be replayed."""

    def __init__(self, ttl_seconds: float = 300.0, max_entries: int = 4096) -> None:
        self._ttl = ttl_seconds
        self._max = max_entries
        self._seen: OrderedDict[str, float] = OrderedDict()

    def _prune(self, now: float) -> None:
        cutoff = now - self._ttl
        while self._seen:
            key, seen_at = next(iter(self._seen.items()))
            if seen_at >= cutoff:
                break
            self._seen.popitem(last=False)
        while len(self._seen) > self._max:
            self._seen.popitem(last=False)

    def check_and_add(self, nonce: str, now: float | None = None) -> bool:
        """Return True if `nonce` is new; False if it has been seen already."""
        now = time.time() if now is None else now
        self._prune(now)
        if nonce in self._seen:
            return False
        self._seen[nonce] = now
        return True


class CommandVerifier:
    def __init__(
        self,
        secret: str,
        *,
        require_signature: bool = True,
        max_skew_seconds: float = 120.0,
        allow_destructive: bool = False,
    ) -> None:
        self._secret = secret
        self._require = require_signature
        self._skew = max_skew_seconds
        self._allow_destructive = allow_destructive
        self._nonces = NonceCache(ttl_seconds=max(300.0, max_skew_seconds * 2))

    def verify(self, envelope: dict[str, Any], *, now: float | None = None) -> None:
        """Raise AuthError unless `envelope` is a well-formed, authentic command."""
        now = time.time() if now is None else now

        if not isinstance(envelope, dict):
            raise AuthError("command envelope must be a JSON object")
        cmd = envelope.get("cmd")
        if not isinstance(cmd, str) or not cmd:
            raise AuthError("command envelope is missing a 'cmd' string")
        if not isinstance(envelope.get("args", {}), dict):
            raise AuthError("'args' must be a JSON object")

        if self._require:
            if not self._secret:
                raise AuthError("signature required but no shared secret is configured")

            sig = envelope.get("sig")
            if not isinstance(sig, str) or not sig:
                raise AuthError("command is unsigned but security.require_signature is true")

            expected = sign(self._secret, envelope)
            if not hmac.compare_digest(expected, sig):
                raise AuthError("signature mismatch")

            ts = envelope.get("ts")
            if not isinstance(ts, (int, float)):
                raise AuthError("command envelope is missing a numeric 'ts'")
            if abs(now - float(ts)) > self._skew:
                raise AuthError(
                    f"timestamp is outside the allowed {self._skew:.0f}s window "
                    f"(off by {abs(now - float(ts)):.0f}s) - check clock sync"
                )

            nonce = envelope.get("nonce")
            if not isinstance(nonce, str) or not nonce:
                raise AuthError("command envelope is missing a 'nonce'")
            if not self._nonces.check_and_add(nonce, now=now):
                raise AuthError("nonce already used - replayed command rejected")

        if cmd in DESTRUCTIVE_COMMANDS and not self._allow_destructive:
            raise AuthError(
                f"{cmd} is a destructive command and security.allow_destructive is false"
            )
