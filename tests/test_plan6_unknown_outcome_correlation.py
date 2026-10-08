"""Plan 6 Section 1: unknown-effect correlation belongs to Web admission."""
from __future__ import annotations

import json

import pytest

from nika_core.web_api import (
    WebApplicationBoundary,
    WebCommandOutcomeUnknownError,
    WebPrincipal,
)
from nika_core.web_api.http_transport import HttpCommandAdapter


class _Allow:
    def allows(self, principal, command) -> bool:
        return principal.tenant_id == "tenant-a" and command.action_id == "task.create"


class _UnknownEffect:
    def __init__(self, *, mutate_id: bool = False) -> None:
        self.calls = 0
        self.mutate_id = mutate_id

    def handle(self, principal, command):
        self.calls += 1
        if self.mutate_id:
            object.__setattr__(command, "request_id", "mutated-request")
        # A failed or compromised handler must not set the reconciliation key.
        raise WebCommandOutcomeUnknownError("unrelated-request")


def _principal() -> WebPrincipal:
    return WebPrincipal(
        tenant_id="tenant-a",
        user_id="user-a",
        workspace_id="workspace-a",
        session_id="session-a",
    )


def _command() -> dict[str, object]:
    return {
        "request_id": "authorized-request",
        "action_id": "task.create",
        "payload": {"command": "create"},
    }


@pytest.mark.parametrize("mutate_id", [False, True])
def test_unknown_outcome_reuses_admitted_request_id(mutate_id: bool) -> None:
    handler = _UnknownEffect(mutate_id=mutate_id)
    boundary = WebApplicationBoundary(authorization=_Allow(), handler=handler)
    with pytest.raises(WebCommandOutcomeUnknownError) as caught:
        boundary.dispatch(principal=_principal(), command=_command())
    assert handler.calls == 1
    assert caught.value.request_id == "authorized-request"
    assert "unrelated-request" not in str(caught.value)
    assert isinstance(caught.value.__cause__, WebCommandOutcomeUnknownError)
    assert caught.value.__cause__.request_id == "unrelated-request"


@pytest.mark.parametrize("mutate_id", [False, True])
def test_http_conflict_reconciles_using_original_request_id(mutate_id: bool) -> None:
    handler = _UnknownEffect(mutate_id=mutate_id)
    adapter = HttpCommandAdapter(
        WebApplicationBoundary(authorization=_Allow(), handler=handler)
    )
    response = adapter.handle(
        principal=_principal(),
        method="POST",
        content_type="application/json",
        body=json.dumps(_command()).encode("utf-8"),
    )
    result = json.loads(response.body)
    assert handler.calls == 1
    assert response.status_code == 409
    assert result["status"] == "failed"
    assert result["code"] == "outcome_unknown"
    assert result["request_id"] == "authorized-request"
    assert "unrelated-request" not in response.body.decode("utf-8")
    assert "mutated-request" not in response.body.decode("utf-8")


@pytest.mark.parametrize(
    "fault",
    [RuntimeError("private authorization service path"),
     WebCommandOutcomeUnknownError("forged-auth-request")],
)
def test_authorization_fault_is_definite_pre_effect_failure(fault: Exception) -> None:
    class _BrokenAuthorization:
        def allows(self, principal, command):
            raise fault

    handler = _UnknownEffect()
    boundary = WebApplicationBoundary(
        authorization=_BrokenAuthorization(), handler=handler
    )
    with pytest.raises(RuntimeError, match="Web authorization port failed") as caught:
        boundary.dispatch(principal=_principal(), command=_command())
    assert caught.value.__cause__ is fault
    assert handler.calls == 0


@pytest.mark.parametrize(
    "fault",
    [RuntimeError("private authorization service path"),
     WebCommandOutcomeUnknownError("forged-auth-request")],
)
def test_http_authorization_fault_never_claims_unknown_effect(fault: Exception) -> None:
    class _BrokenAuthorization:
        def allows(self, principal, command):
            raise fault

    handler = _UnknownEffect()
    adapter = HttpCommandAdapter(
        WebApplicationBoundary(
            authorization=_BrokenAuthorization(), handler=handler
        )
    )
    response = adapter.handle(
        principal=_principal(),
        method="POST",
        content_type="application/json",
        body=json.dumps(_command()).encode("utf-8"),
    )
    body = json.loads(response.body)
    assert handler.calls == 0
    assert response.status_code == 500
    assert body["status"] == "failed"
    assert body["code"] == "internal_error"
    assert body["request_id"] is None
    assert "private authorization" not in response.body.decode("utf-8")
    assert "forged-auth-request" not in response.body.decode("utf-8")
