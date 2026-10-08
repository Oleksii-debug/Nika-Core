from __future__ import annotations

import math

import pytest

from nika_core.web_api import (
    WebApplicationBoundary,
    WebCommand,
    WebCommandAdmissionError,
    WebCommandOutcomeUnknownError,
    WebCommandResult,
    WebPrincipal,
)


class _Allow:
    def __init__(self, allowed: object = True) -> None:
        self.allowed = allowed
        self.calls: list[tuple[WebPrincipal, WebCommand]] = []

    def allows(self, principal: WebPrincipal, command: WebCommand) -> bool:
        self.calls.append((principal, command))
        return self.allowed  # type: ignore[return-value]


class _Handler:
    def __init__(self) -> None:
        self.calls: list[tuple[WebPrincipal, WebCommand]] = []

    def handle(self, principal: WebPrincipal, command: WebCommand) -> WebCommandResult:
        self.calls.append((principal, command))
        return WebCommandResult.create(
            request_id=command.request_id,
            status="completed",
            code="ok",
            message="Готово.",
            data={"action_id": command.action_id, "payload": command.payload},
        )


def _principal() -> WebPrincipal:
    return WebPrincipal(
        tenant_id="tenant-1",
        user_id="user-1",
        workspace_id="workspace-1",
        session_id="session-1",
    )


def _command(payload: object | None = None) -> dict[str, object]:
    return {
        "request_id": "request-1",
        "action_id": "task.create",
        "payload": {} if payload is None else payload,
    }


def test_untrusted_command_cannot_supply_server_identity_fields() -> None:
    command = _command()
    command["user_id"] = "attacker"
    with pytest.raises(ValueError, match="exactly"):
        WebCommand.from_untrusted(command)


def test_command_rejects_mapping_and_text_subclasses_before_behavior() -> None:
    class EvilDict(dict[str, object]):
        def items(self):  # type: ignore[override]
            raise AssertionError("must not execute")

    class EvilStr(str):
        def encode(self, *_args: object, **_kwargs: object) -> bytes:
            raise AssertionError("must not execute")

    with pytest.raises(ValueError, match="exact object"):
        WebCommand.from_untrusted(EvilDict(_command()))
    bad = _command()
    bad["request_id"] = EvilStr("request-1")
    with pytest.raises(ValueError, match="exact string"):
        WebCommand.from_untrusted(bad)



def test_action_id_uses_canonical_dotted_registry_semantics() -> None:
    value = _command()
    value["action_id"] = "Custom.Action"
    assert WebCommand.from_untrusted(value).action_id == "Custom.Action"

    value["action_id"] = "not-dotted"
    with pytest.raises(ValueError, match="dotted"):
        WebCommand.from_untrusted(value)


def test_payload_is_detached_from_client_mutation() -> None:
    payload = {"nested": {"value": 1}}
    command = WebCommand.from_untrusted(_command(payload))
    payload["nested"]["value"] = 99  # type: ignore[index]
    assert command.payload == {"nested": {"value": 1}}


@pytest.mark.parametrize(
    "payload",
    [
        {"value": math.nan},
        {"value": math.inf},
        {"value": 1 << 5000},
        {"value": object()},
        {"value": ("tuple",)},
    ],
)
def test_payload_rejects_noncanonical_json_values(payload: object) -> None:
    with pytest.raises(ValueError):
        WebCommand.from_untrusted(_command(payload))


def test_payload_rejects_excessive_depth() -> None:
    payload: object = "leaf"
    for _ in range(34):
        payload = [payload]
    with pytest.raises(ValueError, match="depth"):
        WebCommand.from_untrusted(_command({"value": payload}))


def test_payload_rejects_excessive_node_count() -> None:
    with pytest.raises(ValueError, match="node count"):
        WebCommand.from_untrusted(_command({"values": [0] * 2050}))


def test_payload_rejects_excessive_canonical_bytes() -> None:
    with pytest.raises(ValueError, match="64 KiB"):
        WebCommand.from_untrusted(
            _command(
                {
                    "a": "x" * 16000,
                    "b": "y" * 16000,
                    "c": "z" * 16000,
                    "d": "q" * 16000,
                    "e": "r" * 16000,
                }
            )
        )



