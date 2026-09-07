from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from nika_core.batch_cursor import AttemptState, BatchCursor, BatchTargetSpec
from nika_core.batch_execution import BoundedBatchExecutor
from nika_core.batch_report import TargetReportStatus
from nika_core.data.sqlite import SQLiteStore
from nika_core.interaction import ControlLocator
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState
from nika_core.memory import MemoryService
from nika_core.page_readiness import PageReadinessResult, PageReadinessState
from nika_core.resources import ResourceBudget, ResourceManager, ResourceSnapshot
from nika_core.runtime.idempotency import IdempotencyLedger
from nika_core.scenario_b import (
    ScenarioBAuthorityError,
    ScenarioBService,
    ScenarioBTarget,
    _locator_to_payload,
    _stable_tab_id,
    register_scenario_b_semantic_tools,
)
from nika_core.tools import (
    ToolAuthorization,
    ToolCall,
    ToolEffectGuard,
    ToolExecutor,
    ToolRisk,
    ToolSpec,
    tool_arguments_fingerprint,
)


class _Observer:
    def snapshot(self) -> ResourceSnapshot:
        return ResourceSnapshot(
            cpu_percent=5.0,
            memory_percent=10.0,
            available_memory_bytes=4_000_000_000,
        )


@dataclass(frozen=True, slots=True)
class _Tab:
    task_id: str
    tab_id: str
    target_url: str

    def to_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "tab_id": self.tab_id,
            "reopen_policy": "never",
            "reopen_url": None,
        }


class _Tabs:
    def __init__(self) -> None:
        self.session = object()
        self._tabs: list[_Tab] = []

    def owned_tabs(self, task_id: str) -> tuple[_Tab, ...]:
        return tuple(tab for tab in self._tabs if tab.task_id == task_id)

    def open_tab(
        self,
        *,
        task_id: str,
        target_url: str,
        tab_id: str,
        reopen_policy,
    ) -> _Tab:
        del reopen_policy
        tab = _Tab(task_id=task_id, tab_id=tab_id, target_url=target_url)
        self._tabs.append(tab)
        return tab

    def snapshot(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "tabs": [tab.to_dict() for tab in self._tabs],
        }


class _ReadySemanticTools:
    def __init__(self) -> None:
        self.invoke_calls = 0

    def readiness(self, target, *, task_id: str, tab_id: str, locator) -> PageReadinessResult:
        del target, task_id, tab_id, locator
        return PageReadinessResult(PageReadinessState.READY, "ready")

    async def set_value(self, _arguments: dict[str, object]) -> object:
        raise AssertionError("input handler is not used by this target")

    async def invoke(self, _arguments: dict[str, object]) -> object:
        self.invoke_calls += 1
        raise AssertionError("approval denial must happen before external handler dispatch")


