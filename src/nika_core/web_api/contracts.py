from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Final

_MAX_COMMAND_BYTES: Final = 64 * 1024
_MAX_JSON_NODES: Final = 2048
_MAX_JSON_DEPTH: Final = 32
_MAX_TEXT_CHARS: Final = 64 * 1024
_MAX_TEXT_BYTES: Final = 16 * 1024
_MAX_KEY_BYTES: Final = 256
_MAX_INT_BITS: Final = 4096
_RESULT_STATUSES: Final = frozenset({"accepted", "completed", "rejected", "failed"})


def _machine_text(value: object, *, field_name: str, max_bytes: int = 160) -> str:
    if type(value) is not str:
        raise ValueError(f"{field_name} must be an exact string")
    if not value or value != value.strip():
        raise ValueError(f"{field_name} must be non-empty without surrounding whitespace")
    if len(value) > _MAX_TEXT_CHARS:
        raise ValueError(f"{field_name} is too long")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{field_name} must be valid UTF-8 text") from exc
    if len(encoded) > max_bytes:
        raise ValueError(f"{field_name} exceeds {max_bytes} UTF-8 bytes")
    if any(ch.isspace() or not ch.isprintable() for ch in value):
        raise ValueError(f"{field_name} contains unsupported whitespace or control text")
    return value


def _opaque_identity(value: object, *, field_name: str, max_bytes: int = 160) -> str:
    if type(value) is not str:
        raise ValueError(f"{field_name} must be an exact string")
    if not value or value != value.strip():
        raise ValueError(f"{field_name} must be non-empty without surrounding whitespace")
    if len(value) > _MAX_TEXT_CHARS:
        raise ValueError(f"{field_name} is too long")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{field_name} must be valid UTF-8 text") from exc
    if len(encoded) > max_bytes:
        raise ValueError(f"{field_name} exceeds {max_bytes} UTF-8 bytes")
    if any(not ch.isprintable() for ch in value):
        raise ValueError(f"{field_name} contains control text")
    return value


def _human_text(value: object, *, field_name: str, max_bytes: int) -> str:
    if type(value) is not str:
        raise ValueError(f"{field_name} must be an exact string")
    if len(value) > _MAX_TEXT_CHARS:
        raise ValueError(f"{field_name} is too long")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{field_name} must be valid UTF-8 text") from exc
    if len(encoded) > max_bytes:
        raise ValueError(f"{field_name} exceeds {max_bytes} UTF-8 bytes")
    return value


