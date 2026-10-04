from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import unquote_plus

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
_SENSITIVE_KEY_TOKENS = frozenset({"cookie", "password", "secret", "token"})
_NON_SECRET_KEY_SUFFIXES = frozenset({"count"})
_SENSITIVE_QUERY = re.compile(r"([?&])([^=&#\s]+)=([^&#\s]*)")
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_COOKIE_HEADER = re.compile(r"(?im)\b((?:set-)?cookie)\s*:\s*[^\r\n]*")
_AUTHORIZATION_HEADER = re.compile(r"(?im)\b((?:proxy-)?authorization)\s*:\s*[^\r\n]*")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(?<![A-Za-z0-9_])"
    r"((?:api[-_]?key|access[-_]?token|refresh[-_]?token|client[-_]?secret|"
    r"authorization|password|token|secret|cookie|cookies|session[-_]?id|"
    r"subscription[-_]?key)"
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
        "--subscription-key",
        "--subscription_key",
        "--subscriptionkey",
        "--x-api-key",
        "--x_api_key",
        "--xapikey",
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


def _unquote_key_bounded(key: str) -> str:
    """Recognize credential aliases even when a query name is multiply escaped."""

    for _ in range(3):
        decoded = unquote_plus(key)
        if decoded == key:
            break
        key = decoded
    return key


def _is_secret_key(key: str) -> bool:
    tokens = _normalized_key_tokens(_unquote_key_bounded(key))
    if not tokens:
        return False
    normalized = "_".join(tokens)
    if normalized in _SECRET_KEYS:
        return True
    if len(tokens) >= 2 and tokens[-1] == "key" and tokens[-2] in {
        "api",
        "subscription",
    }:
        return True
    if tokens[-1] in _NON_SECRET_KEY_SUFFIXES:
        return False
    return any(token in _SENSITIVE_KEY_TOKENS for token in tokens)


def _is_sensitive_query_key(key: str) -> bool:
    normalized = "_".join(_normalized_key_tokens(_unquote_key_bounded(key)))
    return _is_secret_key(key) or normalized in {
        "auth", "key", "sig", "signature", "expires",
    }


def _contains_encoded_sensitive_query(value: str) -> bool:
    """Detect escaped nested credentials without decoding the public evidence."""

    # Plain nested URLs are handled by _redact_query_match's recursive pass,
    # which preserves the non-secret part of that URL.
    if "?" in value:
        return False
    decoded = value
    for _ in range(3):
        if "%" not in decoded:
            break
        updated = unquote_plus(decoded)
        if updated == decoded:
            break
        decoded = updated
        if any(
            _is_sensitive_query_key(match.group(2))
            for match in _SENSITIVE_QUERY.finditer(decoded)
        ):
            return True
    return False


def _redact_query_match(match: re.Match[str], depth: int = 0) -> str:
    key = match.group(2)
    if _is_sensitive_query_key(key):
        return f"{match.group(1)}{key}=[REDACTED]"
    value = match.group(3)
    if _contains_encoded_sensitive_query(value):
        # Redact the outer value: publishing the encoded form leaks the
        # recoverable credential even if the inner URL is never opened.
        return f"{match.group(1)}{key}=[REDACTED]"
    if "?" in value:
        # A URL can itself be the value of another query parameter. The broad
        # outer match consumes its nested query unless it is redacted here.
        if depth >= 8:
            value = "[REDACTED]"
        else:
            value = _SENSITIVE_QUERY.sub(
                lambda child: _redact_query_match(child, depth + 1), value
            )
    return f"{match.group(1)}{key}={value}"


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
    return _SENSITIVE_QUERY.sub(_redact_query_match, redacted)


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
