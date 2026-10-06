from __future__ import annotations

from datetime import UTC, datetime

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.product_factory_operations import ProductOperationsCoordinator
from nika_core.product_factory_operations_contracts import (
    DeployableService,
    MaintenanceAction,
    MaintenanceEffectState,
    MaintenanceRequest,
    MaintenanceResult,
    MaintenanceState,
    ProductOperationsError,
    ServiceObservation,
    ServiceReplica,
)
from nika_core.product_factory_operations_idempotency import (
    RuntimeIdempotencyMaintenanceJournal,
)
from nika_core.runtime.idempotency import IdempotencyLedger, IdempotencyStatus

SHA = "a" * 40
NOW = datetime(2026, 8, 24, 22, 30, tzinfo=UTC)


def _service() -> DeployableService:
    return DeployableService(
        service_id="service-a",
        project_id="project-a",
        environment_id="prod-eu",
        release_sha=SHA,
        wave=0,
        replicas=(ServiceReplica("replica-a", "node-a"),),
    )


def _observation() -> ServiceObservation:
    return ServiceObservation(
        service_id="service-a",
        release_sha=SHA,
        healthy_replica_ids=("replica-a",),
        failed_replica_ids=(),
        evidence_refs=("health:service-a",),
        observed_at=NOW,
    )


def _request() -> MaintenanceRequest:
    return MaintenanceRequest(
        request_id="maintenance-a",
        service_id="service-a",
        action=MaintenanceAction.RESTART,
        reason="approved restart with uncertain provider outcome",
        evidence_refs=("health:service-a",),
        approval_ref="approval:maintenance-a",
    )


class ExactApprovalAuthority:
    def verify(
        self,
        *,
        project_id: str,
        service: DeployableService,
        request: MaintenanceRequest,
    ) -> bool:
        return (
            project_id == "project-a"
            and service == _service()
            and request == _request()
        )


class UncertainThenResolvedPort:
    def __init__(self) -> None:
        self.apply_calls = 0
        self.inspect_calls = 0

    def apply(self, request: MaintenanceRequest) -> MaintenanceResult:
        self.apply_calls += 1
        return MaintenanceResult(
            False,
            True,
            (f"provider:{request.request_id}:uncertain",),
        )

    def inspect(self, request: MaintenanceRequest) -> MaintenanceResult:
        self.inspect_calls += 1
        return MaintenanceResult(
            True,
            False,
            (f"provider:{request.request_id}:confirmed",),
        )


def _runtime_journal(tmp_path):
    store = SQLiteStore(tmp_path / "pf8-uncertain-restart.db")
    store.initialize()
    task = TaskQueue(store).create(
        workspace_id="workspace-pf8",
        agent_id="agent-pf8",
        payload={"project_id": "project-a"},
    )
    ledger = IdempotencyLedger(store)
    journal = RuntimeIdempotencyMaintenanceJournal(ledger, task_id=task.task_id)
    return store, task.task_id, ledger, journal


def _coordinator(
    *,
    port: UncertainThenResolvedPort,
    journal: RuntimeIdempotencyMaintenanceJournal,
) -> ProductOperationsCoordinator:
    coordinator = ProductOperationsCoordinator(
        "project-a",
        port=port,
        approval_authority=ExactApprovalAuthority(),
        effect_journal=journal,
    )
    coordinator.register(_service())
    coordinator.record_observation(_observation())
    return coordinator


def test_uncertain_snapshot_survives_restart_and_reconciles_without_redispatch(
    tmp_path,
) -> None:
    store, task_id, ledger, journal = _runtime_journal(tmp_path)
    port = UncertainThenResolvedPort()
    first = _coordinator(port=port, journal=journal)

    uncertain = first.request_maintenance(_request())
    assert uncertain.result.uncertain is True
    assert uncertain.result.applied is False
    assert first.snapshot().services[0].maintenance is MaintenanceState.PAUSED
    operation = ledger.list_for_task(task_id)[0]
    assert operation.status is IdempotencyStatus.UNCERTAIN
    assert port.apply_calls == 1
    assert port.inspect_calls == 0

    saved_snapshot = first.snapshot()
    restarted_journal = RuntimeIdempotencyMaintenanceJournal(
        IdempotencyLedger(store),
        task_id=task_id,
    )
    restarted = ProductOperationsCoordinator(
        "project-a",
        port=port,
        approval_authority=ExactApprovalAuthority(),
        effect_journal=restarted_journal,
    )
    restarted.restore(saved_snapshot)

    restored_effect = restarted_journal.lookup(
        project_id="project-a",
        service=_service(),
        request=_request(),
    )
    assert restored_effect is not None
    assert restored_effect.state is MaintenanceEffectState.UNCERTAIN
    assert port.apply_calls == 1
    assert port.inspect_calls == 0

    reconciled = restarted.reconcile_maintenance("maintenance-a")
    assert reconciled.reconciled is True
    assert reconciled.result.applied is True
    assert reconciled.result.uncertain is False
    assert restarted.snapshot().services[0].maintenance is MaintenanceState.RESTARTING
    assert port.apply_calls == 1
    assert port.inspect_calls == 1
    assert ledger.require(operation.operation_key).status is IdempotencyStatus.COMPLETED

    final = ProductOperationsCoordinator(
        "project-a",
        port=port,
        approval_authority=ExactApprovalAuthority(),
        effect_journal=RuntimeIdempotencyMaintenanceJournal(
            IdempotencyLedger(store),
            task_id=task_id,
        ),
    )
    final.restore(restarted.snapshot())
    replay = final.request_maintenance(_request())
    assert replay == reconciled
    assert port.apply_calls == 1
    assert port.inspect_calls == 1


def test_uncertain_snapshot_rejects_pending_journal_without_provider_access(tmp_path) -> None:
    store, task_id, ledger, journal = _runtime_journal(tmp_path)
    port = UncertainThenResolvedPort()
    first = _coordinator(port=port, journal=journal)
    uncertain = first.request_maintenance(_request())
    snapshot = first.snapshot()
    operation = ledger.list_for_task(task_id)[0]

    with store.connection() as conn:
        conn.execute(
            "UPDATE idempotency_records SET status = ?, result_json = NULL WHERE operation_key = ?",
            (IdempotencyStatus.PENDING.value, operation.operation_key),
        )

    restarted = ProductOperationsCoordinator(
        "project-a",
        port=port,
        approval_authority=ExactApprovalAuthority(),
        effect_journal=journal,
    )
    with pytest.raises(ProductOperationsError, match="maintenance effect"):
        restarted.restore(snapshot)

    assert uncertain.result.uncertain is True
    assert ledger.require(operation.operation_key).status is IdempotencyStatus.PENDING
    assert port.apply_calls == 1
    assert port.inspect_calls == 0
