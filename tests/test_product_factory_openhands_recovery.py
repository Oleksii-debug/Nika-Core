from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.product_factory_openhands_recovery import ProductFactoryOpenHandsRecoveryProbe
from nika_core.runtime.idempotency import IdempotencyConflictError, IdempotencyLedger
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
from nika_core.toolsmith.openhands_remote_worker import (
    OpenHandsRemoteCodingWorker,
    OpenHandsSandboxEndpoint,
)


class NeverAcquireProvider:
    async def acquire(self, _job):
        raise AssertionError("restart reconciliation must not acquire a sandbox")

    async def release(self, _job, _endpoint, *, succeeded):
        raise AssertionError(f"unexpected release: {succeeded}")


class NeverExecuteRuntime:
    def __init__(self) -> None:
        self.execute_calls = 0
        self.cancel_calls: list[str] = []
        self.bound_cancel_calls = []

    async def execute(self, *_args):
        self.execute_calls += 1
        raise AssertionError("lost in-flight work must not be replayed")

    async def cancel(self, job_id):
        self.cancel_calls.append(job_id)
        return True

    async def cancel_recovery(self, binding):
        self.bound_cancel_calls.append(binding)
        return True


class CleanupProvider:
    def __init__(self) -> None:
        self.acquired = []
        self.released = []

    async def acquire(self, job):
        self.acquired.append(job.job_id)
        raise AssertionError("restart cancellation must not acquire a sandbox")

    async def release(self, job, endpoint, *, succeeded):
        self.released.append((job.job_id, endpoint.endpoint_id, succeeded))


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
        acceptance_commands=(),
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


def test_restart_cancel_never_fabricates_stop_proof_from_fresh_runtime(
    tmp_path: Path,
) -> None:
    database = tmp_path / "nika.db"
    store = SQLiteStore(database)
    store.initialize()
    task = TaskQueue(store).create(
        workspace_id="ws-1",
        agent_id="product-factory",
        payload={"kind": "product_factory", "product_project_id": "project-1"},
    )
    work_id = "work-restart-cancel-1"
    IdempotencyLedger(store).reserve(
        operation_key=f"pf-worker:{work_id}",
        task_id=task.task_id,
        operation_type="product_factory.coding_worker",
        input_fingerprint="e" * 64,
    )

    runtime = NeverExecuteRuntime()
    worker = OpenHandsRemoteCodingWorker(
        NeverAcquireProvider(),
        runtime,
        recovery_probe=ProductFactoryOpenHandsRecoveryProbe(IdempotencyLedger(store)),
    )

    asyncio.run(worker.cancel(work_id))
    state = asyncio.run(worker.inspect(work_id))

    assert state == RecoveryState("manual_reconcile_required", "pf-ledger:pending")
    assert runtime.cancel_calls == []
    assert runtime.execute_calls == 0


