from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

_SECRET_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "auth_token",
        "authorization",
        "client_secret",
        "cookie",
        "cookies",
        "password",
        "refresh_token",
        "secret",
        "session_cookie",
        "session_id",
        "token",
    }
)
_SENSITIVE_KEY_TOKENS = frozenset({"cookie", "credential", "password", "secret", "token"})
_NON_SECRET_KEY_SUFFIXES = frozenset({"count"})
_SENSITIVE_COMPACT_KEYS = frozenset(
    {
        "awsaccesskeyid",
        "googleaccessid",
        "subscriptionkey",
        "xapikey",
        "xamzcredential",
        "xgoogcredential",
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
    r"client[-_]?credential|aws[-_]?access[-_]?key[-_]?id|google[-_]?access[-_]?id|"
    r"subscription[-_]?key|x[-_]?api[-_]?key|"
    r"x[-_]?amz[-_]?credential|x[-_]?goog[-_]?credential|"
    r"authorization|password|token|secret|credential|cookie|cookies|session[-_]?id)"
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
_ACRONYM_BOUNDARY = re.compile(r"(?<=[A-Z])(?=[A-Z][a-z])")
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_KEY_SEPARATOR = re.compile(r"[^A-Za-z0-9]+")


def _normalized_key_tokens(key: str) -> tuple[str, ...]:
    expanded = _ACRONYM_BOUNDARY.sub("_", key)
    expanded = _CAMEL_BOUNDARY.sub("_", expanded)
    return tuple(token.casefold() for token in _KEY_SEPARATOR.split(expanded) if token)


def _is_secret_key(key: str) -> bool:
    tokens = _normalized_key_tokens(key)
    if not tokens:
        return False
    normalized = "_".join(tokens)
    if normalized in _SECRET_KEYS:
        return True
    if tokens[-1] in _NON_SECRET_KEY_SUFFIXES:
        return False
    if any(token in _SENSITIVE_KEY_TOKENS for token in tokens):
        return True
    compact = "".join(tokens)
    if compact in _SENSITIVE_COMPACT_KEYS:
        return True
    token_set = frozenset(tokens)
    return {"api", "key"}.issubset(token_set) or {"subscription", "key"}.issubset(
        token_set
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


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        return redact_mapping(value)
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_value(item) for item in value)
    return value


def redact_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, item in value.items():
        if _is_secret_key(key):
            result[key] = "[REDACTED]"
        else:
            result[key] = _redact_value(item)
    return result