def test_dispatch_wraps_client_admission_without_exposing_raw_error() -> None:
    boundary = WebApplicationBoundary(authorization=_Allow(True), handler=_Handler())
    bad = _command()
    bad["payload"] = {"value": math.nan}
    with pytest.raises(WebCommandAdmissionError) as caught:
        boundary.dispatch(principal=_principal(), command=bad)
    assert str(caught.value) == "Web command was rejected by boundary admission"
    assert isinstance(caught.value.__cause__, ValueError)


def test_authorization_denial_never_reaches_handler() -> None:
    authorization = _Allow(False)
    handler = _Handler()
    boundary = WebApplicationBoundary(authorization=authorization, handler=handler)

    result = boundary.dispatch(principal=_principal(), command=_command())

    assert result.status == "rejected"
    assert result.code == "forbidden"
    assert handler.calls == []
    assert len(authorization.calls) == 1


def test_authorized_command_uses_server_principal_and_detached_payload() -> None:
    authorization = _Allow(True)
    handler = _Handler()
    boundary = WebApplicationBoundary(authorization=authorization, handler=handler)
    principal = _principal()
    payload = {"command": "перевір стан"}

    result = boundary.dispatch(principal=principal, command=_command(payload))
    payload["command"] = "changed"

    assert handler.calls[0][0] == principal
    assert handler.calls[0][0] is not principal  # immutable snapshot, not caller-owned authority
    assert handler.calls[0][1].payload == {"command": "перевір стан"}
    assert result.data["payload"] == {"command": "перевір стан"}


def test_authorization_must_return_exact_bool() -> None:
    boundary = WebApplicationBoundary(authorization=_Allow(1), handler=_Handler())
    with pytest.raises(RuntimeError, match="exact bool"):
        boundary.dispatch(principal=_principal(), command=_command())


def test_handler_cannot_change_request_identity() -> None:
    class WrongHandler:
        def handle(self, principal: WebPrincipal, command: WebCommand) -> WebCommandResult:
            del principal, command
            return WebCommandResult.create(
                request_id="other-request",
                status="completed",
                code="ok",
                message="Готово.",
            )

    boundary = WebApplicationBoundary(authorization=_Allow(True), handler=WrongHandler())
    with pytest.raises(WebCommandOutcomeUnknownError) as caught:
        boundary.dispatch(principal=_principal(), command=_command())
    assert caught.value.request_id == "request-1"


def test_principal_rejects_noncanonical_identity_text() -> None:
    class EvilStr(str):
        def encode(self, *_args: object, **_kwargs: object) -> bytes:
            raise AssertionError("must not execute")

    with pytest.raises(ValueError, match="exact string"):
        WebPrincipal(
            tenant_id=EvilStr("tenant-1"),
            user_id="user-1",
            workspace_id="workspace-1",
            session_id="session-1",
        )
    principal = WebPrincipal(
        tenant_id="орендар 1",
        user_id="користувач@example.test",
        workspace_id="Мій робочий простір",
        session_id="сесія/1",
    )
    assert principal.workspace_id == "Мій робочий простір"

    with pytest.raises(ValueError, match="surrounding whitespace"):
        WebPrincipal(
            tenant_id=" tenant-1",
            user_id="user-1",
            workspace_id="workspace-1",
            session_id="session-1",
        )
    with pytest.raises(ValueError, match="control text"):
        WebPrincipal(
            tenant_id="tenant-1",
            user_id="user-1",
            workspace_id="workspace\n1",
            session_id="session-1",
        )


def test_result_data_is_detached() -> None:
    data = {"items": [{"id": 1}]}
    result = WebCommandResult.create(
        request_id="request-1",
        status="completed",
        code="ok",
        message="Готово.",
        data=data,
    )
    data["items"][0]["id"] = 2
    assert result.data == {"items": [{"id": 1}]}



def test_wide_top_level_command_is_rejected_before_key_snapshot() -> None:
    command = _command()
    command.update({f"extra-{index}": index for index in range(10000)})
    with pytest.raises(ValueError, match="exactly"):
        WebCommand.from_untrusted(command)


