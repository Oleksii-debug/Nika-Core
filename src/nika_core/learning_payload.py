from __future__ import annotations

import hashlib
import json
import math


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate semantic learning JSON field")
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> object:
    raise ValueError("non-finite semantic learning JSON constant")


def _finite_json_float(raw: str) -> float:
    value = float(raw)
    if not math.isfinite(value):
        raise ValueError("non-finite semantic learning JSON float")
    return value


def decode_learning_json_payload(payload: bytes) -> object:
    """Decode transient Loop-B payload bytes through one strict JSON authority."""

    if type(payload) is not bytes:
        raise TypeError("payload must be exact built-in bytes")
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError("learning payload must be valid UTF-8 JSON") from exc
    try:
        return json.loads(
            text,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
            parse_float=_finite_json_float,
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("learning payload must be strict JSON") from exc


def durable_learning_value_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
