from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

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
from nika_core.kernel.checkpoint import Checkpoint, CheckpointService
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState
from nika_core.runtime.contracts import (
    RuntimeCapability,
    RuntimeOutcome,
    RuntimeResult,
)
from nika_core.runtime.idempotency import IdempotencyLedger
from nika_core.runtime.recovery import RecoveryDisposition, RuntimeRecoveryService
from nika_core.runtime.registry import RuntimeRegistry
from nika_core.runtime.session_store import RuntimeSessionStore
from nika_core.security.standing_permission import (
    PermissionContext,
    StandingPermissionBinding,
    StandingPermissionPolicy,
    StandingPermissionScope,
    StandingPermissionStore,
)
from nika_core.tools import ToolEffectGuard, ToolExecutor, ToolRisk, ToolSpec


class _SimulatedProcessLoss(BaseException):
    pass


class _StableTwoStepPlanner:
    def plan(
        self,
        *,
        state: WorldState,
        goal: DeterministicGoal,
        actions: tuple[DeterministicAction, ...],
    ) -> DeterministicPlan:
        if goal.required <= state.facts and not goal.forbidden & state.facts:
            return DeterministicPlan(steps=())
        ordered = tuple(sorted(actions, key=lambda action: action.action_id))
        steps: list[PlanStep] = []
        facts = set(state.facts)
        for action in ordered:
            if action.requires <= facts and not action.forbids & facts:
                changes = bool((action.adds - facts) or (action.removes & facts))
                if changes:
                    steps.append(PlanStep(action_id=action.action_id, tool_id=action.tool_id))
                    facts.difference_update(action.removes)
                    facts.update(action.adds)
            if goal.required <= facts and not goal.forbidden & facts:
                break
        return DeterministicPlan(steps=tuple(steps))


class _CrashAfterFirstComplete:
    def __init__(self, delegate: RuntimeIdempotencyEffectJournal) -> None:
        self._delegate = delegate
        self._completed = 0

    def unresolved_operation_keys(self, *, task_id: str) -> tuple[str, ...]:
        return self._delegate.unresolved_operation_keys(task_id=task_id)

    def reserve(self, *, task_id: str, action: DeterministicAction):
        return self._delegate.reserve(task_id=task_id, action=action)

    def complete(self, operation_key: str) -> None:
        self._delegate.complete(operation_key)
        self._completed += 1
        if self._completed == 1:
            raise _SimulatedProcessLoss()

    def mark_uncertain(self, operation_key: str) -> None:
        self._delegate.mark_uncertain(operation_key)

    def release_pending(self, operation_key: str) -> None:
        self._delegate.release_pending(operation_key)


class _NeverResumedRuntime:
    runtime_id = "dev47-proof-runtime"
    capabilities = frozenset({RuntimeCapability.DURABLE_RESUME})

    def __init__(self) -> None:
        self.resume_calls = 0

    async def run(self, _request) -> RuntimeResult:
        return RuntimeResult(outcome=RuntimeOutcome.COMPLETED)

    async def resume(self, _request) -> RuntimeResult:
        self.resume_calls += 1
        return RuntimeResult(outcome=RuntimeOutcome.COMPLETED)

    async def cancel(self, *, task_id: str, thread_id: str) -> bool:
        del task_id, thread_id
        return True


def _store(tmp_path, name: str = "nika.db") -> SQLiteStore:
    store = SQLiteStore(tmp_path / name)
    store.initialize()
    return store


def _actions() -> tuple[DeterministicAction, DeterministicAction]:
    return (
        DeterministicAction(
            action_id="01-prepare",
            adds=frozenset({"prepared"}),
            tool_id="local.prepare",
            arguments={"phase": "prepare"},
        ),
        DeterministicAction(
            action_id="02-finish",
            requires=frozenset({"prepared"}),
            adds=frozenset({"done"}),
            tool_id="browser.finish",
            arguments={"phase": "finish"},
        ),
    )


def _plan_payload(
    *,
    plan: DeterministicPlan,
    state: WorldState,
    goal: DeterministicGoal,
    completed_actions: tuple[str, ...] = (),
) -> dict[str, object]:
    return {
        "schema": "nika.dev47.plan-restart.v1",
        "state": {"facts": sorted(state.facts)},
        "goal": {
            "required": sorted(goal.required),
            "forbidden": sorted(goal.forbidden),
        },
        "plan": {
            "steps": [
                {"action_id": step.action_id, "tool_id": step.tool_id}
                for step in plan.steps
            ]
        },
        "completed_actions": list(completed_actions),
    }


