from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

import pytest

from nika_core import batch_execution
from nika_core.batch_execution import (
    BatchControlSnapshot,
    BatchStopReason,
    BatchTargetState,
    BoundedBatchExecutor,
)
from nika_core.data.sqlite import SQLiteStore
from nika_core.resources import ResourceBudget, ResourceManager, ResourceSnapshot


class FakeObserver:
    def snapshot(self) -> ResourceSnapshot:
        return ResourceSnapshot(
            cpu_percent=10.0,
            memory_percent=20.0,
            available_memory_bytes=2_000_000_000,
        )


@dataclass
class MutableControl:
    paused: bool = False
    cancelled: bool = False
    deadline_reached: bool = False

    def snapshot(self) -> BatchControlSnapshot:
        return BatchControlSnapshot(
            paused=self.paused,
            cancelled=self.cancelled,
            deadline_reached=self.deadline_reached,
        )


def _manager(tmp_path: Path, *, max_concurrent: int) -> ResourceManager:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    manager = ResourceManager(store, FakeObserver())
    manager.set_budget(
        ResourceBudget(
            scope="task",
            owner_id="worker24",
            max_concurrent=max_concurrent,
        )
    )
    return manager


def _executor(
    manager: ResourceManager,
    *,
    batch_size: int = 5,
    control: MutableControl | None = None,
    run_id: str = "controlled-run",
) -> BoundedBatchExecutor[int]:
    return BoundedBatchExecutor(
        resources=manager,
        resource_scope="task",
        resource_owner_id="worker24",
        run_id=run_id,
        batch_size=batch_size,
        control=control.snapshot if control else None,
    )