def test_handler_failure_is_not_automatically_retried() -> None:
    class FailingHandler:
        def __init__(self) -> None:
            self.calls = 0

        def handle(self, principal: WebPrincipal, command: WebCommand) -> WebCommandResult:
            del principal, command
            self.calls += 1
            raise RuntimeError("effect outcome is unknown")

    handler = FailingHandler()
    boundary = WebApplicationBoundary(authorization=_Allow(True), handler=handler)
    with pytest.raises(WebCommandOutcomeUnknownError) as caught:
        boundary.dispatch(principal=_principal(), command=_command())
    assert handler.calls == 1
    assert caught.value.request_id == "request-1"
    assert "effect outcome is unknown" not in str(caught.value)
    assert isinstance(caught.value.__cause__, RuntimeError)

def test_result_constructor_rejects_noncanonical_internal_json() -> None:
    with pytest.raises(ValueError, match="canonical JSON|finite"):
        WebCommandResult(
            request_id="request-1",
            status="completed",
            code="ok",
            message="Готово.",
            _data_json='{"value":NaN}',
        )


def test_boundary_returns_a_detached_canonical_result() -> None:
    data = {"items": [{"id": 1}]}

    class DataHandler:
        def handle(self, principal: WebPrincipal, command: WebCommand) -> WebCommandResult:
            del principal
            return WebCommandResult.create(
                request_id=command.request_id,
                status="completed",
                code="ok",
                message="Готово.",
                data=data,
            )

    boundary = WebApplicationBoundary(authorization=_Allow(True), handler=DataHandler())
    result = boundary.dispatch(principal=_principal(), command=_command())
    data["items"][0]["id"] = 2
    assert result.data == {"items": [{"id": 1}]}

def test_authorizer_cannot_mutate_core_principal_or_command_after_approval() -> None:
    class MutatingAuthorization:
        def allows(self, principal: WebPrincipal, command: WebCommand) -> bool:
            object.__setattr__(principal, "workspace_id", "workspace-attacker")
            object.__setattr__(principal, "tenant_id", "tenant-attacker")
            object.__setattr__(command, "action_id", "admin.delete")
            object.__setattr__(command, "request_id", "request-attacker")
            object.__setattr__(command, "_payload_json", '{"target":"attacker"}')
            return True

    handler = _Handler()
    boundary = WebApplicationBoundary(
        authorization=MutatingAuthorization(), handler=handler,
    )
    original = _principal()
    result = boundary.dispatch(
        principal=original, command=_command({"target": "authorized"}),
    )
    executed_principal, executed_command = handler.calls[0]
    assert executed_principal is not original
    assert executed_principal == original
    assert executed_principal.tenant_id == "tenant-1"
    assert executed_principal.workspace_id == "workspace-1"
    assert executed_command.action_id == "task.create"
    assert executed_command.request_id == "request-1"
    assert executed_command.payload == {"target": "authorized"}
    assert result.data == {"action_id": "task.create", "payload": {"target": "authorized"}}


def test_authorization_cannot_mutate_caller_owned_principal_in_flight() -> None:
    original = _principal()

    class MutatingCallerAuthorization:
        def allows(self, principal: WebPrincipal, command: WebCommand) -> bool:
            del principal, command
            object.__setattr__(original, "workspace_id", "workspace-attacker")
            return True

    handler = _Handler()
    boundary = WebApplicationBoundary(
        authorization=MutatingCallerAuthorization(), handler=handler,
    )
    boundary.dispatch(principal=original, command=_command())
    assert handler.calls[0][0].workspace_id == "workspace-1"
    assert original.workspace_id == "workspace-attacker"


def test_authority_snapshot_revalidates_mutated_server_carrier_before_handler() -> None:
    principal = _principal()
    object.__setattr__(principal, "workspace_id", "bad\\nworkspace")
    handler = _Handler()
    boundary = WebApplicationBoundary(authorization=_Allow(True), handler=handler)
    with pytest.raises(ValueError, match="control text"):
        boundary.dispatch(principal=principal, command=_command())
    assert handler.calls == []


def test_unknown_effect_reports_request_identity_before_handler_mutation() -> None:
    class MutatingFailingHandler:
        def handle(self, principal: WebPrincipal, command: WebCommand) -> WebCommandResult:
            del principal
            object.__setattr__(command, "request_id", "forged-request")
            raise RuntimeError("uncertain post-effect failure")

    boundary = WebApplicationBoundary(
        authorization=_Allow(True), handler=MutatingFailingHandler(),
    )
    with pytest.raises(WebCommandOutcomeUnknownError) as caught:
        boundary.dispatch(principal=_principal(), command=_command())
    assert caught.value.request_id == "request-1"
