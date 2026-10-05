from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

_SECRET_KEYS = frozenset(
    {
        "authorization",
        "cookie",
        "cookies",
        "password",
        "token",
        "access_token",
        "refresh_token",
        "api_key",
        "secret",
    }
)
_SENSITIVE_QUERY = re.compile(
    r"(?i)([?&](?:token|access_token|auth|key|signature|sig|expires)=[^&#\s]+)"
)
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")


def redact_text(value: str) -> str:
    redacted = _BEARER.sub("Bearer [REDACTED]", value)
    return _SENSITIVE_QUERY.sub(lambda match: match.group(0).split("=", 1)[0] + "=[REDACTED]", redacted)


def redact_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, item in value.items():
        lowered = key.lower()
        if lowered in _SECRET_KEYS or any(token in lowered for token in ("cookie", "password", "token")):
            result[key] = "[REDACTED]"
        elif isinstance(item, str):
            result[key] = redact_text(item)
        elif isinstance(item, Mapping):
            result[key] = redact_mapping(item)
        elif isinstance(item, list):
            result[key] = [redact_text(part) if isinstance(part, str) else part for part in item]
        else:
            result[key] = item
    return result
