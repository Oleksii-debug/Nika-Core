from __future__ import annotations

import asyncio

import pytest

from nika_core.tools import (
    ToolAuthorization,
    ToolCall,
    ToolEffectGuard,
    ToolEffectReservation,
    ToolExecutor,
    ToolRisk,
    ToolSpec,
    tool_arguments_fingerprint,
)

SPEC = ToolSpec(
    tool_id="external.publish",
    description="Publish a report after host approval",
    risk=ToolRisk.EXTERNAL_SIDE_EFFECT,
)

INVALID_IDENTITIES = (
    None,
    7,
    True,
    [],
    {},
    "",
    "   ",
    chr(0xD800),
    "x" * 513,
    "😀" * 129,
)


@pytest.mark.parametrize("invalid", INVALID_IDENTITIES)
def test_malformed_tool_id_never_reaches_policy_or_handler(invalid: object) -> None:
    visited: list[str] = []

    async def policy(_spec: ToolSpec, _call: ToolCall) -> bool:
        visited.append("policy")
        return True

    async def handler(_args: dict[str, object]) -> object:
        visited.append("handler")
        return {"sent": True}

    executor = ToolExecutor(approval_policy=policy)
    executor.register(SPEC, handler)
    result = asyncio.run(
        executor.execute(
            ToolCall(
                call_id="call-1",
                tool_id=invalid,  # type: ignore[arg-type] - hostile runtime carrier
                task_id="task-1",
                arguments={"command": "safe"},
            )
        )
    )
    assert result.error == "invalid tool id"
    assert result.tool_id == ""
    assert visited == []


@pytest.mark.parametrize("invalid", (*INVALID_IDENTITIES, "external.delete"))
def test_direct_guard_rejects_unmatched_tool_id_before_reservation(
    invalid: object,
) -> None:
    class ReservationTrap:
        def reserve_once(self, **_kwargs: object) -> None:
            pytest.fail("malformed or mismatched tool ID reached durable reservation")

    guard = ToolEffectGuard(ReservationTrap())  # type: ignore[arg-type]
    call = ToolCall(
        call_id="call-1",
        tool_id=invalid,  # type: ignore[arg-type] - hostile runtime carrier
        task_id="task-1",
        arguments={"command": "safe"},
    )
    with pytest.raises(ValueError, match="tool_id"):
        guard.reserve(spec=SPEC, call=call)


def test_well_formed_unknown_tool_remains_a_controlled_unknown_tool() -> None:
    executor = ToolExecutor()
    result = asyncio.run(
        executor.execute(
            ToolCall(call_id="call-1", tool_id="other.tool", arguments={})
        )
    )
    assert result.error == "unknown tool"
    assert result.tool_id == "other.tool"


@pytest.mark.parametrize("invalid", INVALID_IDENTITIES)
def test_tool_spec_rejects_invalid_ids_before_registration(invalid: object) -> None:
    with pytest.raises(ValueError, match="tool_id"):
        ToolSpec(
            tool_id=invalid,  # type: ignore[arg-type] - untrusted spec carrier
            description="untrusted discovery",
        )


def test_tool_spec_accepts_exact_utf8_byte_boundary() -> None:
    boundary = "😀" * 128  # exactly 512 UTF-8 bytes
    spec = ToolSpec(tool_id=boundary, description="valid boundary")
    executor = ToolExecutor()

    async def handler(_args: dict[str, object]) -> object:
        return {"ok": True}

    executor.register(spec, handler)
    assert executor.specs() == (spec,)


def test_registered_tool_cannot_be_downgraded_through_original_spec() -> None:
    executed: list[str] = []

    async def handler(_args: dict[str, object]) -> object:
        executed.append("external_effect")
        return {"published": True}

    schema = {"properties": {"text": {"type": "string"}}}
    spec = ToolSpec(
        tool_id="external.publish",
        description="Requires approval",
        risk=ToolRisk.EXTERNAL_SIDE_EFFECT,
        input_schema=schema,
    )
    executor = ToolExecutor()
    executor.register(spec, handler)

    # Frozen dataclasses do not make caller-owned references an authority boundary.
    object.__setattr__(spec, "risk", ToolRisk.READ_ONLY)
    object.__setattr__(spec, "timeout_seconds", 86_400)
    object.__setattr__(spec, "tool_id", "changed.tool")
    schema["properties"]["text"]["type"] = "integer"

    admitted = executor.specs()[0]
    assert admitted.tool_id == "external.publish"
    assert admitted.risk is ToolRisk.EXTERNAL_SIDE_EFFECT
    assert admitted.timeout_seconds == 30.0
    assert admitted.input_schema["properties"]["text"]["type"] == "string"

    result = asyncio.run(
        executor.execute(
            ToolCall(call_id="call-1", tool_id="external.publish", arguments={})
        )
    )
    assert result.error == "approval required"
    assert executed == []


