from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from nika_core.batch_cursor import BatchCursor, BatchTargetSpec
from nika_core.batch_execution import BoundedBatchExecutor
from nika_core.batch_report import TargetReportStatus
from nika_core.data.sqlite import SQLiteStore
from nika_core.interaction import ControlLocator
from nika_core.interaction.domain import StaleSnapshotError
from nika_core.kernel.task_queue import TaskQueue
from nika_core.memory import MemoryService
from nika_core.page_readiness import PageReadinessResult, PageReadinessState
from nika_core.resources import ResourceBudget, ResourceManager, ResourceSnapshot
from nika_core.runtime.idempotency import IdempotencyLedger, IdempotencyStatus
from nika_core.scenario_b import ScenarioBService, ScenarioBTarget
from nika_core.task_browser_tabs import TaskBrowserTabs
from nika_core.tools import (
    ToolAuthorization,
    ToolEffectGuard,
    ToolExecutor,
    tool_arguments_fingerprint,
)


@dataclass
class _FakePage:
    page_id: str
    url: str | None = None
    closed: bool = False

    def goto(self, url: str, *, wait_until: str, timeout: float) -> None:
        assert wait_until == "domcontentloaded"
        assert timeout == 250
        self.url = url

    def bring_to_front(self) -> None:
        if self.closed:
            raise RuntimeError("closed")

    def close(self) -> None:
        self.closed = True

    def is_closed(self) -> bool:
        return self.closed


@dataclass
class _FakePageRecord:
    page: _FakePage


class _FakeRegistry:
    def __init__(self) -> None:
        self.pages: dict[str, _FakePageRecord] = {}

    def get(self, page_id: str) -> _FakePageRecord:
        record = self.pages.get(page_id)
        if record is None or record.page.closed:
            raise StaleSnapshotError("stale")
        return record


class _FakeSession:
    timeout_ms = 250

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.registry = _FakeRegistry()
        self._counter = 0

    def new_page(self) -> str:
        self._counter += 1
        page_id = f"{self.session_id}-page-{self._counter}"
        self.registry.pages[page_id] = _FakePageRecord(_FakePage(page_id))
        return page_id


class _Observer:
    def snapshot(self) -> ResourceSnapshot:
        return ResourceSnapshot(
            cpu_percent=5.0,
            memory_percent=10.0,
            available_memory_bytes=4_000_000_000,
        )


class _SuccessfulSemanticTools:
    def __init__(self) -> None:
        self.gate = asyncio.Event()
        self.started: asyncio.Queue[str] = asyncio.Queue()
        self.active = 0
        self.peak_active = 0
        self.invoke_calls = 0

    def reset_batch(self) -> None:
        self.gate = asyncio.Event()
        self.started = asyncio.Queue()

    def readiness(self, target, *, task_id: str, tab_id: str, locator) -> PageReadinessResult:
        del target, task_id, tab_id, locator
        return PageReadinessResult(PageReadinessState.READY, "ready")

    async def set_value(self, _arguments: dict[str, object]) -> object:
        raise AssertionError("input handler is not used by this acceptance target")

    async def invoke(self, arguments: dict[str, object]) -> object:
        target_id = arguments["target_id"]
        assert isinstance(target_id, str)
        self.invoke_calls += 1
        self.active += 1
        self.peak_active = max(self.peak_active, self.active)
        await self.started.put(target_id)
        try:
            await self.gate.wait()
        finally:
            self.active -= 1
        return {
            "target_id": target_id,
            "verified": True,
            "evidence_ref": f"result:scenario-b/{target_id}",
        }


async def _approve(spec, call):
    assert call.task_id is not None
    return ToolAuthorization(
        tool_id=spec.tool_id,
        task_id=call.task_id,
        risk=spec.risk,
        arguments_fingerprint=tool_arguments_fingerprint(call.arguments),
        effect_fingerprint=f"effect:{call.call_id}",
        approval_fingerprint=f"approval:{call.call_id}",
    )


def _executor(
    *,
    resources: ResourceManager,
    run_id: str,
) -> BoundedBatchExecutor[ScenarioBTarget]:
    return BoundedBatchExecutor(
        resources=resources,
        resource_scope="task",
        resource_owner_id="scenario-b",
        run_id=run_id,
        batch_size=5,
    )


def _tool_executor(
    ledger: IdempotencyLedger,
    semantic_tools: _SuccessfulSemanticTools,
) -> ToolExecutor:
    executor = ToolExecutor(
        approval_policy=_approve,
        effect_guard=ToolEffectGuard(ledger),
    )
    # ScenarioBService registers these bound handlers during construction.
    del semantic_tools
    return executor


def _targets() -> list[ScenarioBTarget]:
    action = ControlLocator(role="button", name="Run")
    success = ControlLocator(role="status", name="Done")
    return [
        ScenarioBTarget(
            target_id=f"target-{index:02d}",
            url=f"https://example.test/{index:02d}",
            action_locator=action,
            success_locator=success,
        )
        for index in range(20)
    ]


