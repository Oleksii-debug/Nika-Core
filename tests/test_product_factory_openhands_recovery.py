from __future__ import annotations

import asyncio
from pathlib import Path

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.product_factory_openhands_recovery import ProductFactoryOpenHandsRecoveryProbe
from nika_core.runtime.idempotency import IdempotencyLedger
from nika_core.toolsmith.contracts import (
    AllowedPathPolicy,
    CodingJob,
    IsolationClass,
    NetworkPolicy,
    ProcessPolicy,
    RecoveryState,
    RepositorySnapshot,
    ResourceBudget,
    WorkspaceLease,
)
from nika_core.toolsmith.openhands_remote_worker import OpenHandsRemoteCodingWorker


class NeverAcquireProvider:
    async def acquire(self, _job):
        raise AssertionError("restart reconciliation must not acquire a sandbox")

    async def release(self, _job, _endpoint, *, succeeded):
        raise AssertionError(f"unexpected release: {succeeded}")


class NeverExecuteRuntime:
    def __init__(self) -> None:
        self.execute_calls = 0

    async def execute(self, *_args):
        self.execute_calls += 1
        raise AssertionError("lost in-flight work must not be replayed")

    async def cancel(self, _job_id):
        return False


def _job(root: Path, work_id: str) -> CodingJob:
    return CodingJob(
        job_id=work_id,
        task_id="product:project-1:component:core",
        goal="continue bounded coding work",
        repository=RepositorySnapshot("repo-1", "a" * 40, "d" * 64),
        lease=WorkspaceLease(
            "lease-1", root, IsolationClass.PROCESS_CONTAINED, "2099-01-01T00:00:00Z"
        ),
        allowed_paths=AllowedPathPolicy(("src",)),
        process_policy=ProcessPolicy(("python",)),
        network_policy=NetworkPolicy(),
        resource_budget=ResourceBudget(30, 1024 * 1024, 8),
        permission_ceiling=frozenset({"read_source", "write_source", "run_tests"}),
    )


def test_fresh_worker_reconstructs_lost_inflight_identity_without_duplicate_execute(
    tmp_path: Path,
) -> None:
    database = tmp_path / "nika.db"
    store_a = SQLiteStore(database)
    store_a.initialize()
    task = TaskQueue(store_a).create(
        workspace_id="ws-1",
        agent_id="product-factory",
        payload={"kind": "product_factory", "product_project_id": "project-1"},
    )
    work_id = "work-restart-1"
    IdempotencyLedger(store_a).reserve(
        operation_key=f"pf-worker:{work_id}",
        task_id=task.task_id,
        operation_type="product_factory.coding_worker",
        input_fingerprint="f" * 64,
    )

    # Instance A had process-local running state before process loss.
    runtime_a = NeverExecuteRuntime()
    worker_a = OpenHandsRemoteCodingWorker(NeverAcquireProvider(), runtime_a)
    asyncio.run(worker_a._set_state(work_id, RecoveryState("running")))
    assert asyncio.run(worker_a.inspect(work_id)) == RecoveryState("running")

    # Process B has no RAM state. It reconstructs the already-dispatched identity from
    # Product Factory canonical SQLite idempotency authority and fails closed.
    store_b = SQLiteStore(database)
    store_b.initialize()
    runtime_b = NeverExecuteRuntime()
    worker_b = OpenHandsRemoteCodingWorker(
        NeverAcquireProvider(),
        runtime_b,
        recovery_probe=ProductFactoryOpenHandsRecoveryProbe(IdempotencyLedger(store_b)),
    )

    state = asyncio.run(worker_b.inspect(work_id))
    assert state == RecoveryState("manual_reconcile_required", "pf-ledger:pending")

    root = tmp_path / "worker"
    (root / "src").mkdir(parents=True)
    result = asyncio.run(worker_b.recover(_job(root, work_id), state))

    assert not result.succeeded
    assert result.failure is not None
    assert result.failure.retryable is False
    assert result.recovery_state == RecoveryState(
        "manual_reconcile_required", "pf-ledger:pending"
    )
    assert runtime_b.execute_calls == 0


def test_missing_durable_operation_remains_unknown_after_restart(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    worker = OpenHandsRemoteCodingWorker(
        NeverAcquireProvider(),
        NeverExecuteRuntime(),
        recovery_probe=ProductFactoryOpenHandsRecoveryProbe(IdempotencyLedger(store)),
    )

    assert asyncio.run(worker.inspect("never-dispatched")) is None
