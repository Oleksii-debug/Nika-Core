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
from nika_core.kernel.audit import AuditLog
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState
from nika_core.runtime.idempotency import IdempotencyLedger, IdempotencyStatus
from nika_core.runtime.recovery import RecoveryDisposition, RuntimeRecoveryService
from nika_core.runtime.registry import RuntimeRegistry
from nika_core.runtime.session_store import RuntimeSessionStore
from nika_core.tools import ToolExecutor, ToolRisk, ToolSpec


class _SimulatedProcessLoss(BaseException):
    """Bypass normal exception handling like abrupt process termination."""


class _OneActionPlanner:
    def plan(self, *, state, goal, actions):
        del state, goal
        if not actions:
            return DeterministicPlan(steps=())
        action = actions[0]
        return DeterministicPlan(
            steps=(PlanStep(action_id=action.action_id, tool_id=action.tool_id),)
        )


class _CrashAfterCompleteJournal:
    def __init__(self, delegate: RuntimeIdempotencyEffectJournal) -> None:
        self._delegate = delegate

    def unresolved_operation_keys(self, *, task_id):
        return self._delegate.unresolved_operation_keys(task_id=task_id)

    def reserve(self, *, task_id, action):
        return self._delegate.reserve(task_id=task_id, action=action)

    def complete(self, operation_key: str) -> None:
        self._delegate.complete(operation_key)
        raise _SimulatedProcessLoss()

    def mark_uncertain(self, operation_key: str) -> None:
        self._delegate.mark_uncertain(operation_key)

    def release_pending(self, operation_key: str) -> None:
        self._delegate.release_pending(operation_key)


