from __future__ import annotations

import json

import pytest

from nika_core.web_api import (
    WebApplicationBoundary,
    WebCommand,
    WebCommandResult,
    WebPrincipal,
)
from nika_core.web_api.http_transport import HttpCommandAdapter


class _Authorization:
    def __init__(self, allowed: object = True, *, error: Exception | None = None) -> None:
        self.allowed = allowed
        self.error = error
        self.calls = 0

    def allows(self, principal: WebPrincipal, command: WebCommand) -> bool:
        del principal, command
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.allowed  # type: ignore[return-value]


class _Handler:
    def __init__(
        self,
        *,
        status: str = "completed",
        code: str = "ok",
        error: Exception | None = None,
    ) -> None:
        self.status = status
        self.code = code
        self.error = error
        self.calls = 0

    def handle(self, principal: WebPrincipal, command: WebCommand) -> WebCommandResult:
        del principal
        self.calls += 1
        if self.error is not None:
            raise self.error
        return WebCommandResult.create(
            request_id=command.request_id,
            status=self.status,
            code=self.code,
            message="Готово.",
            data={"payload": command.payload},
        )


def _principal() -> WebPrincipal:
    return WebPrincipal(
        tenant_id="орендар 1",
        user_id="користувач@example.test",
        workspace_id="Мій простір",
        session_id="сесія/1",
    )