def test_missing_durable_operation_remains_unknown_after_restart(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    worker = OpenHandsRemoteCodingWorker(
        NeverAcquireProvider(),
        NeverExecuteRuntime(),
        recovery_probe=ProductFactoryOpenHandsRecoveryProbe(IdempotencyLedger(store)),
    )

    assert asyncio.run(worker.inspect("never-dispatched")) is None


def _endpoint() -> OpenHandsSandboxEndpoint:
    return OpenHandsSandboxEndpoint(
        endpoint_id="sandbox-recovery-1",
        host="http://127.0.0.1:30000",
        working_dir="/workspace/nika-job",
        isolation_class=IsolationClass.REMOTE_SANDBOXED,
        sandbox_egress_hosts=("localhost",),
        network_policy_enforced=True,
    )


def test_probe_persists_secret_free_remote_binding_before_recovery(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    task = TaskQueue(store).create(
        workspace_id="ws-1",
        agent_id="product-factory",
        payload={"kind": "product_factory", "product_project_id": "project-1"},
    )
    work_id = "work-binding-1"
    ledger = IdempotencyLedger(store)
    ledger.reserve(
        operation_key=f"pf-worker:{work_id}",
        task_id=task.task_id,
        operation_type="product_factory.coding_worker",
        input_fingerprint="a" * 64,
    )
    probe = ProductFactoryOpenHandsRecoveryProbe(ledger)
    endpoint = _endpoint()
    conversation_id = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"nika-core:openhands:{endpoint.endpoint_id}:{work_id}",
        )
    )
    profile_id = "11111111-1111-4111-8111-111111111111"

    binding = probe.bind(
        _job(tmp_path / "worker", work_id),
        endpoint,
        conversation_id,
        profile_id,
    )

    assert probe.load(work_id) == binding
    assert asyncio.run(probe.inspect(work_id)) == RecoveryState(
        "remote_reconcile_required",
        binding.opaque_token,
    )
    record = ledger.get(f"pf-openhands-binding:{work_id}")
    assert record is not None
    assert record.result is not None
    serialized = json.dumps(dict(record.result), sort_keys=True).casefold()
    assert "session" not in serialized
    assert "api_key" not in serialized
    assert "secret" not in serialized


def test_probe_rejects_rebinding_remote_identity(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    task = TaskQueue(store).create(
        workspace_id="ws-1",
        agent_id="product-factory",
        payload={"kind": "product_factory", "product_project_id": "project-1"},
    )
    work_id = "work-binding-conflict"
    ledger = IdempotencyLedger(store)
    ledger.reserve(
        operation_key=f"pf-worker:{work_id}",
        task_id=task.task_id,
        operation_type="product_factory.coding_worker",
        input_fingerprint="b" * 64,
    )
    probe = ProductFactoryOpenHandsRecoveryProbe(ledger)
    first = _endpoint()
    first_conversation = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"nika-core:openhands:{first.endpoint_id}:{work_id}",
        )
    )
    probe.bind(
        _job(tmp_path / "worker", work_id),
        first,
        first_conversation,
        "11111111-1111-4111-8111-111111111111",
    )
    second = OpenHandsSandboxEndpoint(
        endpoint_id="sandbox-recovery-2",
        host=first.host,
        working_dir=first.working_dir,
        isolation_class=IsolationClass.REMOTE_SANDBOXED,
        sandbox_egress_hosts=first.sandbox_egress_hosts,
        network_policy_enforced=True,
    )
    second_conversation = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"nika-core:openhands:{second.endpoint_id}:{work_id}",
        )
    )

    with pytest.raises(IdempotencyConflictError):
        probe.bind(
            _job(tmp_path / "worker", work_id),
            second,
            second_conversation,
            "11111111-1111-4111-8111-111111111111",
        )


def test_restart_cancel_uses_bound_conversation_and_cleanup_without_replay(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    task = TaskQueue(store).create(
        workspace_id="ws-1",
        agent_id="product-factory",
        payload={"kind": "product_factory", "product_project_id": "project-1"},
    )
    work_id = "work-bound-cancel"
    ledger = IdempotencyLedger(store)
    ledger.reserve(
        operation_key=f"pf-worker:{work_id}",
        task_id=task.task_id,
        operation_type="product_factory.coding_worker",
        input_fingerprint="c" * 64,
    )
    probe = ProductFactoryOpenHandsRecoveryProbe(ledger)
    endpoint = _endpoint()
    conversation_id = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"nika-core:openhands:{endpoint.endpoint_id}:{work_id}",
        )
    )
    binding = probe.bind(
        _job(tmp_path / "worker", work_id),
        endpoint,
        conversation_id,
        "11111111-1111-4111-8111-111111111111",
    )
    runtime = NeverExecuteRuntime()
    provider = CleanupProvider()
    worker = OpenHandsRemoteCodingWorker(
        provider,
        runtime,
        recovery_probe=probe,
    )

    asyncio.run(worker.cancel(work_id))
    state = asyncio.run(worker.inspect(work_id))

    assert state == RecoveryState("cancelled", binding.opaque_token)
    assert runtime.cancel_calls == []
    assert runtime.bound_cancel_calls == [binding]
    result = asyncio.run(worker.recover(_job(tmp_path / "worker", work_id), state))
    assert not result.succeeded
    assert result.failure is not None
    assert result.failure.kind.value == "cancelled"
    assert result.failure.retryable is False
    assert runtime.execute_calls == 0
    assert provider.acquired == []
    assert provider.released == [(work_id, endpoint.endpoint_id, False)]


