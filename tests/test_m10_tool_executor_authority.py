from __future__ import annotations

import asyncio

from nika_core.tools import ToolCall, ToolExecutor, ToolRisk, ToolSpec


async def _handler(arguments: dict[str, object]) -> object:
    return {"executed": True, "arguments": arguments}


def _run(executor: ToolExecutor, call: ToolCall):
    return asyncio.run(executor.execute(call))


def test_caller_approved_true_is_not_external_effect_authority() -> None:
    executor = ToolExecutor()
    executor.register(
        ToolSpec(
            tool_id="external.effect",
            description="external effect",
            risk=ToolRisk.EXTERNAL_SIDE_EFFECT,
        ),
        _handler,
    )

    result = _run(
        executor,
        ToolCall(
            call_id="caller-forgery",
            tool_id="external.effect",
            arguments={"target": "outside"},
            approved=True,
        ),
    )

    assert result.ok is False
    assert result.error == "approval required"


def test_caller_approved_true_cannot_override_host_denial() -> None:
    async def deny(_spec: ToolSpec, _call: ToolCall) -> bool:
        return False

    executor = ToolExecutor(approval_policy=deny)
    executor.register(
        ToolSpec(
            tool_id="high.impact",
            description="high impact",
            risk=ToolRisk.HIGH_IMPACT,
        ),
        _handler,
    )

    result = _run(
        executor,
        ToolCall(
            call_id="caller-bypass",
            tool_id="high.impact",
            arguments={},
            approved=True,
        ),
    )

    assert result.ok is False
    assert result.error == "approval required"


def test_only_host_injected_policy_can_authorize_effect() -> None:
    seen: list[tuple[str, str]] = []

    async def approve(spec: ToolSpec, call: ToolCall) -> bool:
        seen.append((spec.tool_id, call.call_id))
        return spec.tool_id == "external.effect" and call.call_id == "host-approved"

    executor = ToolExecutor(approval_policy=approve)
    executor.register(
        ToolSpec(
            tool_id="external.effect",
            description="external effect",
            risk=ToolRisk.EXTERNAL_SIDE_EFFECT,
        ),
        _handler,
    )

    result = _run(
        executor,
        ToolCall(
            call_id="host-approved",
            tool_id="external.effect",
            arguments={"value": 7},
            approved=False,
        ),
    )

    assert result.ok is True
    assert result.output == {"executed": True, "arguments": {"value": 7}}
    assert seen == [("external.effect", "host-approved")]


def test_read_only_tool_does_not_need_human_authority() -> None:
    executor = ToolExecutor()
    executor.register(
        ToolSpec(
            tool_id="read.only",
            description="read only",
            risk=ToolRisk.READ_ONLY,
        ),
        _handler,
    )

    result = _run(
        executor,
        ToolCall(call_id="read", tool_id="read.only", arguments={"query": "safe"}),
    )

    assert result.ok is True
