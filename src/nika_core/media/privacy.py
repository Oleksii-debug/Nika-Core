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
    r"(?i)([?&](?:token|access_token|refresh_token|api_key|auth|key|password|secret|signature|sig|expires)=[^&#\s]+)"
)
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_COOKIE_HEADER = re.compile(r"(?im)\b((?:set-)?cookie)\s*:\s*[^\r\n]*")
_AUTHORIZATION_HEADER = re.compile(r"(?im)\b((?:proxy-)?authorization)\s*:\s*[^\r\n]*")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(?<![A-Za-z0-9_])"
    r"((?:api[-_]?key|access[-_]?token|refresh[-_]?token|client[-_]?secret|"
    r"authorization|password|token|secret|cookie|cookies|session[-_]?id)"
    r"\s*[:=]\s*)"
    r"([^\s,;&#]+)"
)
_SENSITIVE_ARGV_OPTIONS = frozenset(
    {
        "--access-token",
        "--access_token",
        "--api-key",
        "--api_key",
        "--auth",
        "--authorization",
        "--client-secret",
        "--client_secret",
        "--cookie",
        "--cookies",
        "--cookies-from-browser",
        "--netrc-cmd",
        "--netrc-location",
        "--password",
        "--refresh-token",
        "--refresh_token",
        "--secret",
        "--session-id",
        "--session_id",
        "--token",
    }
)


def redact_text(value: str) -> str:
    redacted = _AUTHORIZATION_HEADER.sub(
        lambda match: f"{match.group(1)}: [REDACTED]",
        value,
    )
    redacted = _COOKIE_HEADER.sub(lambda match: f"{match.group(1)}: [REDACTED]", redacted)
    redacted = _BEARER.sub("Bearer [REDACTED]", redacted)
    redacted = _SECRET_ASSIGNMENT.sub(
        lambda match: f"{match.group(1)}[REDACTED]",
        redacted,
    )
    return _SENSITIVE_QUERY.sub(
        lambda match: match.group(0).split("=", 1)[0] + "=[REDACTED]",
        redacted,
    )


def redact_argv(argv: tuple[str, ...]) -> tuple[str, ...]:
    """Redact public argv evidence without changing the subprocess argv itself."""

    result: list[str] = []
    redact_next = False
    for part in argv:
        if redact_next:
            result.append("[REDACTED]")
            redact_next = False
            continue
        option, separator, _value = part.partition("=")
        if option.casefold() in _SENSITIVE_ARGV_OPTIONS:
            if separator:
                result.append(f"{option}=[REDACTED]")
            else:
                result.append(redact_text(part))
                redact_next = True
            continue
        result.append(redact_text(part))
    return tuple(result)


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