def test_restart_bound_cancel_without_stop_proof_remains_manual(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    task = TaskQueue(store).create(
        workspace_id="ws-1",
        agent_id="product-factory",
        payload={"kind": "product_factory", "product_project_id": "project-1"},
    )
    work_id = "work-bound-cancel-ambiguous"
    ledger = IdempotencyLedger(store)
    ledger.reserve(
        operation_key=f"pf-worker:{work_id}",
        task_id=task.task_id,
        operation_type="product_factory.coding_worker",
        input_fingerprint="d" * 64,
    )
    probe = ProductFactoryOpenHandsRecoveryProbe(ledger)
    endpoint = _endpoint()
    conversation_id = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"nika-core:openhands:{endpoint.endpoint_id}:{work_id}",
        )
    )
    binding = probe.bind(
        _job(tmp_path / "worker", work_id),
        endpoint,
        conversation_id,
        "11111111-1111-4111-8111-111111111111",
    )

    class AmbiguousRuntime(NeverExecuteRuntime):
        async def cancel_recovery(self, supplied):
            self.bound_cancel_calls.append(supplied)
            return False

    runtime = AmbiguousRuntime()
    worker = OpenHandsRemoteCodingWorker(
        CleanupProvider(),
        runtime,
        recovery_probe=probe,
    )

    asyncio.run(worker.cancel(work_id))
    state = asyncio.run(worker.inspect(work_id))

    assert state == RecoveryState(
        "manual_reconcile_required",
        binding.opaque_token,
    )
    assert runtime.cancel_calls == []
    assert runtime.bound_cancel_calls == [binding]


def test_fabricated_bound_cancelled_state_cannot_release_remote_sandbox(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    task = TaskQueue(store).create(
        workspace_id="ws-1",
        agent_id="product-factory",
        payload={"kind": "product_factory", "product_project_id": "project-1"},
    )
    work_id = "work-fabricated-cancel"
    ledger = IdempotencyLedger(store)
    ledger.reserve(
        operation_key=f"pf-worker:{work_id}",
        task_id=task.task_id,
        operation_type="product_factory.coding_worker",
        input_fingerprint="e" * 64,
    )
    probe = ProductFactoryOpenHandsRecoveryProbe(ledger)
    endpoint = _endpoint()
    conversation_id = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"nika-core:openhands:{endpoint.endpoint_id}:{work_id}",
        )
    )
    binding = probe.bind(
        _job(tmp_path / "worker", work_id),
        endpoint,
        conversation_id,
        "11111111-1111-4111-8111-111111111111",
    )
    provider = CleanupProvider()
    runtime = NeverExecuteRuntime()
    worker = OpenHandsRemoteCodingWorker(
        provider,
        runtime,
        recovery_probe=probe,
    )

    result = asyncio.run(
        worker.recover(
            _job(tmp_path / "worker", work_id),
            RecoveryState("cancelled", binding.opaque_token),
        )
    )

    assert not result.succeeded
    assert result.failure is not None
    assert result.failure.kind.value == "invalid_request"
    assert result.recovery_state == RecoveryState(
        "manual_reconcile_required",
        binding.opaque_token,
    )
    assert provider.released == []
    assert runtime.execute_calls == 0
    assert runtime.cancel_calls == []
    assert runtime.bound_cancel_calls == []