def test_catalog_cannot_mutate_registered_tool_authority() -> None:
    executed: list[str] = []

    async def handler(_args: dict[str, object]) -> object:
        executed.append("external_effect")
        return {"published": True}

    executor = ToolExecutor()
    executor.register(
        ToolSpec(
            tool_id="external.publish",
            description="Requires approval",
            risk=ToolRisk.HIGH_IMPACT,
            input_schema={"properties": {"text": {"type": "string"}}},
        ),
        handler,
    )
    published = executor.specs()[0]
    object.__setattr__(published, "risk", ToolRisk.READ_ONLY)
    object.__setattr__(published, "tool_id", "changed.tool")
    published.input_schema["properties"]["text"]["type"] = "integer"

    retained = executor.specs()[0]
    assert retained.tool_id == "external.publish"
    assert retained.risk is ToolRisk.HIGH_IMPACT
    assert retained.input_schema["properties"]["text"]["type"] == "string"
    result = asyncio.run(
        executor.execute(
            ToolCall(call_id="call-2", tool_id="external.publish", arguments={})
        )
    )
    assert result.error == "approval required"
    assert executed == []


def test_registration_revalidates_spec_mutated_after_construction() -> None:
    spec = ToolSpec(tool_id="valid.tool", description="valid at construction")
    object.__setattr__(spec, "tool_id", "x" * 513)
    executor = ToolExecutor()

    async def handler(_args: dict[str, object]) -> object:
        return {"ok": True}

    with pytest.raises(ValueError, match="tool_id"):
        executor.register(spec, handler)
    assert executor.specs() == ()


def test_policy_cannot_mutate_registered_risk_or_schema() -> None:
    executed: list[str] = []

    async def policy(spec: ToolSpec, _call: ToolCall) -> bool:
        # A callback must not be able to rewrite the registry's authority.
        object.__setattr__(spec, "risk", ToolRisk.READ_ONLY)
        object.__setattr__(spec, "tool_id", "rewritten.tool")
        spec.input_schema["properties"]["body"]["type"] = "integer"
        return True  # Compatibility bool is not exact-effect authorization.

    async def handler(_arguments: dict[str, object]) -> object:
        executed.append("published")
        return {"published": True}

    executor = ToolExecutor(approval_policy=policy)
    executor.register(
        ToolSpec(
            tool_id="external.publish",
            description="Requires host authorization",
            risk=ToolRisk.EXTERNAL_SIDE_EFFECT,
            input_schema={"properties": {"body": {"type": "string"}}},
        ),
        handler,
    )
    for number in (1, 2):
        result = asyncio.run(
            executor.execute(
                ToolCall(
                    call_id=f"policy-{number}",
                    tool_id="external.publish",
                    task_id="task",
                    arguments={},
                )
            )
        )
        assert result.error == "approval required"
    retained = executor.specs()[0]
    assert retained.tool_id == "external.publish"
    assert retained.risk is ToolRisk.EXTERNAL_SIDE_EFFECT
    assert retained.input_schema["properties"]["body"]["type"] == "string"
    assert executed == []


