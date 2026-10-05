from __future__ import annotations

import asyncio

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.intelligence.brain import DeterministicBrain
from nika_core.intelligence.contracts import (
    DeterministicAction,
    DeterministicEffectConflictError,
    DeterministicErrorCode,
    DeterministicGoal,
    DeterministicPlan,
    PlanStep,
    WorldState,
)
from nika_core.intelligence.runtime_effect_journal import RuntimeIdempotencyEffectJournal
from nika_core.kernel.task_queue import TaskQueue
from nika_core.runtime.idempotency import IdempotencyLedger, IdempotencyStatus
from nika_core.tools import ToolExecutor, ToolRisk, ToolSpec

_OPERATION_TYPE = "deterministic.tool_action"
_FOREIGN_FINGERPRINT = "foreign-rebound-operation"


def _task_and_ledger(tmp_path):
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    task = TaskQueue(store).create(workspace_id="proof", agent_id="deterministic")
    return task, IdempotencyLedger(store)


def _local_write_action() -> DeterministicAction:
    return DeterministicAction(
        action_id="write-result",
        adds=frozenset({"done"}),
        tool_id="write.result",
        arguments={"path": "result.txt"},
    )


def _rebind_reservation(
    ledger: IdempotencyLedger,
    *,
    operation_key: str,
    task_id: str,
) -> None:
    ledger.release_pending(operation_key)
    record, created = ledger.reserve_once(
        operation_key=operation_key,
        task_id=task_id,
        operation_type=_OPERATION_TYPE,
        input_fingerprint=_FOREIGN_FINGERPRINT,
    )
    assert created
    assert record.status is IdempotencyStatus.PENDING


@pytest.mark.parametrize("mutation_name", ["complete", "mark_uncertain", "release_pending"])
def test_journal_finalizers_reject_rebound_operation(
    tmp_path,
    mutation_name: str,
) -> None:
    task, ledger = _task_and_ledger(tmp_path)
    journal = RuntimeIdempotencyEffectJournal(ledger)
    reservation = journal.reserve(
        task_id=task.task_id,
        action=_local_write_action(),
    )
    assert reservation.created

    _rebind_reservation(
        ledger,
        operation_key=reservation.operation_key,
        task_id=task.task_id,
    )

    with pytest.raises(
        DeterministicEffectConflictError,
        match="reservation changed|lacks reservation authority",
    ):
        getattr(journal, mutation_name)(reservation.operation_key)

    rebound = ledger.require(reservation.operation_key)
    assert rebound.task_id == task.task_id
    assert rebound.operation_type == _OPERATION_TYPE
    assert rebound.input_fingerprint == _FOREIGN_FINGERPRINT
    assert rebound.status is IdempotencyStatus.PENDING


def test_brain_cannot_complete_rebound_local_write_effect(tmp_path) -> None:
    task, ledger = _task_and_ledger(tmp_path)
    journal = RuntimeIdempotencyEffectJournal(ledger)
    action = _local_write_action()
    effect_calls = 0

    class OneStepPlanner:
        def plan(self, *, state, goal, actions) -> DeterministicPlan:
            del state, goal, actions
            return DeterministicPlan(
                steps=(PlanStep(action_id=action.action_id, tool_id=action.tool_id),)
            )

    async def write_result(_arguments: dict[str, object]) -> object:
        nonlocal effect_calls
        effect_calls += 1
        records = ledger.list_for_task(task.task_id)
        assert len(records) == 1
        _rebind_reservation(
            ledger,
            operation_key=records[0].operation_key,
            task_id=task.task_id,
        )
        return {"written": True}

    executor = ToolExecutor()
    executor.register(
        ToolSpec(
            tool_id="write.result",
            description="write one deterministic result",
            risk=ToolRisk.LOCAL_WRITE,
        ),
        write_result,
    )

    result = asyncio.run(
        DeterministicBrain(
            planner=OneStepPlanner(),
            tools=executor,
            effect_journal=journal,
        ).run(
            run_id="rebind-proof",
            task_id=task.task_id,
            state=WorldState(),
            goal=DeterministicGoal(required=frozenset({"done"})),
            actions=(action,),
        )
    )

    assert effect_calls == 1
    assert not result.ok
    assert result.error_code is DeterministicErrorCode.SIDE_EFFECT_IDENTITY_CONFLICT
    assert result.completed_actions == ()
    assert result.final_state.facts == frozenset()

    rebound = ledger.list_for_task(task.task_id)
    assert len(rebound) == 1
    assert rebound[0].input_fingerprint == _FOREIGN_FINGERPRINT
    assert rebound[0].status is IdempotencyStatus.PENDING
