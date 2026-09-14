"""Versioned source-bound bridge message contract."""

from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from ._base import (
    MAX_ACTION_BYTES,
    MAX_AUTHORITY_ITEMS,
    MAX_NATIVE_MESSAGE_BYTES,
    MAX_PAYLOAD_BYTES,
    MAX_PROVENANCE_BYTES,
    MAX_TIMEOUT_MS,
    PROTOCOL_VERSION,
    BridgeFrameError,
    BridgeMessageKind,
    BridgeOutcome,
    BridgePeer,
    BridgeProtocolError,
    _canonical_json_bytes,
    _freeze_json,
    _json_error_constant,
    _json_object_no_duplicates,
    _thaw_json,
    _validate_bounded_text,
    _validate_lower_hex,
)


@dataclass(frozen=True, slots=True)
class BridgeMessage:
    """One signed, source-bound bridge message.

    All identity and authority fields are included in the HMAC. ``payload`` is defensively frozen
    so mutable caller input cannot change after validation/signing.
    """

    sender: BridgePeer
    kind: BridgeMessageKind
    message_id: str
    task_id: str
    job_id: str
    target_id: str
    session_id: str
    action: str
    idempotency_key: str
    authority: tuple[str, ...]
    timeout_ms: int
    cancellation_id: str
    provenance_ref: str
    payload: Mapping[str, object]
    nonce: str
    key_id: str
    protocol: str = PROTOCOL_VERSION
    reply_to: str | None = None
    outcome: BridgeOutcome | None = None
    error_code: str | None = None
    signature: str = ""

    def __post_init__(self) -> None:
        if self.protocol != PROTOCOL_VERSION:
            raise BridgeProtocolError("unsupported bridge protocol version")
        if type(self.sender) is not BridgePeer or type(self.kind) is not BridgeMessageKind:
            raise BridgeProtocolError("sender and kind must use canonical bridge enums")

        for name in (
            "message_id",
            "task_id",
            "job_id",
            "target_id",
            "session_id",
            "idempotency_key",
            "cancellation_id",
            "key_id",
        ):
            _validate_bounded_text(getattr(self, name), name=name)
        _validate_bounded_text(self.action, name="action", maximum_bytes=MAX_ACTION_BYTES)
        _validate_bounded_text(
            self.provenance_ref,
            name="provenance_ref",
            maximum_bytes=MAX_PROVENANCE_BYTES,
        )
        _validate_lower_hex(self.nonce, name="nonce", length=32)

        if self.signature:
            _validate_lower_hex(self.signature, name="signature", length=64)
        if type(self.timeout_ms) is not int or not 1 <= self.timeout_ms <= MAX_TIMEOUT_MS:
            raise BridgeProtocolError("timeout_ms must be an exact bounded positive integer")

        if type(self.authority) not in {tuple, list}:
            raise BridgeProtocolError("authority must be a finite sequence")
        authority = tuple(self.authority)
        if not authority or len(authority) > MAX_AUTHORITY_ITEMS:
            raise BridgeProtocolError("authority must contain a bounded non-empty set")
        for index, item in enumerate(authority):
            _validate_bounded_text(item, name=f"authority[{index}]")
        if len(set(authority)) != len(authority):
            raise BridgeProtocolError("authority entries must be unique")
        object.__setattr__(self, "authority", authority)

        if type(self.payload) is dict:
            payload_source = self.payload
        elif isinstance(self.payload, MappingProxyType):
            payload_source = _thaw_json(self.payload)
        else:
            raise BridgeProtocolError("payload must be an exact JSON object")
        payload_bytes = _canonical_json_bytes(payload_source)
        if len(payload_bytes) > MAX_PAYLOAD_BYTES:
            raise BridgeProtocolError("payload exceeds the canonical UTF-8 byte bound")
        object.__setattr__(self, "payload", _freeze_json(payload_source))

        is_response = self.kind in {BridgeMessageKind.RESULT, BridgeMessageKind.ERROR}
        if is_response:
            if self.reply_to is None:
                raise BridgeProtocolError("result/error messages require reply_to")
            _validate_bounded_text(self.reply_to, name="reply_to")
            if type(self.outcome) is not BridgeOutcome:
                raise BridgeProtocolError("result/error messages require canonical outcome")
        else:
            if self.reply_to is not None or self.outcome is not None:
                raise BridgeProtocolError(
                    "request/event/cancel messages cannot carry response fields"
                )

        if self.kind is BridgeMessageKind.ERROR:
            if self.outcome is not BridgeOutcome.FAILED:
                raise BridgeProtocolError("error messages require failed outcome")
            if self.error_code is None:
                raise BridgeProtocolError("error messages require error_code")
            _validate_bounded_text(self.error_code, name="error_code")
        elif self.error_code is not None:
            raise BridgeProtocolError("only error messages may carry error_code")

    @classmethod
    def new(
        cls,
        *,
        sender: BridgePeer,
        kind: BridgeMessageKind,
        task_id: str,
        job_id: str,
        target_id: str,
        session_id: str,
        action: str,
        idempotency_key: str,
        authority: tuple[str, ...],
        timeout_ms: int,
        cancellation_id: str,
        provenance_ref: str,
        payload: dict[str, object],
        key_id: str,
        reply_to: str | None = None,
        outcome: BridgeOutcome | None = None,
        error_code: str | None = None,
    ) -> BridgeMessage:
        return cls(
            sender=sender,
            kind=kind,
            message_id=secrets.token_hex(16),
            task_id=task_id,
            job_id=job_id,
            target_id=target_id,
            session_id=session_id,
            action=action,
            idempotency_key=idempotency_key,
            authority=authority,
            timeout_ms=timeout_ms,
            cancellation_id=cancellation_id,
            provenance_ref=provenance_ref,
            payload=payload,
            nonce=secrets.token_hex(16),
            key_id=key_id,
            reply_to=reply_to,
            outcome=outcome,
            error_code=error_code,
        )

    def payload_dict(self) -> dict[str, object]:
        return _thaw_json(self.payload)  # type: ignore[return-value]

    def _wire_dict(self, *, include_signature: bool) -> dict[str, object]:
        data: dict[str, object] = {
            "protocol": self.protocol,
            "sender": self.sender.value,
            "kind": self.kind.value,
            "message_id": self.message_id,
            "task_id": self.task_id,
            "job_id": self.job_id,
            "target_id": self.target_id,
            "session_id": self.session_id,
            "action": self.action,
            "idempotency_key": self.idempotency_key,
            "authority": list(self.authority),
            "timeout_ms": self.timeout_ms,
            "cancellation_id": self.cancellation_id,
            "provenance_ref": self.provenance_ref,
            "payload": self.payload_dict(),
            "nonce": self.nonce,
            "key_id": self.key_id,
            "reply_to": self.reply_to,
            "outcome": None if self.outcome is None else self.outcome.value,
            "error_code": self.error_code,
        }
        if include_signature:
            data["signature"] = self.signature
        return data

    def canonical_unsigned_bytes(self) -> bytes:
        return _canonical_json_bytes(self._wire_dict(include_signature=False))

    def to_json_bytes(self) -> bytes:
        encoded = _canonical_json_bytes(self._wire_dict(include_signature=True))
        if len(encoded) > MAX_NATIVE_MESSAGE_BYTES:
            raise BridgeFrameError("encoded bridge message exceeds the Native Messaging bound")
        return encoded

    @property
    def logical_fingerprint(self) -> str:
        logical = {
            "protocol": self.protocol,
            "sender": self.sender.value,
            "kind": self.kind.value,
            "task_id": self.task_id,
            "job_id": self.job_id,
            "target_id": self.target_id,
            "session_id": self.session_id,
            "action": self.action,
            "idempotency_key": self.idempotency_key,
            "authority": list(self.authority),
            "timeout_ms": self.timeout_ms,
            "cancellation_id": self.cancellation_id,
            "provenance_ref": self.provenance_ref,
            "payload": self.payload_dict(),
            "outcome": None if self.outcome is None else self.outcome.value,
            "error_code": self.error_code,
        }
        return hashlib.sha256(_canonical_json_bytes(logical)).hexdigest()

    @classmethod
    def from_json_bytes(cls, data: bytes) -> BridgeMessage:
        if type(data) is not bytes:
            raise BridgeProtocolError("bridge JSON input must be exact bytes")
        if not data or len(data) > MAX_NATIVE_MESSAGE_BYTES:
            raise BridgeFrameError("bridge JSON input is empty or exceeds the message bound")
        try:
            text = data.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise BridgeProtocolError("bridge message is not valid UTF-8") from exc
        try:
            parsed = json.loads(
                text,
                object_pairs_hook=_json_object_no_duplicates,
                parse_constant=_json_error_constant,
            )
        except BridgeProtocolError:
            raise
        except (ValueError, RecursionError) as exc:
            raise BridgeProtocolError("bridge message is not valid bounded JSON") from exc
        if type(parsed) is not dict:
            raise BridgeProtocolError("bridge message root must be an object")

        expected = {
            "protocol",
            "sender",
            "kind",
            "message_id",
            "task_id",
            "job_id",
            "target_id",
            "session_id",
            "action",
            "idempotency_key",
            "authority",
            "timeout_ms",
            "cancellation_id",
            "provenance_ref",
            "payload",
            "nonce",
            "key_id",
            "reply_to",
            "outcome",
            "error_code",
            "signature",
        }
        if set(parsed) != expected:
            raise BridgeProtocolError("bridge message fields do not match the v1 schema")
        try:
            sender = BridgePeer(parsed["sender"])
            kind = BridgeMessageKind(parsed["kind"])
            outcome_raw = parsed["outcome"]
            outcome = None if outcome_raw is None else BridgeOutcome(outcome_raw)
        except (TypeError, ValueError) as exc:
            raise BridgeProtocolError("bridge enum field is invalid") from exc
        authority_raw = parsed["authority"]
        if type(authority_raw) is not list:
            raise BridgeProtocolError("authority must be a JSON array")
        payload_raw = parsed["payload"]
        if type(payload_raw) is not dict:
            raise BridgeProtocolError("payload must be a JSON object")
        return cls(
            protocol=parsed["protocol"],
            sender=sender,
            kind=kind,
            message_id=parsed["message_id"],
            task_id=parsed["task_id"],
            job_id=parsed["job_id"],
            target_id=parsed["target_id"],
            session_id=parsed["session_id"],
            action=parsed["action"],
            idempotency_key=parsed["idempotency_key"],
            authority=authority_raw,
            timeout_ms=parsed["timeout_ms"],
            cancellation_id=parsed["cancellation_id"],
            provenance_ref=parsed["provenance_ref"],
            payload=payload_raw,
            nonce=parsed["nonce"],
            key_id=parsed["key_id"],
            reply_to=parsed["reply_to"],
            outcome=outcome,
            error_code=parsed["error_code"],
            signature=parsed["signature"],
        )