def test_twenty_targets_never_exceed_five_active_or_materialized(tmp_path: Path, monkeypatch) -> None:
    manager = _manager(tmp_path, max_concurrent=5)
    executor = _executor(manager)
    gather_widths: list[int] = []
    original_gather = batch_execution._gather

    def tracked_gather(*awaitables):
        gather_widths.append(len(awaitables))
        return original_gather(*awaitables)

    monkeypatch.setattr(batch_execution, "_gather", tracked_gather)

    async def scenario():
        releases = [asyncio.Event() for _ in range(4)]
        started: asyncio.Queue[int] = asyncio.Queue()
        active = 0
        peak_active = 0

        async def execute(target: int) -> None:
            nonlocal active, peak_active
            active += 1
            peak_active = max(peak_active, active)
            await started.put(target)
            try:
                await releases[target // 5].wait()
            finally:
                active -= 1

        task = asyncio.create_task(executor.run(tuple(range(20)), identify=str, execute=execute))
        for batch_index in range(4):
            observed = tuple([await started.get() for _ in range(5)])
            assert observed == tuple(range(batch_index * 5, batch_index * 5 + 5))
            assert active == 5
            assert manager.active_count(scope="task", owner_id="worker24") == 5
            assert started.empty()
            releases[batch_index].set()
        report = await task
        return report, peak_active

    report, peak_active = asyncio.run(scenario())

    assert peak_active == 5
    assert report.peak_in_flight == 5
    assert gather_widths == [5, 5, 5, 5]
    assert report.stop_reason is BatchStopReason.COMPLETED
    assert (report.total_count, report.completed_count, report.failed_count, report.not_started_count) == (
        20,
        20,
        0,
        0,
    )
    assert manager.active_count(scope="task", owner_id="worker24") == 0


def test_resource_manager_budget_remains_authoritative_when_batch_request_is_larger(
    tmp_path: Path, monkeypatch
) -> None:
    manager = _manager(tmp_path, max_concurrent=3)
    executor = _executor(manager, batch_size=5, run_id="budget-bound")
    gather_widths: list[int] = []
    original_gather = batch_execution._gather

    def tracked_gather(*awaitables):
        gather_widths.append(len(awaitables))
        return original_gather(*awaitables)

    monkeypatch.setattr(batch_execution, "_gather", tracked_gather)

    async def execute(_target: int) -> None:
        return None

    report = asyncio.run(executor.run(tuple(range(10)), identify=str, execute=execute))

    assert gather_widths == [3, 3, 3, 1]
    assert report.peak_in_flight == 3
    assert report.completed_count == 10


def test_one_target_failure_is_isolated_and_accounting_is_exact(tmp_path: Path) -> None:
    manager = _manager(tmp_path, max_concurrent=5)
    executor = _executor(manager, run_id="failure-isolation")
    attempted: list[int] = []

    async def execute(target: int) -> None:
        attempted.append(target)
        if target == 7:
            raise ValueError("synthetic detail must not enter the report")

    report = asyncio.run(
        executor.run(tuple(range(20)), identify=lambda target: f"target-{target:02d}", execute=execute)
    )

    assert len(attempted) == 20
    assert (report.completed_count, report.failed_count, report.not_started_count) == (19, 1, 0)
    assert report.results[7].state is BatchTargetState.FAILED
    assert report.results[7].reason == "ValueError"
    assert "synthetic detail" not in repr(report)
    assert tuple(result.target_id for result in report.results) == tuple(
        f"target-{target:02d}" for target in range(20)
    )


@pytest.mark.parametrize(
    ("control_field", "expected_reason"),
    [
        ("paused", BatchStopReason.PAUSED),
        ("cancelled", BatchStopReason.CANCELLED),
        ("deadline_reached", BatchStopReason.DEADLINE),
    ],
)
def test_control_signal_stops_launching_the_next_batch(
    tmp_path: Path,
    control_field: str,
    expected_reason: BatchStopReason,
) -> None:
    manager = _manager(tmp_path, max_concurrent=5)
    control = MutableControl()
    executor = _executor(manager, control=control, run_id=f"stop-{control_field}")

    async def scenario():
        release = asyncio.Event()
        started: asyncio.Queue[int] = asyncio.Queue()

        async def execute(target: int) -> None:
            await started.put(target)
            await release.wait()

        task = asyncio.create_task(executor.run(tuple(range(20)), identify=str, execute=execute))
        observed = tuple([await started.get() for _ in range(5)])
        assert observed == tuple(range(5))
        setattr(control, control_field, True)
        release.set()
        return await task

    report = asyncio.run(scenario())

    assert report.stop_reason is expected_reason
    assert (report.completed_count, report.failed_count, report.not_started_count) == (5, 0, 15)
    assert all(result.state is BatchTargetState.COMPLETED for result in report.results[:5])
    assert all(result.state is BatchTargetState.NOT_STARTED for result in report.results[5:])
    assert all(result.reason == expected_reason.value for result in report.results[5:])


def test_final_report_order_is_input_order_not_completion_order(tmp_path: Path) -> None:
    manager = _manager(tmp_path, max_concurrent=5)
    executor = _executor(manager, run_id="report-order")

    async def scenario():
        gates = [asyncio.Event() for _ in range(5)]
        started: asyncio.Queue[int] = asyncio.Queue()
        completed: asyncio.Queue[int] = asyncio.Queue()

        async def execute(target: int) -> None:
            await started.put(target)
            await gates[target].wait()
            await completed.put(target)

        task = asyncio.create_task(executor.run(tuple(range(5)), identify=str, execute=execute))
        assert tuple([await started.get() for _ in range(5)]) == tuple(range(5))
        completion_order: list[int] = []
        for target in reversed(range(5)):
            gates[target].set()
            completion_order.append(await completed.get())
        report = await task
        return report, completion_order

    report, completion_order = asyncio.run(scenario())

    assert completion_order == [4, 3, 2, 1, 0]
    assert tuple(result.target_id for result in report.results) == ("0", "1", "2", "3", "4")
    assert tuple(result.index for result in report.results) == (0, 1, 2, 3, 4)
