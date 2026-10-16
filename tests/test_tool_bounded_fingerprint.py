from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping

import pytest

from nika_core.security import ActionIntent
from nika_core.tools import (
    ToolAuthorization,
    ToolCall,
    ToolEffectGuard,
    ToolExecutor,
    ToolRisk,
    ToolSpec,
    tool_arguments_fingerprint,
)


def _invalid_arguments(kind: str) -> dict[str, object]:
    if kind == "cycle":
        cycle: list[object] = []
        cycle.append(cycle)
        return {"payload": cycle}
    if kind == "depth":
        nested: object = "leaf"
        for _ in range(65):
            nested = [nested]
        return {"payload": nested}
    if kind == "width":
        return {"payload": [0] * 20_001}
    if kind == "bytes":
        return {"payload": "x" * (8 * 1024 * 1024 + 1)}
    if kind == "unicode":
        return {"payload": chr(0xD800)}
    raise AssertionError(kind)


def _spec() -> ToolSpec:
    return ToolSpec(tool_id="external.test", description="Test external tool",
                    risk=ToolRisk.EXTERNAL_SIDE_EFFECT)


def _authorization() -> ToolAuthorization:
    return ToolAuthorization(
        tool_id="external.test",
        task_id="task-1",
        risk=ToolRisk.EXTERNAL_SIDE_EFFECT,
        arguments_fingerprint="previously-authorized",
        effect_fingerprint="effect-1",
        approval_fingerprint="approval-1",
    )


@pytest.mark.parametrize("kind", ("cycle", "depth", "width", "bytes", "unicode"))
def test_direct_fingerprint_rejects_unbounded_arguments(kind: str) -> None:
    arguments = _invalid_arguments(kind)
    with pytest.raises(ValueError):
        tool_arguments_fingerprint(arguments)

    call = ToolCall(call_id="call-1", tool_id="external.test",
                    task_id="task-1", arguments=arguments)
    # A direct guard caller must fail before it can reserve a durable effect.
    with pytest.raises(ValueError, match="durable tool arguments"):
        ToolEffectGuard._fingerprint(spec=_spec(), call=call)
    assert not _authorization().matches(spec=_spec(), call=call)


def test_direct_fingerprint_uses_action_intent_canonical_json() -> None:
    arguments = {"names": [{"e\u0301": ["Київ", "😀"]}], "count": 1}
    canonical = {"names": [{"é": ["Київ", "😀"]}], "count": 1}
    intent = ActionIntent(
        action_id="action-1",
        tool_id="external.test",
        risk=ToolRisk.EXTERNAL_SIDE_EFFECT,
        target="report",
        arguments=arguments,
    )
    assert tool_arguments_fingerprint(arguments) == tool_arguments_fingerprint(canonical)
    assert tool_arguments_fingerprint(arguments) == intent.arguments_fingerprint
    expected = hashlib.sha256(
        json.dumps(canonical, allow_nan=False, ensure_ascii=False,
                   sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    assert tool_arguments_fingerprint(arguments) == expected


def test_direct_fingerprint_rejects_normalized_duplicate_keys() -> None:
    with pytest.raises(ValueError, match="duplicate normalized key"):
        tool_arguments_fingerprint({"e\u0301": 1, "é": 2})


def test_invalid_arguments_cannot_turn_host_policy_into_tool_execution() -> None:
    called = 0

    async def policy(_spec: ToolSpec, _call: ToolCall) -> ToolAuthorization:
        return _authorization()

    async def handler(_arguments: dict[str, object]) -> object:
        nonlocal called
        called += 1
        return {"unsafe": True}

    executor = ToolExecutor(approval_policy=policy)
    executor.register(_spec(), handler)
    call = ToolCall(
        call_id="call-1", tool_id="external.test", task_id="task-1",
        arguments=_invalid_arguments("cycle"),
    )
    result = asyncio.run(executor.execute(call))
    assert result.error == "approval required"
    assert called == 0


class CrashingMapping(Mapping[str, object]):
    def __getitem__(self, key: str) -> object:
        raise RuntimeError("untrusted mapping lookup")

    def __iter__(self):
        raise RuntimeError("untrusted mapping iteration")

    def __len__(self) -> int:
        return 1


def test_hostile_mapping_is_denied_by_all_direct_tool_boundaries() -> None:
    arguments = CrashingMapping()
    with pytest.raises(ValueError, match="deterministic JSON-compatible"):
        tool_arguments_fingerprint(arguments)

    call = ToolCall(
        call_id="call-1", tool_id="external.test", task_id="task-1",
        arguments=arguments,  # type: ignore[arg-type] - untrusted runtime carrier
    )
    assert not _authorization().matches(spec=_spec(), call=call)
    with pytest.raises(ValueError, match="durable tool arguments"):
        ToolEffectGuard._fingerprint(spec=_spec(), call=call)


@pytest.mark.parametrize("kind", ("cycle", "width"))
def test_guard_rejects_invalid_arguments_without_reserving_an_effect(kind: str) -> None:
    class ReservationTrap:
        def reserve_once(self, **_kwargs: object) -> None:
            pytest.fail("the ledger must not be reached before argument admission")

    guard = ToolEffectGuard(ReservationTrap())  # type: ignore[arg-type]
    call = ToolCall(
        call_id="call-1", tool_id="external.test", task_id="task-1",
        arguments=_invalid_arguments(kind),
    )
    with pytest.raises(ValueError, match="durable tool arguments"):
        guard.reserve(spec=_spec(), call=call)
