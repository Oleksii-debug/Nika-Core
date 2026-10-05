from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Final

from nika_core.web_api.application import (
    WebApplicationBoundary,
    WebCommandAdmissionError,
    WebCommandOutcomeUnknownError,
)
from nika_core.web_api.contracts import WebCommandResult, WebPrincipal

_MAX_HTTP_BODY_BYTES: Final = 256 * 1024
_MAX_HTTP_RESPONSE_BYTES: Final = 80 * 1024
_JSON_CONTENT_TYPE: Final = "application/json; charset=utf-8"


def _json_content_type(value: object) -> bool:
    if type(value) is not str:
        return False
    parts = value.split(";")
    if not parts or parts[0].strip().casefold() != "application/json":
        return False
    charset_seen = False
    for parameter in parts[1:]:
        name, separator, raw_value = parameter.strip().partition("=")
        if not separator or name.strip().casefold() != "charset" or charset_seen:
            return False
        charset = raw_value.strip()
        if len(charset) >= 2 and charset[0] == charset[-1] == '"':
            charset = charset[1:-1]
        if charset.casefold() != "utf-8":
            return False
        charset_seen = True
    return True


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> object:
    raise ValueError("non-finite JSON constant")


def _encode_payload(payload: dict[str, object]) -> bytes:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > _MAX_HTTP_RESPONSE_BYTES:
        raise RuntimeError("HTTP response exceeds bounded transport size")
    return encoded


@dataclass(frozen=True, slots=True)
class HttpCommandResponse:
    """Framework-neutral HTTP response projection for one Web command request."""

    status_code: int
    body: bytes

    def __post_init__(self) -> None:
        if type(self.status_code) is not int or not 100 <= self.status_code <= 599:
            raise ValueError("HTTP status_code must be an exact integer from 100 through 599")
        if type(self.body) is not bytes:
            raise ValueError("HTTP response body must be exact bytes")
        if len(self.body) > _MAX_HTTP_RESPONSE_BYTES:
            raise ValueError("HTTP response body exceeds bounded transport size")

    @property
    def headers(self) -> tuple[tuple[str, str], ...]:
        return (
            ("content-type", _JSON_CONTENT_TYPE),
            ("cache-control", "no-store"),
            ("x-content-type-options", "nosniff"),
        )


class HttpCommandAdapter:
    """Decode one bounded HTTP command envelope and delegate to WebApplicationBoundary.

    Authentication is deliberately out of scope. The server layer must resolve a trusted
    ``WebPrincipal`` before calling this adapter. Client headers or JSON never become identity.
    """

    def __init__(self, boundary: WebApplicationBoundary) -> None:
        if type(boundary) is not WebApplicationBoundary:
            raise ValueError("boundary must be the exact WebApplicationBoundary")
        self._boundary = boundary

    def handle(
        self,
        *,
        principal: WebPrincipal,
        method: object,
        content_type: object,
        body: object,
    ) -> HttpCommandResponse:
        if type(principal) is not WebPrincipal:
            return self._error(500, "server_principal_invalid", "Server identity is unavailable.")
        if type(method) is not str or method != "POST":
            return self._error(405, "method_not_allowed", "Only POST is supported.")
        if not _json_content_type(content_type):
            return self._error(415, "unsupported_media_type", "Content-Type must be JSON UTF-8.")
        if type(body) is not bytes:
            return self._error(500, "transport_body_invalid", "Server request body is unavailable.")
        if not body:
            return self._error(400, "empty_body", "Request body must not be empty.")
        if len(body) > _MAX_HTTP_BODY_BYTES:
            return self._error(
                413,
                "payload_too_large",
                "Request body exceeds the transport limit.",
            )

        try:
            text = body.decode("utf-8", errors="strict")
            command = json.loads(
                text,
                object_pairs_hook=_unique_object,
                parse_constant=_reject_json_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
            return self._error(400, "invalid_json", "Request body must be valid JSON UTF-8.")
        if type(command) is not dict:
            return self._error(400, "invalid_command", "Command body must be a JSON object.")

        try:
            result = self._boundary.dispatch(principal=principal, command=command)
        except WebCommandAdmissionError:
            return self._error(400, "invalid_command", "Command failed admission.")
        except WebCommandOutcomeUnknownError as exc:
            return self._error(
                409,
                "outcome_unknown",
                "Command outcome is unknown; reconcile before retry.",
                request_id=exc.request_id,
            )
        except Exception:
            return self._error(500, "internal_error", "Server command processing failed.")
        return self._from_result(result)

    @staticmethod
    def _from_result(result: WebCommandResult) -> HttpCommandResponse:
        if result.status == "accepted":
            status_code = 202
        elif result.status == "rejected":
            status_code = 403 if result.code == "forbidden" else 409
        else:
            # A domain-level completed/failed result was transported successfully. Returning 200
            # avoids teaching generic HTTP clients that a failed command is automatically retryable.
            status_code = 200
        return HttpCommandResponse(
            status_code=status_code,
            body=_encode_payload(
                {
                    "code": result.code,
                    "data": result.data,
                    "message": result.message,
                    "request_id": result.request_id,
                    "status": result.status,
                }
            ),
        )

    @staticmethod
    def _error(
        status_code: int,
        code: str,
        message: str,
        *,
        request_id: str | None = None,
    ) -> HttpCommandResponse:
        return HttpCommandResponse(
            status_code=status_code,
            body=_encode_payload(
                {
                    "code": code,
                    "data": {},
                    "message": message,
                    "request_id": request_id,
                    "status": "failed",
                }
            ),
        )
