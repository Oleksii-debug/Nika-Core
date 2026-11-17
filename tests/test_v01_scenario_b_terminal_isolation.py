from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

from nika_core.batch_cursor import BatchCursor, BatchTargetSpec
from nika_core.batch_execution import BoundedBatchExecutor
from nika_core.batch_report import TargetReportStatus
from nika_core.data.sqlite import SQLiteStore
from nika_core.interaction import ControlLocator
from nika_core.kernel.task_queue import TaskQueue
from nika_core.memory import MemoryService
from nika_core.page_readiness import PageReadinessResult, PageReadinessState
from nika_core.resources import ResourceBudget, ResourceManager, ResourceSnapshot
from nika_core.runtime.idempotency import IdempotencyLedger
from nika_core.scenario_b import ScenarioBService, ScenarioBTarget
from nika_core.tools import ToolExecutor


class _Observer:
    def snapshot(self) -> ResourceSnapshot:
        return ResourceSnapshot(
            cpu_percent=5.0,
            memory_percent=10.0,
            available_memory_bytes=4_000_000_000,
        )


class _Tabs:
    def __init__(self) -> None:
        self.session = object()
        self.opened: list[str] = []

    def owned_tabs(self, _task_id: str) -> tuple[object, ...]:
        return ()

    def open_tab(self, *, task_id: str, target_url: str, tab_id: str, reopen_policy) -> None:
        del task_id, target_url, reopen_policy
        self.opened.append(tab_id)

    def snapshot(self) -> dict[str, object]:
        return {"schema_version": 1, "tabs": []}


class _MissingSemanticTools:
    def readiness(self, target, *, task_id: str, tab_id: str, locator) -> PageReadinessResult:
        del target, task_id, tab_id, locator
        return PageReadinessResult(PageReadinessState.MISSING, "fixture missing")

    async def set_value(self, _arguments: dict[str, object]) -> object:
        raise AssertionError("local input tool must not run for missing action target")

    async def invoke(self, _arguments: dict[str, object]) -> object:
        raise AssertionError("external invoke must not run for missing action target")


def _build(tmp_path: Path):
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    task_queue = TaskQueue(store)
    task_id = task_queue.create(
        workspace_id="scenario-b-terminal-isolation",
        agent_id="scenario-b",
    ).task_id
    memory = MemoryService(store)
    ledger = IdempotencyLedger(store)
    specs = [
        BatchTargetSpec(target_id=f"target-{index}", payload={"index": index})
        for index in range(20)
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
    executor = BoundedBatchExecutor(
        resources=resources,
        resource_scope="task",
        resource_owner_id="scenario-b",
        run_id="scenario-b",
        batch_size=5,
    )
    locator = ControlLocator(role="button", name="Run")
    targets = [
        ScenarioBTarget(
            target_id=f"target-{index}",
            url=f"https://example.test/{index}",
            action_locator=locator,
            success_locator=ControlLocator(role="status", name="Done"),
        )
        for index in range(20)
    ]
    tabs = _Tabs()
    fixed_now = datetime(2032, 1, 2, 3, 4, 5, tzinfo=UTC)
    service = ScenarioBService(
        task_id=task_id,
        task_queue=task_queue,
        cursor=cursor,
        tabs=tabs,  # type: ignore[arg-type]
        executor=executor,
        tool_executor=ToolExecutor(),
        idempotency=ledger,
        memory=memory,
        targets=targets,
        semantic_tools=_MissingSemanticTools(),  # type: ignore[arg-type]
        inter_batch_delay_seconds=0,
        clock=lambda: fixed_now,
    )
    return service, memory, ledger, task_id, specs, tabs, fixed_now


def test_twenty_deterministic_pre_effect_failures_isolate_and_survive_restart(
    tmp_path: Path,
) -> None:
    service, memory, ledger, task_id, specs, tabs, fixed_now = _build(tmp_path)
    peaks: list[int] = []

    for batch_index in range(4):
        result = asyncio.run(service.run_ready_batch())
        assert result.execution is not None
        assert result.execution.failed_count == 5
        assert result.execution.completed_count == 0
        assert result.execution.not_started_count == 0
        peaks.append(result.execution.peak_in_flight)

        if batch_index < 3:
            assert result.waiting_until == fixed_now.isoformat()
            service.release_inter_batch_wait(now=fixed_now)
        else:
            assert result.waiting_until is None

    state = service.cursor.state
    assert peaks == [5, 5, 5, 5]
    assert state.failed_count == 20
    assert state.confirmed_count == 0
    assert state.uncertain_count == 0
    assert state.pending_count == 0
    assert len(tabs.opened) == 20
    assert ledger.list_for_task(task_id) == ()

    final_report = asyncio.run(service.run_ready_batch()).report
    assert len(final_report) == 20
    assert all(item.status is TargetReportStatus.FAILED for item in final_report)
    assert all(item.attempted is True for item in final_report)

    restarted = BatchCursor.restore(
        memory,
        ledger,
        task_id=task_id,
        cursor_id="cursor",
        targets=specs,
        batch_size=5,
    )
    assert restarted.state.failed_count == 20
    assert restarted.state.pending_count == 0
    assert restarted.state.next_scheduled_intent is None