def _body(*, request_id: str = "request-1", action_id: str = "task.create") -> bytes:
    return json.dumps(
        {
            "request_id": request_id,
            "action_id": action_id,
            "payload": {"command": "перевір стан"},
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _adapter(
    authorization: _Authorization | None = None,
    handler: _Handler | None = None,
) -> tuple[HttpCommandAdapter, _Authorization, _Handler]:
    authority = authorization or _Authorization()
    command_handler = handler or _Handler()
    boundary = WebApplicationBoundary(authorization=authority, handler=command_handler)
    return HttpCommandAdapter(boundary), authority, command_handler


def _json(response_body: bytes) -> dict[str, object]:
    value = json.loads(response_body.decode("utf-8"))
    assert type(value) is dict
    return value


def test_completed_command_returns_canonical_no_store_json() -> None:
    adapter, authorization, handler = _adapter()

    response = adapter.handle(
        principal=_principal(),
        method="POST",
        content_type="application/json; charset=utf-8",
        body=_body(),
    )

    assert response.status_code == 200
    assert ("cache-control", "no-store") in response.headers
    assert ("x-content-type-options", "nosniff") in response.headers
    payload = _json(response.body)
    assert payload["request_id"] == "request-1"
    assert payload["status"] == "completed"
    assert payload["data"] == {"payload": {"command": "перевір стан"}}
    assert authorization.calls == 1
    assert handler.calls == 1


def test_accepted_command_returns_202() -> None:
    adapter, _, _ = _adapter(handler=_Handler(status="accepted", code="queued"))
    response = adapter.handle(
        principal=_principal(), method="POST", content_type="application/json", body=_body()
    )
    assert response.status_code == 202
    assert _json(response.body)["status"] == "accepted"


def test_forbidden_command_returns_403_without_handler_effect() -> None:
    authorization = _Authorization(False)
    handler = _Handler()
    adapter, _, _ = _adapter(authorization=authorization, handler=handler)
    response = adapter.handle(
        principal=_principal(), method="POST", content_type="application/json", body=_body()
    )
    assert response.status_code == 403
    assert _json(response.body)["code"] == "forbidden"
    assert handler.calls == 0


def test_domain_failure_returns_200_not_automatic_retry_signal() -> None:
    adapter, _, _ = _adapter(handler=_Handler(status="failed", code="domain_failed"))
    response = adapter.handle(
        principal=_principal(), method="POST", content_type="application/json", body=_body()
    )
    assert response.status_code == 200
    assert _json(response.body)["status"] == "failed"


def test_handler_exception_returns_reconciliation_response_without_secret_text() -> None:
    adapter, _, handler = _adapter(handler=_Handler(error=RuntimeError("secret-provider-token")))
    response = adapter.handle(
        principal=_principal(), method="POST", content_type="application/json", body=_body()
    )
    payload = _json(response.body)
    assert response.status_code == 409
    assert payload["code"] == "outcome_unknown"
    assert payload["request_id"] == "request-1"
    assert b"secret-provider-token" not in response.body
    assert handler.calls == 1


def test_authorization_exception_is_generic_server_error_without_handler_effect() -> None:
    authorization = _Authorization(error=RuntimeError("secret-auth-detail"))
    handler = _Handler()
    adapter, _, _ = _adapter(authorization=authorization, handler=handler)
    response = adapter.handle(
        principal=_principal(), method="POST", content_type="application/json", body=_body()
    )
    assert response.status_code == 500
    assert _json(response.body)["code"] == "internal_error"
    assert b"secret-auth-detail" not in response.body
    assert handler.calls == 0


@pytest.mark.parametrize(
    ("method", "content_type", "expected"),
    [
        ("GET", "application/json", 405),
        ("POST", "text/plain", 415),
        ("POST", "application/json; charset=windows-1251", 415),
        ("POST", "application/json; charset=utf-8; charset=utf-8", 415),
    ],
)
def test_method_and_content_type_are_admitted_before_dispatch(
    method: object,
    content_type: object,
    expected: int,
) -> None:
    adapter, authorization, handler = _adapter()
    response = adapter.handle(
        principal=_principal(), method=method, content_type=content_type, body=_body()
    )
    assert response.status_code == expected
    assert authorization.calls == 0
    assert handler.calls == 0


def test_utf8_charset_parameter_is_case_insensitive_and_may_be_quoted() -> None:
    adapter, _, _ = _adapter()
    for content_type in (
        "Application/JSON; Charset=UTF-8",
        'application/json; charset="utf-8"',
    ):
        response = adapter.handle(
            principal=_principal(), method="POST", content_type=content_type, body=_body()
        )
        assert response.status_code == 200


def test_duplicate_keys_nonfinite_constants_and_non_object_roots_are_rejected() -> None:
    adapter, authorization, handler = _adapter()
    bodies = (
        b'{"request_id":"one","request_id":"two","action_id":"task.create","payload":{}}',
        b'{"request_id":"one","action_id":"task.create","payload":{"value":NaN}}',
        b"[]",
    )
    for body in bodies:
        response = adapter.handle(
            principal=_principal(), method="POST", content_type="application/json", body=body
        )
        assert response.status_code == 400
    assert authorization.calls == 0
    assert handler.calls == 0



def test_oversized_numeric_literals_are_rejected_before_dispatch() -> None:
    adapter, authorization, handler = _adapter()
    prefix = b'{"request_id":"one","action_id":"task.create","payload":{"value":'
    for literal in (b"9" * 5000, b"1." + b"2" * 5000):
        response = adapter.handle(
            principal=_principal(),
            method="POST",
            content_type="application/json",
            body=prefix + literal + b"}}",
        )
        assert response.status_code == 400
    assert authorization.calls == 0
    assert handler.calls == 0


def test_invalid_utf8_and_empty_body_are_rejected_before_dispatch() -> None:
    adapter, authorization, handler = _adapter()
    for body in (b"\xff", b""):
        response = adapter.handle(
            principal=_principal(), method="POST", content_type="application/json", body=body
        )
        assert response.status_code == 400
    assert authorization.calls == 0
    assert handler.calls == 0


def test_oversized_body_is_rejected_before_json_allocation_or_dispatch() -> None:
    adapter, authorization, handler = _adapter()
    body = b"{" + b" " * (256 * 1024) + b"}"
    response = adapter.handle(
        principal=_principal(), method="POST", content_type="application/json", body=body
    )
    assert response.status_code == 413
    assert authorization.calls == 0
    assert handler.calls == 0


def test_client_cannot_smuggle_identity_through_json() -> None:
    adapter, authorization, handler = _adapter()
    value = json.loads(_body().decode("utf-8"))
    value["workspace_id"] = "attacker-space"
    body = json.dumps(value).encode("utf-8")
    response = adapter.handle(
        principal=_principal(), method="POST", content_type="application/json", body=body
    )
    assert response.status_code == 400
    assert authorization.calls == 0
    assert handler.calls == 0


def test_invalid_server_principal_and_non_bytes_body_are_server_contract_errors() -> None:
    adapter, authorization, handler = _adapter()
    response = adapter.handle(
        principal=object(),  # type: ignore[arg-type]
        method="POST",
        content_type="application/json",
        body=_body(),
    )
    assert response.status_code == 500
    response = adapter.handle(
        principal=_principal(),
        method="POST",
        content_type="application/json",
        body=bytearray(_body()),
    )
    assert response.status_code == 500
    assert authorization.calls == 0
    assert handler.calls == 0