def _grant(
    permissions: StandingPermissionStore,
    *,
    permission_id: str,
    context: PermissionContext,
    start: datetime,
) -> StandingPermissionBinding:
    permissions.grant(
        permission_id=permission_id,
        scope=StandingPermissionScope(
            subject_id="agent-dev47",
            context=context,
            action_class="browser.finish",
            targets=("target:restart-proof",),
            sites=("example.test",),
            resources=("resource:restart-proof",),
            risk_ceiling=ToolRisk.EXTERNAL_SIDE_EFFECT,
            granted_at=start,
            expires_at=start + timedelta(hours=2),
        ),
    )
    return StandingPermissionBinding(
        permission_id=permission_id,
        subject_id="agent-dev47",
        context=context,
        target="target:restart-proof",
        resource_id="resource:restart-proof",
        network_host="example.test",
    )


def _tools(
    *,
    store: SQLiteStore,
    permissions: StandingPermissionStore,
    binding: StandingPermissionBinding,
    now: datetime,
    handler_calls: list[str],
) -> ToolExecutor:
    async def handler(arguments: dict[str, object]) -> object:
        handler_calls.append(str(arguments["phase"]))
        return {"phase": arguments["phase"]}

    executor = ToolExecutor(
        approval_policy=StandingPermissionPolicy(
            permissions,
            binding,
            clock=lambda: now,
        ),
        effect_guard=ToolEffectGuard(IdempotencyLedger(store)),
    )
    executor.register(
        ToolSpec(
            tool_id="local.prepare",
            description="DEV47 restart-local durable effect proof",
            risk=ToolRisk.LOCAL_WRITE,
        ),
        handler,
    )
    executor.register(
        ToolSpec(
            tool_id="browser.finish",
            description="DEV47 restart external reauthorization proof",
            risk=ToolRisk.EXTERNAL_SIDE_EFFECT,
        ),
        handler,
    )
    return executor


def _run(
    *,
    task_id: str,
    planner: _StableTwoStepPlanner,
    tools: ToolExecutor,
    journal,
    state: WorldState,
    goal: DeterministicGoal,
    actions: tuple[DeterministicAction, ...],
):
    return asyncio.run(
        DeterministicBrain(
            planner=planner,
            tools=tools,
            effect_journal=journal,
        ).run(
            run_id="dev47-plan-restart",
            task_id=task_id,
            state=state,
            goal=goal,
            actions=actions,
        )
    )


