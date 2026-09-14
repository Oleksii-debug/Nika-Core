"""Shared-secret HMAC authentication for bridge messages."""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping
from dataclasses import replace

from ._base import (
    MAX_SHARED_KEYS,
    MIN_SHARED_SECRET_BYTES,
    BridgeAuthenticationError,
    _validate_bounded_text,
)
from .message import BridgeMessage


class BridgeAuthenticator:
    """HMAC-SHA256 authentication with bounded key rotation and no secret serialization."""

    def __init__(self, keys: Mapping[str, bytes]) -> None:
        if not isinstance(keys, Mapping) or not 1 <= len(keys) <= MAX_SHARED_KEYS:
            raise ValueError("bridge authenticator requires a bounded non-empty key mapping")
        copied: dict[str, bytes] = {}
        for key_id, secret in keys.items():
            _validate_bounded_text(key_id, name="key_id")
            if type(secret) is not bytes or len(secret) < MIN_SHARED_SECRET_BYTES:
                raise ValueError("bridge shared secrets must be exact bytes with at least 32 bytes")
            copied[key_id] = bytes(secret)
        self._keys = copied

    def sign(self, message: BridgeMessage) -> BridgeMessage:
        secret = self._keys.get(message.key_id)
        if secret is None:
            raise BridgeAuthenticationError("bridge message authentication failed")
        unsigned = replace(message, signature="")
        digest = hmac.new(secret, unsigned.canonical_unsigned_bytes(), hashlib.sha256).hexdigest()
        return replace(unsigned, signature=digest)

    def verify(self, message: BridgeMessage) -> None:
        secret = self._keys.get(message.key_id)
        key_known = secret is not None
        verification_secret = secret if secret is not None else (b"\x00" * MIN_SHARED_SECRET_BYTES)
        supplied = message.signature if message.signature else ("0" * 64)
        expected = hmac.new(
            verification_secret,
            replace(message, signature="").canonical_unsigned_bytes(),
            hashlib.sha256,
        ).hexdigest()
        signature_matches = hmac.compare_digest(expected, supplied)
        if not key_known or not message.signature or not signature_matches:
            raise BridgeAuthenticationError("bridge message authentication failed")
