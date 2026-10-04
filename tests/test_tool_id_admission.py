from __future__ import annotations

import asyncio

import pytest

from nika_core.tools import ToolCall, ToolEffectGuard, ToolExecutor, ToolRisk, ToolSpec

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