def _snapshot_json_object(value: object) -> str:
    nodes = [0]

    def snapshot(item: object, *, depth: int) -> object:
        if depth > _MAX_JSON_DEPTH:
            raise ValueError("JSON payload exceeds maximum depth")
        nodes[0] += 1
        if nodes[0] > _MAX_JSON_NODES:
            raise ValueError("JSON payload exceeds maximum node count")

        if item is None or type(item) is bool:
            return item
        if type(item) is int:
            if item.bit_length() > _MAX_INT_BITS:
                raise ValueError("JSON integer exceeds supported size")
            return item
        if type(item) is float:
            if not math.isfinite(item):
                raise ValueError("JSON number must be finite")
            return item
        if type(item) is str:
            if len(item) > _MAX_TEXT_CHARS:
                raise ValueError("JSON string is too long")
            try:
                encoded = item.encode("utf-8", errors="strict")
            except UnicodeEncodeError as exc:
                raise ValueError("JSON string must be valid UTF-8 text") from exc
            if len(encoded) > _MAX_TEXT_BYTES:
                raise ValueError("JSON string exceeds supported UTF-8 size")
            return item
        if type(item) is list:
            return [snapshot(child, depth=depth + 1) for child in item]
        if type(item) is dict:
            result: dict[str, object] = {}
            for key, child in item.items():
                if type(key) is not str:
                    raise ValueError("JSON object keys must be exact strings")
                if not key:
                    raise ValueError("JSON object keys must not be empty")
                if len(key) > _MAX_TEXT_CHARS:
                    raise ValueError("JSON object key is too long")
                try:
                    encoded_key = key.encode("utf-8", errors="strict")
                except UnicodeEncodeError as exc:
                    raise ValueError("JSON object key must be valid UTF-8 text") from exc
                if len(encoded_key) > _MAX_KEY_BYTES:
                    raise ValueError("JSON object key exceeds supported UTF-8 size")
                result[key] = snapshot(child, depth=depth + 1)
            return result
        raise ValueError("payload must contain only exact JSON value carriers")

    if type(value) is not dict:
        raise ValueError("payload must be an exact JSON object")
    detached = snapshot(value, depth=0)
    encoded = json.dumps(
        detached,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > _MAX_COMMAND_BYTES:
        raise ValueError("payload exceeds 64 KiB")
    return encoded.decode("utf-8")


@dataclass(frozen=True, slots=True)
class WebPrincipal:
    """Server-established authority identity for a Web/Cloud request."""

    tenant_id: str
    user_id: str
    workspace_id: str
    session_id: str

    def __post_init__(self) -> None:
        for name in ("tenant_id", "user_id", "workspace_id", "session_id"):
            _opaque_identity(getattr(self, name), field_name=name)


@dataclass(frozen=True, slots=True)
class WebCommand:
    """Detached command contract admitted from an untrusted client."""

    request_id: str
    action_id: str
    _payload_json: str = field(repr=False)

    @classmethod
    def from_untrusted(cls, value: object) -> WebCommand:
        if type(value) is not dict:
            raise ValueError("command must be an exact object")
        allowed = {"request_id", "action_id", "payload"}
        if len(value) != len(allowed):
            raise ValueError("command must contain exactly request_id, action_id and payload")
        keys = tuple(value)
        if any(type(key) is not str for key in keys):
            raise ValueError("command keys must be exact strings")
        if set(keys) != allowed:
            raise ValueError("command must contain exactly request_id, action_id and payload")

        request_id = _machine_text(
            value["request_id"],
            field_name="request_id",
            max_bytes=120,
        )
        action_id = _machine_text(
            value["action_id"],
            field_name="action_id",
            max_bytes=120,
        )
        if "." not in action_id:
            raise ValueError("action_id must be a stable dotted identifier")
        payload_json = _snapshot_json_object(value["payload"])
        return cls(
            request_id=request_id,
            action_id=action_id,
            _payload_json=payload_json,
        )

    @property
    def payload(self) -> dict[str, object]:
        value = json.loads(self._payload_json)
        if type(value) is not dict:
            raise RuntimeError("stored command payload lost object identity")
        return value


@dataclass(frozen=True, slots=True)
class WebCommandResult:
    """Presentation-neutral result safe for a future HTTP/event adapter."""

    request_id: str
    status: str
    code: str
    message: str
    _data_json: str = field(default="{}", repr=False)

    def __post_init__(self) -> None:
        _machine_text(self.request_id, field_name="request_id", max_bytes=120)
        status = _machine_text(self.status, field_name="status", max_bytes=32)
        if status not in _RESULT_STATUSES:
            raise ValueError("unsupported Web command result status")
        _machine_text(self.code, field_name="code", max_bytes=120)
        _human_text(self.message, field_name="message", max_bytes=4000)
        if type(self._data_json) is not str:
            raise ValueError("result data must use the canonical JSON carrier")
        if len(self._data_json) > _MAX_COMMAND_BYTES:
            raise ValueError("result data exceeds the supported carrier size")
        try:
            encoded = self._data_json.encode("utf-8", errors="strict")
            parsed = json.loads(self._data_json)
        except (UnicodeEncodeError, json.JSONDecodeError, RecursionError) as exc:
            raise ValueError("result data must be valid bounded JSON") from exc
        if len(encoded) > _MAX_COMMAND_BYTES:
            raise ValueError("result data exceeds 64 KiB")
        canonical = _snapshot_json_object(parsed)
        if canonical != self._data_json:
            raise ValueError("result data must use canonical JSON encoding")

    @classmethod
    def create(
        cls,
        *,
        request_id: str,
        status: str,
        code: str,
        message: str,
        data: object | None = None,
    ) -> WebCommandResult:
        data_json = _snapshot_json_object({} if data is None else data)
        return cls(
            request_id=request_id,
            status=status,
            code=code,
            message=message,
            _data_json=data_json,
        )

    @property
    def data(self) -> dict[str, object]:
        value = json.loads(self._data_json)
        if type(value) is not dict:
            raise RuntimeError("stored result data lost object identity")
        return value