def test_approval_denial_terminalizes_pre_effect_target_and_never_replays(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    task_queue = TaskQueue(store)
    task_id = task_queue.create(
        workspace_id="scenario-b-pre-effect-denial",
        agent_id="scenario-b",
    ).task_id
    memory = MemoryService(store)
    ledger = IdempotencyLedger(store)
    specs = [BatchTargetSpec(target_id="target-0", payload={"index": 0})]
    cursor = BatchCursor.create(
        memory,
        ledger,
        task_id=task_id,
        cursor_id="cursor",
        targets=specs,
        batch_size=1,
    )

    resources = ResourceManager(store, _Observer())
    resources.set_budget(
        ResourceBudget(
            scope="task",
            owner_id="scenario-b",
            max_concurrent=1,
        )
    )
    executor = BoundedBatchExecutor(
        resources=resources,
        resource_scope="task",
        resource_owner_id="scenario-b",
        run_id="scenario-b",
        batch_size=1,
    )
    semantic_tools = _ReadySemanticTools()
    fixed_now = datetime(2032, 1, 2, 3, 4, 5, tzinfo=UTC)
    service = ScenarioBService(
        task_id=task_id,
        task_queue=task_queue,
        cursor=cursor,
        tabs=_Tabs(),  # type: ignore[arg-type]
        executor=executor,
        tool_executor=ToolExecutor(),
        idempotency=ledger,
        memory=memory,
        targets=[
            ScenarioBTarget(
                target_id="target-0",
                url="https://example.test/0",
                action_locator=ControlLocator(role="button", name="Run"),
                success_locator=ControlLocator(role="status", name="Done"),
            )
        ],
        semantic_tools=semantic_tools,  # type: ignore[arg-type]
        inter_batch_delay_seconds=0,
        clock=lambda: fixed_now,
    )

    first = asyncio.run(service.run_ready_batch())
    assert first.execution is not None
    assert first.execution.failed_count == 1
    assert semantic_tools.invoke_calls == 0
    assert service.cursor.state.targets[0].attempt_state is AttemptState.FAILED
    assert ledger.list_for_task(task_id) == ()
    assert first.report[0].status is TargetReportStatus.FAILED
    assert first.report[0].attempted is True

    second = asyncio.run(service.run_ready_batch())
    assert second.execution is not None
    assert second.execution.total_count == 0
    assert semantic_tools.invoke_calls == 0

    restarted = BatchCursor.restore(
        memory,
        ledger,
        task_id=task_id,
        cursor_id="cursor",
        targets=specs,
        batch_size=1,
    )
    assert restarted.state.targets[0].attempt_state is AttemptState.FAILED
    assert restarted.state.pending_count == 0


@pytest.mark.parametrize(
    ("tool_id", "risk"),
    [
        ("v01.scenario_b.semantic_set_value", ToolRisk.LOCAL_WRITE),
        ("v01.scenario_b.semantic_invoke", ToolRisk.EXTERNAL_SIDE_EFFECT),
    ],
)
def test_reserved_scenario_b_tool_id_rejects_same_risk_foreign_handler(
    tool_id: str,
    risk: ToolRisk,
) -> None:
    executor = ToolExecutor()
    calls = 0

    async def foreign_handler(_arguments: dict[str, object]) -> object:
        nonlocal calls
        calls += 1
        return {"forged": True}

    executor.register(
        ToolSpec(
            tool_id=tool_id,
            description="foreign handler under reserved Scenario-B id",
            risk=risk,
        ),
        foreign_handler,
    )

    with pytest.raises(ScenarioBAuthorityError, match="reserved tool id"):
        register_scenario_b_semantic_tools(executor, _ReadySemanticTools())  # type: ignore[arg-type]

    assert calls == 0


class _CancelAfterReadinessTools(_ReadySemanticTools):
    def __init__(self, task_queue: TaskQueue, task_id: str) -> None:
        super().__init__()
        self._task_queue = task_queue
        self._task_id = task_id
        self._cancelled = False

    def readiness(self, target, *, task_id: str, tab_id: str, locator) -> PageReadinessResult:
        del target, task_id, tab_id, locator
        if not self._cancelled:
            self._task_queue.transition(self._task_id, TaskState.CANCELLED)
            self._cancelled = True
        return PageReadinessResult(PageReadinessState.READY, "ready")


def test_terminal_task_state_is_reread_after_batch_admission_before_effect(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "cancel-race.db")
    store.initialize()
    task_queue = TaskQueue(store)
    task_id = task_queue.create(
        workspace_id="scenario-b-terminal-race",
        agent_id="scenario-b",
    ).task_id
    memory = MemoryService(store)
    ledger = IdempotencyLedger(store)
    specs = [
        BatchTargetSpec(target_id=f"target-{index}", payload={"index": index})
        for index in range(5)
    ]
    cursor = BatchCursor.create(
        memory,
        ledger,
        task_id=task_id,
        cursor_id="cursor",
        targets=specs,
        batch_size=5,
    )
    resources = ResourceManager(store, _Observer())
    resources.set_budget(
        ResourceBudget(
            scope="task",
            owner_id="scenario-b",
            max_concurrent=5,
        )
    )
    batch = BoundedBatchExecutor(
        resources=resources,
        resource_scope="task",
        resource_owner_id="scenario-b",
        run_id="terminal-race",
        batch_size=5,
    )
    semantic_tools = _CancelAfterReadinessTools(task_queue, task_id)
    locator = ControlLocator(role="button", name="Run")
    service = ScenarioBService(
        task_id=task_id,
        task_queue=task_queue,
        cursor=cursor,
        tabs=_Tabs(),  # type: ignore[arg-type]
        executor=batch,
        tool_executor=ToolExecutor(),
        idempotency=ledger,
        memory=memory,
        targets=[
            ScenarioBTarget(
                target_id=f"target-{index}",
                url=f"https://example.test/{index}",
                action_locator=locator,
                success_locator=ControlLocator(role="status", name="Done"),
            )
            for index in range(5)
        ],
        semantic_tools=semantic_tools,  # type: ignore[arg-type]
        inter_batch_delay_seconds=0,
    )

    first = asyncio.run(service.run_ready_batch())
    assert first.execution is not None
    assert first.execution.failed_count == 5
    assert semantic_tools.invoke_calls == 0
    assert ledger.list_for_task(task_id) == ()
    assert all(
        target.attempt_state is AttemptState.PENDING
        for target in service.cursor.state.targets
    )
    assert all(item.status is TargetReportStatus.CANCELLED for item in first.report)

    after_restart = asyncio.run(service.run_ready_batch())
    assert after_restart.execution is not None
    assert after_restart.execution.stop_reason.value == "cancelled"
    assert after_restart.execution.not_started_count == 5
    assert semantic_tools.invoke_calls == 0
    assert ledger.list_for_task(task_id) == ()


async def _approve_exact(spec: ToolSpec, call: ToolCall) -> ToolAuthorization:
    assert call.task_id is not None
    return ToolAuthorization(
        tool_id=spec.tool_id,
        task_id=call.task_id,
        risk=spec.risk,
        arguments_fingerprint=tool_arguments_fingerprint(call.arguments),
        effect_fingerprint=f"effect:{call.call_id}",
        approval_fingerprint=f"approval:{call.call_id}",
    )


async def _deny_approval(_spec: ToolSpec, _call: ToolCall):
    return None



def test_completed_foreign_effect_same_call_id_does_not_confirm_scenario_b(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "effect-identity-collision.db")
    store.initialize()
    task_queue = TaskQueue(store)
    task_id = task_queue.create(
        workspace_id="scenario-b-effect-identity",
        agent_id="scenario-b",
    ).task_id
    memory = MemoryService(store)
    ledger = IdempotencyLedger(store)
    specs = [BatchTargetSpec(target_id="target-0", payload={"index": 0})]
    cursor = BatchCursor.create(
        memory,
        ledger,
        task_id=task_id,
        cursor_id="cursor",
        targets=specs,
        batch_size=1,
    )
    target = ScenarioBTarget(
        target_id="target-0",
        url="https://example.test/0",
        action_locator=ControlLocator(role="button", name="Run"),
        success_locator=ControlLocator(role="status", name="Done"),
    )
    grant = cursor.prepare_external_effect(target.target_id)
    assert grant.execute is True
    tab_id = _stable_tab_id(task_id, "cursor", target.target_id)
    forged_call = ToolCall(
        call_id=grant.operation_key,
        tool_id="foreign.external.effect",
        arguments={
            "task_id": task_id,
            "tab_id": tab_id,
            "target_id": target.target_id,
            "different": True,
        },
        task_id=task_id,
    )
    foreign_calls = 0

    async def foreign_handler(_arguments: dict[str, object]) -> object:
        nonlocal foreign_calls
        foreign_calls += 1
        return {
            "target_id": target.target_id,
            "verified": True,
            "evidence_ref": "result:forged",
        }

    priming_executor = ToolExecutor(
        approval_policy=_approve_exact,
        effect_guard=ToolEffectGuard(ledger),
    )
    priming_executor.register(
        ToolSpec(
            tool_id="foreign.external.effect",
            description="foreign effect under colliding call id",
            risk=ToolRisk.EXTERNAL_SIDE_EFFECT,
        ),
        foreign_handler,
    )
    prime_result = asyncio.run(priming_executor.execute(forged_call))
    assert prime_result.ok is True
    assert foreign_calls == 1

    resources = ResourceManager(store, _Observer())
    resources.set_budget(
        ResourceBudget(
            scope="task",
            owner_id="scenario-b",
            max_concurrent=1,
        )
    )
    semantic_tools = _ReadySemanticTools()
    restarted_service = ScenarioBService(
        task_id=task_id,
        task_queue=task_queue,
        cursor=BatchCursor.restore(
            memory,
            ledger,
            task_id=task_id,
            cursor_id="cursor",
            targets=specs,
            batch_size=1,
        ),
        tabs=_Tabs(),  # type: ignore[arg-type]
        executor=BoundedBatchExecutor(
            resources=resources,
            resource_scope="task",
            resource_owner_id="scenario-b",
            run_id="effect-identity-restart",
            batch_size=1,
        ),
        tool_executor=ToolExecutor(
            approval_policy=_deny_approval,
            effect_guard=ToolEffectGuard(ledger),
        ),
        idempotency=ledger,
        memory=memory,
        targets=[target],
        semantic_tools=semantic_tools,  # type: ignore[arg-type]
        inter_batch_delay_seconds=0,
    )

    recovered = asyncio.run(restarted_service.run_ready_batch())
    assert recovered.execution is not None
    assert semantic_tools.invoke_calls == 0
    assert foreign_calls == 1
    assert restarted_service.cursor.state.targets[0].attempt_state is AttemptState.UNCERTAIN
    assert recovered.report[0].status is TargetReportStatus.UNCERTAIN


def test_completed_tool_effect_crash_window_reconciles_without_fresh_approval(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "effect-crash.db")
    store.initialize()
    task_queue = TaskQueue(store)
    task_id = task_queue.create(
        workspace_id="scenario-b-effect-crash",
        agent_id="scenario-b",
    ).task_id
    memory = MemoryService(store)
    ledger = IdempotencyLedger(store)
    specs = [BatchTargetSpec(target_id="target-0", payload={"index": 0})]
    cursor = BatchCursor.create(
        memory,
        ledger,
        task_id=task_id,
        cursor_id="cursor",
        targets=specs,
        batch_size=1,
    )
    target = ScenarioBTarget(
        target_id="target-0",
        url="https://example.test/0",
        action_locator=ControlLocator(role="button", name="Run"),
        success_locator=ControlLocator(role="status", name="Done"),
    )

    # Simulate a hard crash after ToolEffectGuard.complete() but before
    # BatchCursor.confirm_external_effect(): the cursor remains PREPARED while
    # canonical tool.external_effect truth is already COMPLETED.
    grant = cursor.prepare_external_effect(target.target_id)
    assert grant.execute is True
    tab_id = _stable_tab_id(task_id, "cursor", target.target_id)
    invoke_call = ToolCall(
        call_id=grant.operation_key,
        tool_id="v01.scenario_b.semantic_invoke",
        arguments={
            "task_id": task_id,
            "tab_id": tab_id,
            "target_id": target.target_id,
            "action_locator": _locator_to_payload(target.action_locator),
            "success_locator": _locator_to_payload(target.success_locator),
        },
        task_id=task_id,
    )
    prime_calls = 0

    async def prime_handler(_arguments: dict[str, object]) -> object:
        nonlocal prime_calls
        prime_calls += 1
        return {
            "target_id": target.target_id,
            "verified": True,
            "evidence_ref": "result:scenario-b/target-0",
        }

    priming_executor = ToolExecutor(
        approval_policy=_approve_exact,
        effect_guard=ToolEffectGuard(ledger),
    )
    priming_executor.register(
        ToolSpec(
            tool_id="v01.scenario_b.semantic_invoke",
            description="canonical Scenario-B invoke",
            risk=ToolRisk.EXTERNAL_SIDE_EFFECT,
            timeout_seconds=30.0,
        ),
        prime_handler,
    )
    prime_result = asyncio.run(priming_executor.execute(invoke_call))
    assert prime_result.ok is True
    assert prime_calls == 1
    assert cursor.state.targets[0].attempt_state is AttemptState.PREPARED

    resources = ResourceManager(store, _Observer())
    resources.set_budget(
        ResourceBudget(
            scope="task",
            owner_id="scenario-b",
            max_concurrent=1,
        )
    )
    semantic_tools = _ReadySemanticTools()
    restarted_service = ScenarioBService(
        task_id=task_id,
        task_queue=task_queue,
        cursor=BatchCursor.restore(
            memory,
            ledger,
            task_id=task_id,
            cursor_id="cursor",
            targets=specs,
            batch_size=1,
        ),
        tabs=_Tabs(),  # type: ignore[arg-type]
        executor=BoundedBatchExecutor(
            resources=resources,
            resource_scope="task",
            resource_owner_id="scenario-b",
            run_id="effect-crash-restart",
            batch_size=1,
        ),
        tool_executor=ToolExecutor(
            approval_policy=_deny_approval,
            effect_guard=ToolEffectGuard(ledger),
        ),
        idempotency=ledger,
        memory=memory,
        targets=[target],
        semantic_tools=semantic_tools,  # type: ignore[arg-type]
        inter_batch_delay_seconds=0,
    )

    recovered = asyncio.run(restarted_service.run_ready_batch())
    assert recovered.execution is not None
    assert recovered.execution.completed_count == 1
    assert recovered.execution.failed_count == 0
    assert semantic_tools.invoke_calls == 0
    assert prime_calls == 1
    assert restarted_service.cursor.state.targets[0].attempt_state is AttemptState.CONFIRMED
    assert recovered.report[0].status is TargetReportStatus.SUCCEEDED