def test_effect_guard_cannot_downgrade_registered_risk_for_next_call() -> None:
    executed: list[str] = []
    policy_calls = 0

    async def policy(spec: ToolSpec, call: ToolCall) -> ToolAuthorization | None:
        nonlocal policy_calls
        policy_calls += 1
        if policy_calls > 1:
            return None
        return ToolAuthorization(
            tool_id=spec.tool_id,
            task_id=call.task_id or "",
            risk=spec.risk,
            arguments_fingerprint=tool_arguments_fingerprint(call.arguments),
            effect_fingerprint="effect-1",
            approval_fingerprint="approval-1",
        )

    class MutatingGuard:
        def reserve(self, *, spec: ToolSpec, call: ToolCall) -> ToolEffectReservation:
            object.__setattr__(spec, "risk", ToolRisk.READ_ONLY)
            object.__setattr__(spec, "tool_id", "rewritten.tool")
            spec.input_schema["properties"]["body"]["type"] = "integer"
            return ToolEffectReservation(
                operation_key="replayed-effect",
                completed_result={"output": {"replayed": True}},
            )

    async def handler(_arguments: dict[str, object]) -> object:
        executed.append("published")
        return {"published": True}

    executor = ToolExecutor(
        approval_policy=policy,
        effect_guard=MutatingGuard(),  # type: ignore[arg-type] - adversarial seam
    )
    executor.register(
        ToolSpec(
            tool_id="external.publish",
            description="Requires host authorization",
            risk=ToolRisk.HIGH_IMPACT,
            input_schema={"properties": {"body": {"type": "string"}}},
        ),
        handler,
    )
    first = asyncio.run(
        executor.execute(
            ToolCall(call_id="guard-1", tool_id="external.publish", task_id="task", arguments={})
        )
    )
    assert first.ok and first.output == {"replayed": True}
    second = asyncio.run(
        executor.execute(
            ToolCall(call_id="guard-2", tool_id="external.publish", task_id="task", arguments={})
        )
    )
    assert second.error == "approval required"
    retained = executor.specs()[0]
    assert retained.tool_id == "external.publish"
    assert retained.risk is ToolRisk.HIGH_IMPACT
    assert retained.input_schema["properties"]["body"]["type"] == "string"
    assert policy_calls == 2
    assert executed == []

@pytest.mark.parametrize(
    ("schema", "error"),
    [
        ({"properties": {"name": {"default": object()}}}, (TypeError, ValueError)),
        ({"title": "\ud800"}, (TypeError, ValueError)),
        ({"limit": float("nan")}, (TypeError, ValueError)),
        (["non-object", "schema"], (TypeError, ValueError)),
    ],
    ids=["non-json-object", "invalid-utf8", "nonfinite", "non-object-root"],
)
def test_registration_rejects_invalid_schema_before_catalog(
    schema: object, error: tuple[type[Exception], ...]
) -> None:
    executor = ToolExecutor()

    async def handler(_arguments: dict[str, object]) -> object:
        pytest.fail("invalid schema must never execute")

    spec = ToolSpec(
        tool_id="schema.test",
        description="schema admission boundary",
        input_schema=schema,  # type: ignore[arg-type] - hostile discovery carrier
    )
    with pytest.raises(error):
        executor.register(spec, handler)
    assert executor.specs() == ()


@pytest.mark.parametrize("failure", ["cyclic", "too_deep", "too_large"])
def test_registration_bounds_recursive_and_oversized_schemas(failure: str) -> None:
    schema: dict[str, object] = {}
    if failure == "cyclic":
        schema["nested"] = schema
    elif failure == "too_deep":
        for _ in range(70):
            schema = {"nested": schema}
    else:
        schema = {"description": "x" * (8 * 1024 * 1024 + 1)}
    executor = ToolExecutor()

    async def handler(_arguments: dict[str, object]) -> object:
        pytest.fail("unbounded schema must never execute")

    with pytest.raises(ValueError, match="maximum argument"):
        executor.register(
            ToolSpec(tool_id="schema.test", description="bounded schema", input_schema=schema),
            handler,
        )
    assert executor.specs() == ()


def test_registered_schema_has_detached_canonical_unicode_identity() -> None:
    schema = {"properties": {"cafe\u0301": {"title": "cafe\u0301"}}}
    executor = ToolExecutor()

    async def handler(_arguments: dict[str, object]) -> object:
        return {"ok": True}

    executor.register(
        ToolSpec(tool_id="schema.test", description="canonical schema", input_schema=schema),
        handler,
    )
    schema["properties"]["cafe\u0301"]["title"] = "changed"
    registered = executor.specs()[0]
    assert registered.input_schema == {"properties": {"café": {"title": "café"}}}
    registered.input_schema["properties"]["café"]["title"] = "catalog mutation"
    assert executor.specs()[0].input_schema == {
        "properties": {"café": {"title": "café"}}
    }
