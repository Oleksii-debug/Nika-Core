from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from nika_core.media.privacy import redact_mapping, redact_text

_POSIX_LOCAL_USER_PATH = re.compile(
    r"(?<![A-Za-z0-9])/(?:home|Users)/[^/\s\"'<>]+(?:/[^\s\"'<>]*)?"
)
_WINDOWS_LOCAL_USER_PATH = re.compile(
    r"(?i)(?<![A-Za-z0-9])[A-Z]:\\Users\\[^\\\s\"'<>]+(?:\\[^\s\"'<>]*)?"
)


def minimize_for_persistence(value: Any) -> Any:
    """Minimize secrets and sensitive local user paths before durable memory storage."""

    return _redact_local_paths(_redact_secrets(value))


def _redact_secrets(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        result: dict[Any, Any] = {}
        for key, item in value.items():
            if isinstance(key, str):
                result[key] = redact_mapping({key: item})[key]
            else:
                result[key] = _redact_secrets(item)
        return result
    if isinstance(value, list):
        return [_redact_secrets(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_secrets(item) for item in value)
    return value


def _redact_local_paths(value: Any) -> Any:
    if isinstance(value, str):
        redacted = _POSIX_LOCAL_USER_PATH.sub("[LOCAL_PATH]", value)
        return _WINDOWS_LOCAL_USER_PATH.sub("[LOCAL_PATH]", redacted)
    if isinstance(value, Mapping):
        return {key: _redact_local_paths(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_local_paths(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_local_paths(item) for item in value)
    return value
