"""Shared constants, enums, errors, and canonical JSON validation for the Autopilot bridge."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType


PROTOCOL_VERSION = "nika.autopilot.native/v1"
MAX_NATIVE_MESSAGE_BYTES = 1_048_576
MAX_PAYLOAD_BYTES = 786_432
MAX_IDENTIFIER_BYTES = 256
MAX_ACTION_BYTES = 256
MAX_PROVENANCE_BYTES = 2_048
MAX_AUTHORITY_ITEMS = 32
MAX_JSON_DEPTH = 16
MAX_JSON_CONTAINER_ITEMS = 1_024
MAX_JSON_NODES = 8_192
MAX_TIMEOUT_MS = 86_400_000
MIN_SHARED_SECRET_BYTES = 32
MAX_SHARED_KEYS = 8
DEFAULT_REPLAY_CAPACITY = 4_096


class BridgeProtocolError(ValueError):
    """Base fail-closed bridge protocol error."""


class BridgeFrameError(BridgeProtocolError):
    """Native Messaging frame is malformed or outside the configured bound."""


class BridgeAuthenticationError(BridgeProtocolError):
    """Message authentication failed without exposing key/signature detail."""


class BridgeDirectionError(BridgeProtocolError):
    """Authenticated message arrived from an unexpected peer."""


class BridgeReplayError(BridgeProtocolError):
    """Base replay/idempotency admission failure."""


class BridgeReplayConflictError(BridgeReplayError):
    """An idempotency key was reused for different logical input."""


class BridgeReplayUncertainError(BridgeReplayError):
    """A prior attempt is still pending/uncertain and must not be replayed."""


class BridgeReplayCapacityError(BridgeReplayError):
    """The bounded process-lifetime replay guard is full and fails closed."""


class BridgePeer(StrEnum):
    NIKA = "nika"
    AUTOPILOT = "autopilot"


class BridgeMessageKind(StrEnum):
    REQUEST = "request"
    RESULT = "result"
    EVENT = "event"
    CANCEL = "cancel"
    ERROR = "error"


class BridgeOutcome(StrEnum):
    ACCEPTED = "accepted"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class _ReplayStatus(StrEnum):
    PENDING = "pending"
    COMPLETED = "completed"


def _validate_bounded_text(
    value: object,
    *,
    name: str,
    maximum_bytes: int = MAX_IDENTIFIER_BYTES,
) -> str:
    if type(value) is not str:
        raise BridgeProtocolError(f"{name} must be text")
    if not value or value != value.strip():
        raise BridgeProtocolError(f"{name} must be non-empty trimmed text")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise BridgeProtocolError(f"{name} must not contain control characters")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise BridgeProtocolError(f"{name} must be valid UTF-8 text") from exc
    if len(encoded) > maximum_bytes:
        raise BridgeProtocolError(f"{name} exceeds the UTF-8 byte bound")
    return value


def _validate_lower_hex(value: object, *, name: str, length: int) -> str:
    text = _validate_bounded_text(value, name=name, maximum_bytes=length)
    if len(text) != length or any(char not in "0123456789abcdef" for char in text):
        raise BridgeProtocolError(f"{name} must be {length} lowercase hexadecimal characters")
    return text


def _json_error_constant(value: str) -> None:
    raise BridgeProtocolError(f"non-finite JSON number is forbidden: {value}")


def _json_object_no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise BridgeProtocolError("duplicate JSON object key is forbidden")
        result[key] = value
    return result


def _validate_json_value(
    value: object,
    *,
    depth: int = 0,
    counter: list[int] | None = None,
) -> None:
    if counter is None:
        counter = [0]
    counter[0] += 1
    if counter[0] > MAX_JSON_NODES:
        raise BridgeProtocolError("JSON payload exceeds the node bound")
    if depth > MAX_JSON_DEPTH:
        raise BridgeProtocolError("JSON payload exceeds the nesting-depth bound")

    if value is None or type(value) is bool or type(value) is str:
        return
    if type(value) is int:
        if not -(2**63) <= value <= (2**63 - 1):
            raise BridgeProtocolError("JSON integer exceeds the signed 64-bit bound")
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise BridgeProtocolError("non-finite JSON number is forbidden")
        return
    if type(value) is list or type(value) is tuple:
        if len(value) > MAX_JSON_CONTAINER_ITEMS:
            raise BridgeProtocolError("JSON array exceeds the item bound")
        for item in value:
            _validate_json_value(item, depth=depth + 1, counter=counter)
        return
    if type(value) is dict:
        if len(value) > MAX_JSON_CONTAINER_ITEMS:
            raise BridgeProtocolError("JSON object exceeds the item bound")
        for key, item in value.items():
            _validate_bounded_text(key, name="payload key")
            _validate_json_value(item, depth=depth + 1, counter=counter)
        return
    raise BridgeProtocolError("payload must contain only canonical JSON value types")


def _freeze_json(value: object) -> object:
    if type(value) is dict:
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if type(value) is list or type(value) is tuple:
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if type(value) is tuple:
        return [_thaw_json(item) for item in value]
    return value


def _canonical_json_bytes(value: object) -> bytes:
    _validate_json_value(value)
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise BridgeProtocolError("value is not canonical bridge JSON") from exc
    return encoded
