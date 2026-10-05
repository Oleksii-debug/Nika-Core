"""Strict bounded JSON admission for persisted V0.1 Windows settings.

Raw JSON digests remain the authority for accepted per-task identities. This
reader only checks that the bytes have one unambiguous, finite interpretation.
"""

from __future__ import annotations

import json
import math
from typing import Any


def _unique_object_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate stored JSON key")
        result[key] = value
    return result


def _finite_float(raw: str) -> float:
    value = float(raw)
    if not math.isfinite(value):
        raise ValueError("nonfinite stored JSON number")
    return value


def _reject_nonfinite_constant(_raw: str) -> None:
    raise ValueError("nonfinite stored JSON constant")


def load_persisted_json_object(value: str, *, max_bytes: int) -> dict[str, Any]:
    """Parse SQLite TEXT once; reject ambiguous, invalid or oversized JSON.

    The caller translates errors to its established private-data-safe UI error.
    The caller must retain/hash the original text rather than reserialize the
    parsed mapping when checking a previously accepted selection identifier.
    """
    if type(value) is not str:
        raise TypeError("stored JSON must be text")
    if len(value) > max_bytes or len(value.encode("utf-8")) > max_bytes:
        raise ValueError("oversized stored JSON")
    decoded = json.loads(
        value,
        object_pairs_hook=_unique_object_keys,
        parse_float=_finite_float,
        parse_constant=_reject_nonfinite_constant,
    )
    if type(decoded) is not dict:
        raise ValueError("stored JSON must be an object")
    return decoded
