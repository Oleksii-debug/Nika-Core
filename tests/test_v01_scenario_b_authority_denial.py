from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from nika_core.batch_cursor import AttemptState, BatchCursor, BatchTargetSpec
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
    task_id = TaskQueue(store).create(
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
