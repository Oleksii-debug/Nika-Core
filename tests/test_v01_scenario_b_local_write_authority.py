from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

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
from nika_core.scenario_b import ScenarioBService, ScenarioBTarget
from nika_core.tools import ToolCall, ToolExecutor, ToolRisk, ToolSpec


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


class _SemanticTools:
    def __init__(self) -> None:
        self.set_value_calls = 0
        self.invoke_calls = 0

    def readiness(self, target, *, task_id: str, tab_id: str, locator) -> PageReadinessResult:
        del target, task_id, tab_id, locator
        return PageReadinessResult(PageReadinessState.READY, "ready")

    async def set_value(self, _arguments: dict[str, object]) -> object:
        self.set_value_calls += 1
        raise AssertionError("cancelled task must not dispatch semantic input mutation")

    async def invoke(self, _arguments: dict[str, object]) -> object:
        self.invoke_calls += 1
        raise AssertionError("cancelled task must not reach external invoke")


class _CancelBeforeLocalDispatchExecutor(ToolExecutor):
    def __init__(self, task_queue: TaskQueue, task_id: str) -> None:
        super().__init__()
        self._task_queue = task_queue
        self._task_id = task_id
        self._cancelled = False

    async def execute(
        self,
        call: ToolCall,
        *,
        pre_handler_authority=None,
    ):
        if (
            call.tool_id == "v01.scenario_b.semantic_set_value"
            and not self._cancelled
        ):
            # Deterministically place cancellation after ScenarioBService's early
            # authority read but before ToolExecutor's last pre-handler boundary.
            self._task_queue.transition(self._task_id, TaskState.CANCELLED)
            self._cancelled = True
        return await super().execute(
            call,
            pre_handler_authority=pre_handler_authority,
        )


def test_local_write_pre_handler_authority_denial_never_dispatches_handler() -> None:
    calls = 0

    async def handler(_arguments: dict[str, object]) -> object:
        nonlocal calls
        calls += 1
        return {"unexpected": True}

    executor = ToolExecutor()
    executor.register(
        ToolSpec(
            tool_id="local.mutation",
            description="local mutation under trusted last-moment guard",
            risk=ToolRisk.LOCAL_WRITE,
        ),
        handler,
    )
    result = asyncio.run(
        executor.execute(
            ToolCall(
                call_id="local-1",
                tool_id="local.mutation",
                arguments={},
                task_id="task-1",
            ),
            pre_handler_authority=lambda: False,
        )
    )

    assert result.error == "pre-handler authority denied"
    assert calls == 0


def test_cancel_between_scenario_check_and_local_dispatch_keeps_cursor_retryable(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "local-write-cancel-race.db")
    store.initialize()
    task_queue = TaskQueue(store)
    task_id = task_queue.create(
        workspace_id="scenario-b-local-write-race",
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
    semantic_tools = _SemanticTools()
    service = ScenarioBService(
        task_id=task_id,
        task_queue=task_queue,
        cursor=cursor,
        tabs=_Tabs(),  # type: ignore[arg-type]
        executor=BoundedBatchExecutor(
            resources=resources,
            resource_scope="task",
            resource_owner_id="scenario-b",
            run_id="local-write-race",
            batch_size=1,
        ),
        tool_executor=_CancelBeforeLocalDispatchExecutor(task_queue, task_id),
        idempotency=ledger,
        memory=memory,
        targets=[
            ScenarioBTarget(
                target_id="target-0",
                url="https://example.test/0",
                input_locator=ControlLocator(role="textbox", name="Value"),
                input_value="safe-value",
                action_locator=ControlLocator(role="button", name="Run"),
                success_locator=ControlLocator(role="status", name="Done"),
            )
        ],
        semantic_tools=semantic_tools,  # type: ignore[arg-type]
        inter_batch_delay_seconds=0,
    )

    first = asyncio.run(service.run_ready_batch())
    assert first.execution is not None
    assert first.execution.failed_count == 1
    assert semantic_tools.set_value_calls == 0
    assert semantic_tools.invoke_calls == 0
    assert ledger.list_for_task(task_id) == ()
    assert service.cursor.state.targets[0].attempt_state is AttemptState.PENDING
    assert first.report[0].status is TargetReportStatus.CANCELLED
    assert first.report[0].attempted is False

    restarted = asyncio.run(service.run_ready_batch())
    assert restarted.execution is not None
    assert restarted.execution.stop_reason.value == "cancelled"
    assert restarted.execution.not_started_count == 1
    assert semantic_tools.set_value_calls == 0
    assert semantic_tools.invoke_calls == 0
    assert ledger.list_for_task(task_id) == ()