def _store(tmp_path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    return store


def _task_id(store: SQLiteStore) -> str:
    return TaskQueue(store).create(workspace_id="proof", agent_id="deterministic").task_id


def _action(*, arguments: dict[str, object] | None = None) -> DeterministicAction:
    return DeterministicAction(
        action_id="write-result",
        adds=frozenset({"done"}),
        tool_id="write.result",
        arguments=arguments or {"target": "result.txt"},
    )


def _brain(
    *,
    tools: ToolExecutor,
    journal: RuntimeIdempotencyEffectJournal | _CrashAfterCompleteJournal,
) -> DeterministicBrain:
    return DeterministicBrain(
        planner=_OneActionPlanner(),
        tools=tools,
        effect_journal=journal,
    )


def _run(
    brain: DeterministicBrain,
    action: DeterministicAction,
    *,
    task_id: str,
):
    return asyncio.run(
        brain.run(
            run_id="attempt-local",
            task_id=task_id,
            state=WorldState(),
            goal=DeterministicGoal(required=frozenset({"done"})),
            actions=(action,),
        )
    )


def test_process_loss_after_tool_effect_leaves_pending_and_blocks_replay(tmp_path) -> None:
    store = _store(tmp_path)
    queue = TaskQueue(store)
    task = queue.create(workspace_id="proof", agent_id="deterministic")
    queue.transition(task.task_id, TaskState.READY)
    queue.transition(task.task_id, TaskState.RUNNING)
    RuntimeSessionStore(store).record_active(
        task_id=task.task_id,
        runtime_id="deterministic-proof",
        thread_id="thread-1",
        resume_token="thread-1",
    )
    ledger = IdempotencyLedger(store)
    journal = RuntimeIdempotencyEffectJournal(ledger)
    effects = 0

    async def effect_then_die(_arguments: dict[str, object]) -> object:
        nonlocal effects
        effects += 1
        raise _SimulatedProcessLoss()

    first_tools = ToolExecutor()
    first_tools.register(
        ToolSpec(
            tool_id="write.result",
            description="write",
            risk=ToolRisk.LOCAL_WRITE,
        ),
        effect_then_die,
    )

    try:
        _run(_brain(tools=first_tools, journal=journal), _action(), task_id=task.task_id)
    except _SimulatedProcessLoss:
        pass
    else:  # pragma: no cover - abrupt-loss fixture must escape the brain
        raise AssertionError("simulated process loss did not escape")

    records = ledger.list_for_task(task.task_id)
    assert len(records) == 1
    assert records[0].status == IdempotencyStatus.PENDING
    assert effects == 1

    candidate = RuntimeRecoveryService(
        queue=queue,
        audit=AuditLog(store),
        runtimes=RuntimeRegistry(),
    ).inspect()[0]
    assert candidate.disposition == RecoveryDisposition.RECONCILE_SIDE_EFFECTS
    assert candidate.unresolved_operation_keys == (records[0].operation_key,)

    async def must_not_repeat(_arguments: dict[str, object]) -> object:
        nonlocal effects
        effects += 1
        return "repeated"

    restart_tools = ToolExecutor()
    restart_tools.register(
        ToolSpec(
            tool_id="write.result",
            description="write",
            risk=ToolRisk.LOCAL_WRITE,
        ),
        must_not_repeat,
    )
    result = _run(
        _brain(tools=restart_tools, journal=journal),
        _action(),
        task_id=task.task_id,
    )

    assert result.error_code == DeterministicErrorCode.SIDE_EFFECT_RECONCILIATION_REQUIRED
    assert result.completed_actions == ()
    assert effects == 1


def test_unresolved_effect_blocks_alternative_plan_before_planner_runs(tmp_path) -> None:
    store = _store(tmp_path)
    task_id = _task_id(store)
    journal = RuntimeIdempotencyEffectJournal(IdempotencyLedger(store))
    journal.reserve(task_id=task_id, action=_action())
    planner_calls = 0

    class AlternativePlanner:
        def plan(self, *, state, goal, actions):
            nonlocal planner_calls
            del state, goal, actions
            planner_calls += 1
            return DeterministicPlan(steps=(PlanStep(action_id="alternative"),))

    brain = DeterministicBrain(
        planner=AlternativePlanner(),
        tools=ToolExecutor(),
        effect_journal=journal,
    )
    result = asyncio.run(
        brain.run(
            run_id="restart-alternative",
            task_id=task_id,
            state=WorldState(),
            goal=DeterministicGoal(required=frozenset({"done"})),
            actions=(DeterministicAction(action_id="alternative", adds=frozenset({"done"})),),
        )
    )

    assert result.error_code == DeterministicErrorCode.SIDE_EFFECT_RECONCILIATION_REQUIRED
    assert result.completed_actions == ()
    assert planner_calls == 0


def test_completed_effect_survives_loss_before_brain_applies_state(tmp_path) -> None:
    store = _store(tmp_path)
    task_id = _task_id(store)
    ledger = IdempotencyLedger(store)
    base_journal = RuntimeIdempotencyEffectJournal(ledger)
    effects = 0

    async def write(_arguments: dict[str, object]) -> object:
        nonlocal effects
        effects += 1
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

    crashing_journal = _CrashAfterCompleteJournal(base_journal)
    try:
        _run(
            _brain(tools=tools, journal=crashing_journal),
            _action(),
            task_id=task_id,
        )
    except _SimulatedProcessLoss:
        pass
    else:  # pragma: no cover - fixture must stop after durable completion
        raise AssertionError("simulated process loss did not escape")

    records = ledger.list_for_task(task_id)
    assert len(records) == 1
    assert records[0].status == IdempotencyStatus.COMPLETED
    assert effects == 1

    restarted = _run(
        _brain(tools=tools, journal=base_journal),
        _action(),
        task_id=task_id,
    )
    assert restarted.ok
    assert restarted.completed_actions == ("write-result",)
    assert restarted.final_state == WorldState(frozenset({"done"}))
    assert effects == 1


def test_approval_denial_releases_unused_effect_reservation(tmp_path) -> None:
    store = _store(tmp_path)
    task_id = _task_id(store)
    ledger = IdempotencyLedger(store)
    journal = RuntimeIdempotencyEffectJournal(ledger)
    action = _action()
    calls = 0

    async def write(_arguments: dict[str, object]) -> object:
        nonlocal calls
        calls += 1
        return "written"

    denied_tools = ToolExecutor()
    denied_tools.register(
        ToolSpec(
            tool_id="write.result",
            description="write",
            risk=ToolRisk.HIGH_IMPACT,
        ),
        write,
    )
    denied = _run(
        _brain(tools=denied_tools, journal=journal),
        action,
        task_id=task_id,
    )
    assert denied.error_code == DeterministicErrorCode.TOOL_EXECUTION_FAILED
    assert denied.error == "approval required"
    assert ledger.list_for_task(task_id) == ()
    assert calls == 0

    async def approve(_spec, _call) -> bool:
        return True

    approved_tools = ToolExecutor(approval_policy=approve)
    approved_tools.register(
        ToolSpec(
            tool_id="write.result",
            description="write",
            risk=ToolRisk.HIGH_IMPACT,
        ),
        write,
    )
    approved = _run(
        _brain(tools=approved_tools, journal=journal),
        action,
        task_id=task_id,
    )
    assert approved.ok
    assert calls == 1
    assert ledger.list_for_task(task_id)[0].status == IdempotencyStatus.COMPLETED


def test_pending_effect_takes_reconciliation_precedence_over_changed_arguments(tmp_path) -> None:
    store = _store(tmp_path)
    task_id = _task_id(store)
    journal = RuntimeIdempotencyEffectJournal(IdempotencyLedger(store))
    journal.reserve(
        task_id=task_id,
        action=_action(arguments={"target": "first.txt"}),
    )

    changed = _run(
        _brain(tools=ToolExecutor(), journal=journal),
        _action(arguments={"target": "other.txt"}),
        task_id=task_id,
    )

    assert changed.error_code == DeterministicErrorCode.SIDE_EFFECT_RECONCILIATION_REQUIRED


def test_completed_effect_identity_cannot_be_rebound_to_changed_arguments(tmp_path) -> None:
    store = _store(tmp_path)
    task_id = _task_id(store)
    ledger = IdempotencyLedger(store)
    journal = RuntimeIdempotencyEffectJournal(ledger)
    reservation = journal.reserve(
        task_id=task_id,
        action=_action(arguments={"target": "first.txt"}),
    )
    journal.complete(reservation.operation_key)
    calls = 0

    async def must_not_run(_arguments: dict[str, object]) -> object:
        nonlocal calls
        calls += 1
        return "unexpected"

    tools = ToolExecutor()
    tools.register(
        ToolSpec(
            tool_id="write.result",
            description="write",
            risk=ToolRisk.LOCAL_WRITE,
        ),
        must_not_run,
    )
    changed = _run(
        _brain(tools=tools, journal=journal),
        _action(arguments={"target": "other.txt"}),
        task_id=task_id,
    )

    assert changed.error_code == DeterministicErrorCode.SIDE_EFFECT_IDENTITY_CONFLICT
    assert calls == 0


def test_effect_journal_requires_stable_task_identity(tmp_path) -> None:
    store = _store(tmp_path)
    journal = RuntimeIdempotencyEffectJournal(IdempotencyLedger(store))
    tools = ToolExecutor()

    brain = _brain(tools=tools, journal=journal)
    try:
        asyncio.run(
            brain.run(
                run_id="attempt",
                state=WorldState(),
                goal=DeterministicGoal(),
                actions=(),
            )
        )
    except ValueError as exc:
        assert "task_id" in str(exc)
    else:  # pragma: no cover - durable identity is mandatory with the journal
        raise AssertionError("missing durable identity was accepted")