async def _run_one_five_target_batch(
    service: ScenarioBService,
    semantic_tools: _SuccessfulSemanticTools,
    expected_ids: set[str],
):
    semantic_tools.reset_batch()
    task = asyncio.create_task(service.run_ready_batch())
    started = {await semantic_tools.started.get() for _ in range(5)}
    assert started == expected_ids
    assert semantic_tools.active == 5
    semantic_tools.gate.set()
    result = await task
    assert result.execution is not None
    assert result.execution.completed_count == 5
    assert result.execution.failed_count == 0
    assert result.execution.peak_in_flight == 5
    return result


def test_twenty_target_success_path_is_max_five_restart_safe_and_exactly_once(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    task_id = TaskQueue(store).create(
        workspace_id="scenario-b-one-head",
        agent_id="scenario-b",
    ).task_id
    memory = MemoryService(store)
    ledger = IdempotencyLedger(store)
    specs = [
        BatchTargetSpec(target_id=f"target-{index:02d}", payload={"index": index})
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
    semantic_tools = _SuccessfulSemanticTools()
    fixed_now = datetime(2032, 1, 2, 3, 4, 5, tzinfo=UTC)
    targets = _targets()

    service = ScenarioBService(
        task_id=task_id,
        cursor=cursor,
        tabs=TaskBrowserTabs(session=_FakeSession("session-a")),  # type: ignore[arg-type]
        executor=_executor(resources=resources, run_id="scenario-b-a"),
        tool_executor=_tool_executor(ledger, semantic_tools),
        idempotency=ledger,
        memory=memory,
        targets=targets,
        semantic_tools=semantic_tools,  # type: ignore[arg-type]
        inter_batch_delay_seconds=0,
        clock=lambda: fixed_now,
    )

    first = asyncio.run(
        _run_one_five_target_batch(
            service,
            semantic_tools,
            {f"target-{index:02d}" for index in range(5)},
        )
    )
    assert first.waiting_until == fixed_now.isoformat()

    # Process restart: rebuild cursor, tab runtime bindings, batch executor and ToolExecutor
    # over the same durable MemoryService/IdempotencyLedger.
    restarted_cursor = BatchCursor.restore(
        memory,
        ledger,
        task_id=task_id,
        cursor_id="cursor",
        targets=specs,
        batch_size=5,
    )
    restarted = ScenarioBService(
        task_id=task_id,
        cursor=restarted_cursor,
        tabs=TaskBrowserTabs(session=_FakeSession("session-b")),  # type: ignore[arg-type]
        executor=_executor(resources=resources, run_id="scenario-b-b"),
        tool_executor=_tool_executor(ledger, semantic_tools),
        idempotency=ledger,
        memory=memory,
        targets=targets,
        semantic_tools=semantic_tools,  # type: ignore[arg-type]
        inter_batch_delay_seconds=0,
        clock=lambda: fixed_now,
    )
    restarted.release_inter_batch_wait(now=fixed_now)

    for batch_index in range(1, 4):
        result = asyncio.run(
            _run_one_five_target_batch(
                restarted,
                semantic_tools,
                {
                    f"target-{index:02d}"
                    for index in range(batch_index * 5, batch_index * 5 + 5)
                },
            )
        )
        if batch_index < 3:
            assert result.waiting_until == fixed_now.isoformat()
            restarted.release_inter_batch_wait(now=fixed_now)
        else:
            assert result.waiting_until is None

    assert semantic_tools.peak_active == 5
    assert semantic_tools.invoke_calls == 20
    assert restarted.cursor.state.confirmed_count == 20
    assert restarted.cursor.state.failed_count == 0
    assert restarted.cursor.state.uncertain_count == 0
    assert restarted.cursor.state.pending_count == 0
    assert len(restarted.tabs.owned_tabs(task_id)) == 20

    final = asyncio.run(restarted.run_ready_batch())
    assert final.execution is not None
    assert final.execution.total_count == 0
    assert len(final.report) == 20
    assert all(item.status is TargetReportStatus.SUCCEEDED for item in final.report)
    assert all(item.attempted is True for item in final.report)
    assert semantic_tools.invoke_calls == 20

    records = ledger.list_for_task(task_id)
    tool_records = [record for record in records if record.operation_type == "tool.external_effect"]
    batch_records = [
        record for record in records if record.operation_type == "v01.batch_target_effect"
    ]
    assert len(tool_records) == 20
    assert len(batch_records) == 20
    assert all(record.status is IdempotencyStatus.COMPLETED for record in records)

    final_restart = BatchCursor.restore(
        memory,
        ledger,
        task_id=task_id,
        cursor_id="cursor",
        targets=specs,
        batch_size=5,
    )
    final_service = ScenarioBService(
        task_id=task_id,
        cursor=final_restart,
        tabs=TaskBrowserTabs(session=_FakeSession("session-c")),  # type: ignore[arg-type]
        executor=_executor(resources=resources, run_id="scenario-b-c"),
        tool_executor=_tool_executor(ledger, semantic_tools),
        idempotency=ledger,
        memory=memory,
        targets=targets,
        semantic_tools=semantic_tools,  # type: ignore[arg-type]
        inter_batch_delay_seconds=0,
        clock=lambda: fixed_now,
    )
    replay = asyncio.run(final_service.run_ready_batch())
    assert replay.execution is not None
    assert replay.execution.total_count == 0
    assert semantic_tools.invoke_calls == 20
