from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_factory_coordinator import ComponentWorkRequest
from nika_core.product_factory_program_host import (
    ProductFactoryProgramError,
    ProductFactoryProgramHost,
    _operation_key,
)
from nika_core.product_factory_work_ownership import ProductFactoryWorkOwnership
from nika_core.runtime.idempotency import IdempotencyStatus


class _UnusedWorker:
    async def dispatch(self, request):
        raise AssertionError(f"unexpected dispatch for {request.work_id}")

    async def inspect(self, work_id):
        raise AssertionError(f"unexpected inspect for {work_id}")

    async def recover(self, request, state):
        raise AssertionError(f"unexpected recovery for {request.work_id}")


class _Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 29, 3, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def _request() -> ComponentWorkRequest:
    return ComponentWorkRequest(
        work_id="work-1",
        project_id="project-1",
        component_id="component-1",
        repository_id="repo-1",
        goal="implement component",
        base_sha="a" * 40,
        allowed_paths=("src/component-1",),
        permission_ceiling=frozenset({"read_source", "write_source", "run_tests"}),
        acceptance_commands=(("python", "-m", "pytest", "tests/component-1"),),
    )


def _host(tmp_path, clock: _Clock, owner_id: str):
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    with store.connection() as connection:
        connection.execute(
            "INSERT OR IGNORE INTO tasks("
            "task_id, workspace_id, agent_id, state, payload_json, created_at, updated_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "host-task",
                "workspace-1",
                "agent-1",
                "running",
                "{}",
                clock.now.isoformat(),
                clock.now.isoformat(),
            ),
        )
    ownership = ProductFactoryWorkOwnership(store, clock=clock)
    return (
        ProductFactoryProgramHost(
            store,
            _UnusedWorker(),
            ownership=ownership,
            owner_id=owner_id,
            lease_seconds=10,
        ),
        ownership,
    )


def test_recovery_claim_blocks_direct_terminal_reconciliation(tmp_path) -> None:
    clock = _Clock()
    host, ownership = _host(tmp_path, clock, "program-host:first")
    request = _request()
    lease = ownership.acquire(
        project_id=request.project_id,
        work_id=request.work_id,
        owner_id=host.owner_id,
        lease_seconds=10,
    )
    operation, created = host._reserve_effect(
        host_task_id="host-task",
        request=request,
        lease=lease,
    )
    assert created is True
    host._ledger.mark_uncertain(operation.operation_key)

    claimed = host._claim_recovery_effect(
        host_task_id="host-task",
        request=request,
        lease=lease,
    )

    assert claimed is not None
    assert claimed.status is IdempotencyStatus.UNCERTAIN
    with pytest.raises(
        sqlite3.IntegrityError,
        match="active Product Factory recovery claim blocks completion",
    ):
        host._ledger.reconcile_completed(operation.operation_key, {"winner": "manual"})
    assert host._ledger.require(operation.operation_key).status is IdempotencyStatus.UNCERTAIN

    status, detail = host._mark_uncertain_and_release_recovery_claim(
        operation.operation_key,
        lease,
    )
    assert status is IdempotencyStatus.UNCERTAIN
    assert detail == ""
    reconciled = host._ledger.reconcile_completed(
        operation.operation_key,
        {"winner": "manual"},
    )
    assert reconciled.status is IdempotencyStatus.COMPLETED


def test_terminal_completion_before_claim_prevents_recovery_admission(tmp_path) -> None:
    clock = _Clock()
    host, ownership = _host(tmp_path, clock, "program-host:first")
    request = _request()
    lease = ownership.acquire(
        project_id=request.project_id,
        work_id=request.work_id,
        owner_id=host.owner_id,
        lease_seconds=10,
    )
    operation, _ = host._reserve_effect(
        host_task_id="host-task",
        request=request,
        lease=lease,
    )
    host._ledger.mark_uncertain(operation.operation_key)
    host._ledger.reconcile_completed(operation.operation_key, {"winner": "manual"})

    claimed = host._claim_recovery_effect(
        host_task_id="host-task",
        request=request,
        lease=lease,
    )

    assert claimed is None
    with host.store.connection() as connection:
        row = connection.execute(
            "SELECT 1 FROM product_factory_recovery_claims WHERE operation_key = ?",
            (operation.operation_key,),
        ).fetchone()
    assert row is None


