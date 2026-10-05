from __future__ import annotations

import asyncio

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.runtime.idempotency import IdempotencyLedger, IdempotencyStatus
from nika_core.tools import (
    ToolAuthorization,
    ToolCall,
    ToolEffectConflictError,
    ToolEffectGuard,
    ToolExecutor,
    ToolRisk,
    ToolSpec,
    tool_arguments_fingerprint,
)

_OPERATION_TYPE = "tool.external_effect"
def _guard_bundle(tmp_path):
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    task = TaskQueue(store).create(workspace_id="proof", agent_id="tool-effect")
    ledger = IdempotencyLedger(store)
    return task, store, ledger, ToolEffectGuard(ledger)


def _spec() -> ToolSpec:
    return ToolSpec(
        tool_id="publish",
        description="publish externally",
        risk=ToolRisk.EXTERNAL_SIDE_EFFECT,
    )


def _call(task_id: str) -> ToolCall:
    return ToolCall(
        call_id="publish-once",
        tool_id="publish",
        task_id=task_id,
        arguments={"value": "hello"},
    )


def _rebind_exact_reservation(
    store: SQLiteStore,
    ledger: IdempotencyLedger,
    *,
    operation_key: str,
    task_id: str,
    input_fingerprint: str,
) -> None:
    original = ledger.require(operation_key)
    ledger.release_pending(operation_key)
    record, created = ledger.reserve_once(
        operation_key=operation_key,
        task_id=task_id,
        operation_type=_OPERATION_TYPE,
        input_fingerprint=input_fingerprint,
    )
    assert created
    assert record.status is IdempotencyStatus.PENDING
    assert record.reservation_generation != original.reservation_generation
    with store.connection() as conn:
        conn.execute(
            "UPDATE idempotency_records SET created_at = ? WHERE operation_key = ?",
            (original.created_at, operation_key),
        )


@pytest.mark.parametrize("mutation_name", ["complete", "mark_uncertain"])
def test_tool_guard_finalizers_reject_exact_semantic_rebind(
    tmp_path,
    mutation_name: str,
) -> None:
    task, store, ledger, guard = _guard_bundle(tmp_path)
    reservation = guard.reserve(spec=_spec(), call=_call(task.task_id))
    original = ledger.require(reservation.operation_key)

    _rebind_exact_reservation(
        store,
        ledger,
        operation_key=reservation.operation_key,
        task_id=task.task_id,
        input_fingerprint=original.input_fingerprint,
    )

    with pytest.raises(
        ToolEffectConflictError,
        match="reservation changed|lacks reservation authority",
    ):
        if mutation_name == "complete":
            guard.complete(reservation, {"published": True})
        else:
            guard.mark_uncertain(reservation)

    rebound = ledger.require(reservation.operation_key)
    assert rebound.input_fingerprint == original.input_fingerprint
    assert rebound.created_at == original.created_at
    assert rebound.reservation_generation != original.reservation_generation
    assert rebound.status is IdempotencyStatus.PENDING


def test_tool_executor_cannot_finalize_rebound_external_effect(tmp_path) -> None:
    task, store, ledger, guard = _guard_bundle(tmp_path)
    calls = 0

    async def approve(spec: ToolSpec, call: ToolCall) -> ToolAuthorization:
        return ToolAuthorization(
            tool_id=spec.tool_id,
            task_id=call.task_id or "",
            risk=spec.risk,
            arguments_fingerprint=tool_arguments_fingerprint(call.arguments),
            effect_fingerprint="effect-v1",
            approval_fingerprint="approval-v1",
        )

    async def publish(_arguments: dict[str, object]) -> object:
        nonlocal calls
        calls += 1
        records = ledger.list_for_task(task.task_id)
        assert len(records) == 1
        current = records[0]
        _rebind_exact_reservation(
            store,
            ledger,
            operation_key=current.operation_key,
            task_id=task.task_id,
            input_fingerprint=current.input_fingerprint,
        )
        return {"published": True}

    executor = ToolExecutor(approval_policy=approve, effect_guard=guard)
    executor.register(_spec(), publish)

    result = asyncio.run(executor.execute(_call(task.task_id)))

    assert calls == 1
    assert not result.ok
    assert result.error == "tool result durability failed"
    rebound = ledger.list_for_task(task.task_id)
    assert len(rebound) == 1
    assert rebound[0].status is IdempotencyStatus.PENDING