def test_plan_checkpoint_restart_reuses_completed_effect_and_rechecks_authority(tmp_path) -> None:
    store = _store(tmp_path)
    queue = TaskQueue(store)
    task = queue.create(workspace_id="issue-553", agent_id="dev47")
    queue.transition(task.task_id, TaskState.READY)
    queue.transition(task.task_id, TaskState.RUNNING)

    state = WorldState()
    goal = DeterministicGoal(required=frozenset({"done"}))
    actions = _actions()
    planner = _StableTwoStepPlanner()
    initial_plan = planner.plan(state=state, goal=goal, actions=actions)

    checkpoints = CheckpointService(store)
    persisted = checkpoints.save(
        task_id=task.task_id,
        stage="deterministic-plan",
        payload=_plan_payload(plan=initial_plan, state=state, goal=goal),
    )
    loaded = checkpoints.latest(task.task_id)
    assert loaded == persisted
    assert loaded is not None
    assert loaded.payload["plan"] == persisted.payload["plan"]
    assert loaded.checksum_sha256 == persisted.checksum_sha256

    start = datetime(2026, 9, 10, 16, 0, tzinfo=UTC)
    context = PermissionContext(
        user_id="user-dev47",
        project_id="project-dev47",
        task_id=task.task_id,
    )
    permissions = StandingPermissionStore(store, audit_log=AuditLog(store))
    permissions.initialize()
    binding_v1 = _grant(
        permissions,
        permission_id="perm-dev47-v1",
        context=context,
        start=start,
    )
    handler_calls: list[str] = []
    base_journal = RuntimeIdempotencyEffectJournal(IdempotencyLedger(store))

    try:
        _run(
            task_id=task.task_id,
            planner=planner,
            tools=_tools(
                store=store,
                permissions=permissions,
                binding=binding_v1,
                now=start + timedelta(minutes=1),
                handler_calls=handler_calls,
            ),
            journal=_CrashAfterFirstComplete(base_journal),
            state=state,
            goal=goal,
            actions=actions,
        )
    except _SimulatedProcessLoss:
        pass
    else:  # pragma: no cover - the fixture must stop after durable completion
        raise AssertionError("simulated process loss did not escape")

    assert handler_calls == ["prepare"]
    assert checkpoints.latest(task.task_id) == persisted

    permissions.revoke(
        "perm-dev47-v1",
        revoked_at=start + timedelta(minutes=2),
    )
    restarted_permissions = StandingPermissionStore(store, audit_log=AuditLog(store))
    restarted_permissions.initialize()

    state_payload = loaded.payload["state"]
    assert isinstance(state_payload, dict)
    restored_facts = state_payload["facts"]
    assert isinstance(restored_facts, list)
    denied = _run(
        task_id=task.task_id,
        planner=planner,
        tools=_tools(
            store=store,
            permissions=restarted_permissions,
            binding=binding_v1,
            now=start + timedelta(minutes=3),
            handler_calls=handler_calls,
        ),
        journal=base_journal,
        state=WorldState(frozenset(restored_facts)),
        goal=goal,
        actions=actions,
    )
    assert denied.error_code == DeterministicErrorCode.TOOL_EXECUTION_FAILED
    assert denied.error == "approval required"
    assert denied.completed_actions == ("01-prepare",)
    assert denied.final_state == WorldState(frozenset({"prepared"}))
    assert handler_calls == ["prepare"]

    binding_v2 = _grant(
        restarted_permissions,
        permission_id="perm-dev47-v2",
        context=context,
        start=start + timedelta(minutes=3),
    )
    resumed = _run(
        task_id=task.task_id,
        planner=planner,
        tools=_tools(
            store=store,
            permissions=restarted_permissions,
            binding=binding_v2,
            now=start + timedelta(minutes=4),
            handler_calls=handler_calls,
        ),
        journal=base_journal,
        state=state,
        goal=goal,
        actions=actions,
    )
    assert resumed.ok
    assert resumed.completed_actions == ("01-prepare", "02-finish")
    assert resumed.final_state == WorldState(frozenset({"prepared", "done"}))
    assert handler_calls == ["prepare", "finish"]

    recomputed_plan = planner.plan(
        state=WorldState(frozenset({"prepared"})),
        goal=goal,
        actions=actions,
    )
    assert recomputed_plan != initial_plan
    revised = checkpoints.save(
        task_id=task.task_id,
        stage="deterministic-plan",
        payload=_plan_payload(
            plan=recomputed_plan,
            state=WorldState(frozenset({"prepared"})),
            goal=goal,
            completed_actions=("01-prepare",),
        ),
    )
    assert revised.checkpoint_id != persisted.checkpoint_id
    assert revised.checksum_sha256 != persisted.checksum_sha256
    assert checkpoints.latest(task.task_id) == revised


def test_cancelled_plan_checkpoint_is_not_auto_resumed_after_restart(tmp_path) -> None:
    store = _store(tmp_path, "cancelled.db")
    queue = TaskQueue(store)
    task = queue.create(workspace_id="issue-553", agent_id="dev47")
    queue.transition(task.task_id, TaskState.READY)
    queue.transition(task.task_id, TaskState.RUNNING)

    planner = _StableTwoStepPlanner()
    state = WorldState()
    goal = DeterministicGoal(required=frozenset({"done"}))
    plan = planner.plan(state=state, goal=goal, actions=_actions())
    checkpoint: Checkpoint = CheckpointService(store).save(
        task_id=task.task_id,
        stage="deterministic-plan",
        payload=_plan_payload(plan=plan, state=state, goal=goal),
    )

    RuntimeSessionStore(store).record_active(
        task_id=task.task_id,
        runtime_id="dev47-proof-runtime",
        thread_id="thread-dev47",
        resume_token=checkpoint.checkpoint_id,
    )
    queue.transition(task.task_id, TaskState.CANCELLED)

    runtime = _NeverResumedRuntime()
    registry = RuntimeRegistry()
    registry.register(runtime)
    recovery = RuntimeRecoveryService(
        queue=queue,
        audit=AuditLog(store),
        runtimes=registry,
    )

    candidates = recovery.inspect()
    assert len(candidates) == 1
    assert candidates[0].task_id == task.task_id
    assert candidates[0].task_state == TaskState.CANCELLED
    assert candidates[0].disposition == RecoveryDisposition.INCONSISTENT_STATE

    executions = asyncio.run(recovery.resume_safe_crash_sessions())
    assert executions == ()
    assert runtime.resume_calls == 0
    assert queue.get(task.task_id).state == TaskState.CANCELLED
    assert CheckpointService(store).latest(task.task_id) == checkpoint