def test_new_work_fence_takes_over_crash_stale_recovery_claim(tmp_path) -> None:
    clock = _Clock()
    first, first_ownership = _host(tmp_path, clock, "program-host:first")
    request = _request()
    first_lease = first_ownership.acquire(
        project_id=request.project_id,
        work_id=request.work_id,
        owner_id=first.owner_id,
        lease_seconds=10,
    )
    operation, _ = first._reserve_effect(
        host_task_id="host-task",
        request=request,
        lease=first_lease,
    )
    first._ledger.mark_uncertain(operation.operation_key)
    first._claim_recovery_effect(
        host_task_id="host-task",
        request=request,
        lease=first_lease,
    )

    clock.now += timedelta(seconds=11)
    second_ownership = ProductFactoryWorkOwnership(first.store, clock=clock)
    second = ProductFactoryProgramHost(
        first.store,
        _UnusedWorker(),
        ownership=second_ownership,
        owner_id="program-host:second",
        lease_seconds=10,
    )
    second_lease = second_ownership.acquire(
        project_id=request.project_id,
        work_id=request.work_id,
        owner_id=second.owner_id,
        lease_seconds=10,
    )
    assert second_lease.fence > first_lease.fence

    claimed = second._claim_recovery_effect(
        host_task_id="host-task",
        request=request,
        lease=second_lease,
    )

    assert claimed is not None
    with second.store.connection() as connection:
        row = connection.execute(
            "SELECT owner_id, fence FROM product_factory_recovery_claims "
            "WHERE operation_key = ?",
            (_operation_key(request),),
        ).fetchone()
    assert row is not None
    assert row["owner_id"] == second.owner_id
    assert row["fence"] == second_lease.fence


def test_current_recovery_claim_cannot_be_cleared_for_reconciliation(tmp_path) -> None:
    clock = _Clock()
    host, ownership = _host(tmp_path, clock, "program-host:first")
    request = _request()
    lease = ownership.acquire(
        project_id=request.project_id,
        work_id=request.work_id,
        owner_id=host.owner_id,
        lease_seconds=10,
    )
    operation, _ = host._reserve_effect(
        host_task_id="host-task",
        request=request,
        lease=lease,
    )
    host._ledger.mark_uncertain(operation.operation_key)
    host._claim_recovery_effect(
        host_task_id="host-task",
        request=request,
        lease=lease,
    )

    with host.store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        host._assert_lease(connection, lease)
        with pytest.raises(
            ProductFactoryProgramError,
            match="active recovery claim blocks terminal reconciliation",
        ):
            host._clear_stale_recovery_claim_for_reconciliation(
                connection,
                operation.operation_key,
                lease,
            )


def test_newer_work_fence_can_clear_crash_stale_claim_for_reconciliation(
    tmp_path,
) -> None:
    clock = _Clock()
    first, first_ownership = _host(tmp_path, clock, "program-host:first")
    request = _request()
    first_lease = first_ownership.acquire(
        project_id=request.project_id,
        work_id=request.work_id,
        owner_id=first.owner_id,
        lease_seconds=10,
    )
    operation, _ = first._reserve_effect(
        host_task_id="host-task",
        request=request,
        lease=first_lease,
    )
    first._ledger.mark_uncertain(operation.operation_key)
    first._claim_recovery_effect(
        host_task_id="host-task",
        request=request,
        lease=first_lease,
    )

    clock.now += timedelta(seconds=11)
    second_ownership = ProductFactoryWorkOwnership(first.store, clock=clock)
    second = ProductFactoryProgramHost(
        first.store,
        _UnusedWorker(),
        ownership=second_ownership,
        owner_id="program-host:second",
        lease_seconds=10,
    )
    second_lease = second_ownership.acquire(
        project_id=request.project_id,
        work_id=request.work_id,
        owner_id=second.owner_id,
        lease_seconds=10,
    )

    with second.store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        second._assert_lease(connection, second_lease)
        second._clear_stale_recovery_claim_for_reconciliation(
            connection,
            operation.operation_key,
            second_lease,
        )

    reconciled = second._ledger.reconcile_completed(
        operation.operation_key,
        {"winner": "manual-after-crash"},
    )
    assert reconciled.status is IdempotencyStatus.COMPLETED


def test_recovery_claim_blocks_pending_operation_release(tmp_path) -> None:
    clock = _Clock()
    host, ownership = _host(tmp_path, clock, "program-host:first")
    request = _request()
    lease = ownership.acquire(
        project_id=request.project_id,
        work_id=request.work_id,
        owner_id=host.owner_id,
        lease_seconds=10,
    )
    operation, _ = host._reserve_effect(
        host_task_id="host-task",
        request=request,
        lease=lease,
    )
    host._claim_recovery_effect(
        host_task_id="host-task",
        request=request,
        lease=lease,
    )

    with pytest.raises(
        sqlite3.IntegrityError,
        match="active Product Factory recovery claim blocks release",
    ):
        host._ledger.release_pending(operation.operation_key)

    current = host._ledger.require(operation.operation_key)
    assert current.status is IdempotencyStatus.PENDING
