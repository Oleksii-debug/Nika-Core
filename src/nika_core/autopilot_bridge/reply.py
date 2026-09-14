"""Structured bridge handler reply contract."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from ._base import (
    MAX_PAYLOAD_BYTES,
    MAX_PROVENANCE_BYTES,
    BridgeOutcome,
    BridgeProtocolError,
    _canonical_json_bytes,
    _freeze_json,
    _thaw_json,
    _validate_bounded_text,
)


@dataclass(frozen=True, slots=True)
class BridgeReply:
    outcome: BridgeOutcome
    payload: Mapping[str, object]
    provenance_ref: str
    error_code: str | None = None

    def __post_init__(self) -> None:
        if type(self.outcome) is not BridgeOutcome:
            raise BridgeProtocolError("reply outcome must use canonical BridgeOutcome")
        _validate_bounded_text(
            self.provenance_ref,
            name="reply provenance_ref",
            maximum_bytes=MAX_PROVENANCE_BYTES,
        )
        if type(self.payload) is dict:
            payload_source = self.payload
        elif isinstance(self.payload, MappingProxyType):
            payload_source = _thaw_json(self.payload)
        else:
            raise BridgeProtocolError("reply payload must be an exact JSON object")
        payload_bytes = _canonical_json_bytes(payload_source)
        if len(payload_bytes) > MAX_PAYLOAD_BYTES:
            raise BridgeProtocolError("reply payload exceeds the byte bound")
        object.__setattr__(self, "payload", _freeze_json(payload_source))
        if self.outcome is BridgeOutcome.FAILED:
            if self.error_code is None:
                raise BridgeProtocolError("failed reply requires error_code")
            _validate_bounded_text(self.error_code, name="reply error_code")
        elif self.error_code is not None:
            raise BridgeProtocolError("non-failed reply cannot carry error_code")

    def payload_dict(self) -> dict[str, object]:
        return _thaw_json(self.payload)  # type: ignore[return-value]

    @classmethod
    def succeeded(
        cls,
        payload: dict[str, object],
        *,
        provenance_ref: str,
    ) -> "BridgeReply":
        return cls(
            outcome=BridgeOutcome.SUCCEEDED,
            payload=payload,
            provenance_ref=provenance_ref,
        )

    @classmethod
    def failed(
        cls,
        error_code: str,
        *,
        provenance_ref: str,
        payload: dict[str, object] | None = None,
    ) -> "BridgeReply":
        return cls(
            outcome=BridgeOutcome.FAILED,
            payload={} if payload is None else payload,
            provenance_ref=provenance_ref,
            error_code=error_code,
        )
