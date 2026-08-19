from __future__ import annotations

from enum import StrEnum


class MediaErrorCode(StrEnum):
    INVALID_SOURCE = "invalid_source"
    SOURCE_NOT_FOUND = "source_not_found"
    SOURCE_TOO_LARGE = "source_too_large"
    PATH_ESCAPE = "path_escape"
    COMPONENT_MISSING = "component_missing"
    COMPONENT_DISABLED = "component_disabled"
    AUTH_REQUIRED = "auth_required"
    UNSUPPORTED_SOURCE = "unsupported_source"
    PLAYLIST_LIMIT = "playlist_limit"
    DURATION_LIMIT = "duration_limit"
    PROCESS_FAILED = "process_failed"
    PROCESS_TIMEOUT = "process_timeout"
    PROCESS_CANCELLED = "process_cancelled"
    OUTPUT_LIMIT = "output_limit"
    INVALID_METADATA = "invalid_metadata"
    INVALID_SUBTITLE = "invalid_subtitle"
    LOW_QUALITY_SUBTITLE = "low_quality_subtitle"
    PROBE_FAILED = "probe_failed"
    CHECKSUM_MISMATCH = "checksum_mismatch"
    RESOURCE_BLOCKED = "resource_blocked"


class MediaError(RuntimeError):
    def __init__(
        self,
        code: MediaErrorCode,
        message: str,
        *,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
