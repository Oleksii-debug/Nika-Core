from __future__ import annotations

import asyncio

import pytest

import nika_core.tools as tool_module

SPEC = tool_module.ToolSpec(
    tool_id="external.publish",
    description="Requires exact host authorization",
    risk=tool_module.ToolRisk.EXTERNAL_SIDE_EFFECT,
)


def _authorization() -> tool_module.ToolAuthorization:
    return tool_module.ToolAuthorization(
        tool_id=SPEC.tool_id,
        task_id="task",
        risk=SPEC.risk,
        arguments_fingerprint=tool_module.tool_arguments_fingerprint({}),
        effect_fingerprint="effect-1",
        approval_fingerprint="approval-1",
    )


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("approval_fingerprint", ""),
        ("effect_fingerprint", ""),
        ("approval_fingerprint", None),
        ("effect_fingerprint", []),
        ("approval_fingerprint", chr(0xD800)),
        ("effect_fingerprint", "x" * 513),
        ("task_id", True),
        ("risk", "external_side_effect"),
    ],
)
def test_mutated_authorization_cannot_reach_guard_or_handler(
    field: str,
    invalid: object,
) -> None:
    visited: list[str] = []
    authorization = _authorization()
    object.__setattr__(authorization, field, invalid)

    async def policy(
        _spec: tool_module.ToolSpec,
        _call: tool_module.ToolCall,
    ) -> tool_module.ToolAuthorization:
        visited.append("policy")
        return authorization

    async def handler(_arguments: dict[str, object]) -> object:
        visited.append("handler")
        return {"published": True}

    executor = tool_module.ToolExecutor(approval_policy=policy)
    executor.register(SPEC, handler)
    call = tool_module.ToolCall(
        call_id="call-1",
        tool_id=SPEC.tool_id,
        task_id="task",
        arguments={},
    )
    result = asyncio.run(executor.execute(call))

    assert result.error == "approval required"
    assert visited == ["policy"]
    assert authorization.matches(spec=SPEC, call=call) is False


@pytest.mark.parametrize("invalid", ["", None, [], chr(0xD800), "x" * 513])
def test_direct_guard_rejects_mutated_authorization_before_reservation(
    invalid: object,
) -> None:
    class TrapLedger:
        def reserve_once(self, **_kwargs: object) -> None:
            pytest.fail("invalid authorization reached durable reservation")

    authorization = _authorization()
    object.__setattr__(authorization, "approval_fingerprint", invalid)
    call = tool_module.ToolCall(
        call_id="call-1",
        tool_id=SPEC.tool_id,
        task_id="task",
        arguments={},
        authorization=authorization,
    )
    guard = tool_module.ToolEffectGuard(TrapLedger())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="invalid durable host authorization"):
        guard.reserve(spec=SPEC, call=call)


def test_valid_authorization_still_matches_after_snapshot() -> None:
    call = tool_module.ToolCall(
        call_id="call-1",
        tool_id=SPEC.tool_id,
        task_id="task",
        arguments={},
    )
    assert _authorization().matches(spec=SPEC, call=call)


def test_mutation_during_fingerprint_check_cannot_rewrite_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authorization = _authorization()
    object.__setattr__(authorization, "arguments_fingerprint", "not-approved")
    expected = tool_module.tool_arguments_fingerprint({})

    def mutate_during_check(_arguments: object) -> str:
        object.__setattr__(authorization, "arguments_fingerprint", expected)
        return expected

    monkeypatch.setattr(
        "nika_core.tools.tool_arguments_fingerprint",
        mutate_during_check,
    )
    call = tool_module.ToolCall(
        call_id="call-1",
        tool_id=SPEC.tool_id,
        task_id="task",
        arguments={},
    )
    assert not authorization.matches(spec=SPEC, call=call)
    assert authorization.arguments_fingerprint == expected


@pytest.mark.parametrize(
    "field",
    ["tool_id", "task_id", "approval_fingerprint"],
)
def test_authorization_constructor_rejects_invalid_text(field: str) -> None:
    arguments = {
        "tool_id": SPEC.tool_id,
        "task_id": "task",
        "risk": SPEC.risk,
        "arguments_fingerprint": tool_module.tool_arguments_fingerprint({}),
        "effect_fingerprint": "effect-1",
        "approval_fingerprint": "approval-1",
    }
    arguments[field] = chr(0xD800)
    with pytest.raises(ValueError, match="UTF-8"):
        tool_module.ToolAuthorization(**arguments)
