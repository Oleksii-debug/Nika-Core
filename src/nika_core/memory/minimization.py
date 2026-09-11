from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from nika_core.media.privacy import redact_mapping, redact_text

_POSIX_LOCAL_USER_PATH = re.compile(
    r"(?<![A-Za-z0-9])/(?:home|Users)/[^/\s\"'<>]+(?:/[^\s\"'<>]*)?"
)
_WINDOWS_LOCAL_USER_FILE_PATH = re.compile(
    r"(?i)(?<![A-Za-z0-9])[A-Z]:[\\/]Users[\\/]"
    r"(?:[^\\/\r\n\"'<>]+[\\/])+"
    r"[^\\/\r\n\"'<>]*?\.[A-Za-z0-9]{1,16}"
    r"(?=$|[\s,;:!?()\[\]{}])"
)
_WINDOWS_LOCAL_USER_PATH = re.compile(
    r"(?i)(?<![A-Za-z0-9])[A-Z]:[\\/]Users[\\/]"
    r"[^\\/\r\n\"'<>]+(?=[\\/])[\\/]"
    r"(?:[^\\/\r\n\"'<>]+(?=[\\/])[\\/])*"
    r"(?:[^\\/\r\n\"'<>]*?\.[A-Za-z0-9]{1,16}"
    r"(?=$|[\s,;:!?()\[\]{}])|[^\s\\/\r\n\"'<>]+)"
)
_POSIX_LOCAL_USER_PATH_FULL = re.compile(
    r"/(?:home|Users)/[^/\r\n\"'<>]+(?:/[^\r\n\"'<>]*)?"
)
_WINDOWS_LOCAL_USER_PATH_FULL = re.compile(
    r"(?i)[A-Z]:[\\/]Users[\\/][^\\/\r\n\"'<>]+(?:[\\/][^\r\n\"'<>]*)?"
)
_KEY_COLLISION_ERROR = "memory persistence key collision after minimization"


def minimize_for_persistence(value: Any) -> Any:
    """Minimize secrets and sensitive local user paths before durable memory storage."""

    return _redact_local_paths(_redact_secrets(value))


def _redact_secrets(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        result: dict[Any, Any] = {}
        for key, item in value.items():
            safe_key = redact_text(key) if isinstance(key, str) else key
            _require_unique_key(result, safe_key)
            if isinstance(key, str):
                redacted_item = redact_mapping({key: item})[key]
                result[safe_key] = _redact_secrets(redacted_item)
            else:
                result[safe_key] = _redact_secrets(item)
        return result
    if isinstance(value, list):
        return [_redact_secrets(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_secrets(item) for item in value)
    return value


def _redact_local_paths(value: Any) -> Any:
    if isinstance(value, str):
        return _redact_local_path_text(value)
    if isinstance(value, Mapping):
        result: dict[Any, Any] = {}
        for key, item in value.items():
            safe_key = _redact_local_path_text(key) if isinstance(key, str) else key
            _require_unique_key(result, safe_key)
            result[safe_key] = _redact_local_paths(item)
        return result
    if isinstance(value, list):
        return [_redact_local_paths(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_local_paths(item) for item in value)
    return value


def _redact_local_path_text(value: str) -> str:
    if (
        _POSIX_LOCAL_USER_PATH_FULL.fullmatch(value)
        or _WINDOWS_LOCAL_USER_PATH_FULL.fullmatch(value)
    ):
        return "[LOCAL_PATH]"
    redacted = _WINDOWS_LOCAL_USER_FILE_PATH.sub("[LOCAL_PATH]", value)
    redacted = _POSIX_LOCAL_USER_PATH.sub("[LOCAL_PATH]", redacted)
    return _WINDOWS_LOCAL_USER_PATH.sub("[LOCAL_PATH]", redacted)


def _require_unique_key(result: Mapping[Any, Any], key: Any) -> None:
    if key in result:
        raise ValueError(_KEY_COLLISION_ERROR)
