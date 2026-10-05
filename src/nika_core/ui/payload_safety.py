"""Bound the JSON-like input crossing the untrusted desktop WebView boundary."""

from __future__ import annotations

import json
import math
from typing import Any

_MAX_PAYLOAD_BYTES = 1_048_576
_MAX_PAYLOAD_NODES = 10_000
_MAX_PAYLOAD_DEPTH = 32
_MAX_INTEGER_BITS = 4_096


def validate_ui_payload(payload: object) -> dict[str, Any]:
    """Admit finite, strictly JSON-shaped commands within a compact UTF-8 budget.

    Iterative traversal bounds hostile nesting without recursively copying or
    serializing an arbitrarily large Python object. The byte budget counts the
    exact compact JSON representation, including escaping and punctuation.
    """
    if type(payload) is not dict:
        raise ValueError("UI command payload must be a JSON object")

    nodes = 0
    byte_count = 0
    active: set[int] = set()
    stack: list[tuple[object, int, bool]] = [(payload, 0, False)]

    def charge(amount: int) -> None:
        nonlocal byte_count
        byte_count += amount
        if byte_count > _MAX_PAYLOAD_BYTES:
            raise ValueError("UI command payload exceeds the byte limit")

    while stack:
        item, depth, leaving = stack.pop()
        if leaving:
            active.remove(id(item))
            continue

        nodes += 1
        if nodes > _MAX_PAYLOAD_NODES:
            raise ValueError("UI command payload exceeds the element limit")
        if depth > _MAX_PAYLOAD_DEPTH:
            raise ValueError("UI command payload exceeds the nesting limit")

        if type(item) is dict or type(item) is list:
            if id(item) in active:
                raise ValueError("UI command payload contains a container cycle")
            if len(item) > _MAX_PAYLOAD_NODES - nodes:
                raise ValueError("UI command payload exceeds the element limit")
            active.add(id(item))
            stack.append((item, depth, True))
            charge(2 + (len(item) - 1 if item else 0))
            if type(item) is dict:
                charge(len(item))  # A colon for each key/value pair.
                for key, value in reversed(item.items()):
                    if type(key) is not str:
                        raise ValueError("UI command payload contains a non-text key")
                    stack.append((value, depth + 1, False))
                    stack.append((key, depth + 1, False))
            else:
                for value in reversed(item):
                    stack.append((value, depth + 1, False))
        elif type(item) is str:
            if len(item) > _MAX_PAYLOAD_BYTES - byte_count:
                raise ValueError("UI command payload exceeds the byte limit")
            try:
                encoded = json.dumps(item, ensure_ascii=False).encode("utf-8")
            except UnicodeEncodeError:
                raise ValueError("UI command payload contains invalid Unicode") from None
            charge(len(encoded))
        elif item is None:
            charge(4)
        elif type(item) is bool:
            charge(4 if item else 5)
        elif type(item) is int:
            if item.bit_length() > _MAX_INTEGER_BITS:
                raise ValueError("UI command payload contains an oversized number")
            charge(len(str(item)))
        elif type(item) is float:
            if not math.isfinite(item):
                raise ValueError("UI command payload contains a non-finite number")
            charge(len(json.dumps(item)))
        else:
            raise ValueError("UI command payload contains a non-JSON value")

    # Admission bounds depth, nodes and bytes before this snapshot. A separate
    # JSON tree prevents callers from changing nested dicts/lists after validation.
    return json.loads(
        json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    )
