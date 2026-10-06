from __future__ import annotations

import asyncio

from nika_core.data.sqlite import SQLiteStore
from nika_core.intelligence.brain import DeterministicBrain
from nika_core.intelligence.contracts import (
    DeterministicAction,
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


class _OneActionPlanner:
    def plan(self, *, state, goal, actions):
        del state, goal
        action = actions[0]
        return DeterministicPlan(
            steps=(PlanStep(action_id=action.action_id, tool_id=action.tool_id),)
        )


class _InjectingRaceLedger(IdempotencyLedger):
    """Inject another task effect after our reservation but before journal dispatch."""

    def __init__(self, store: SQLiteStore) -> None:
        super().__init__(store)
        self._inject = True

    def reserve_once(
        self,
        *,
        operation_key: str,
        task_id: str,
        operation_type: str,
        input_fingerprint: str,
    ):
        record, created = super().reserve_once(
            operation_key=operation_key,
            task_id=task_id,
            operation_type=operation_type,
            input_fingerprint=input_fingerprint,
        )
        if self._inject and created:
            self._inject = False
            super().reserve_once(
                operation_key=f"competing:{task_id}",
                task_id=task_id,
                operation_type="competing.effect",
                input_fingerprint="competing-effect",
            )
        return record, created


def test_new_unresolved_effect_between_preflight_and_dispatch_blocks_handler(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    task = TaskQueue(store).create(workspace_id="proof", agent_id="deterministic")
    ledger = _InjectingRaceLedger(store)
    journal = RuntimeIdempotencyEffectJournal(ledger)
    handler_calls = 0

    async def write(_arguments: dict[str, object]) -> object:
        nonlocal handler_calls
        handler_calls += 1
        return "written"

    tools = ToolExecutor()
    tools.register(
        ToolSpec(
            tool_id="write.result",
            description="write",
            risk=ToolRisk.LOCAL_WRITE,
        ),
        write,
    )
    action = DeterministicAction(
        action_id="write-result",
        adds=frozenset({"done"}),
        tool_id="write.result",
    )
    result = asyncio.run(
        DeterministicBrain(
            planner=_OneActionPlanner(),
            tools=tools,
            effect_journal=journal,
        ).run(
            run_id="concurrent-effect-race",
            task_id=task.task_id,
            state=WorldState(),
            goal=DeterministicGoal(required=frozenset({"done"})),
            actions=(action,),
        )
    )

    assert result.error_code == DeterministicErrorCode.SIDE_EFFECT_RECONCILIATION_REQUIRED
    assert handler_calls == 0
    records = ledger.list_for_task(task.task_id)
    assert len(records) == 1
    assert records[0].operation_key == f"competing:{task.task_id}"
    assert records[0].status == IdempotencyStatus.PENDING
