from __future__ import annotations

from datetime import UTC, datetime

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.product_factory_operations import (
    MaintenanceRecord,
    ProductOperationsCoordinator,
    ProductOperationsSnapshot,
    ServiceRecord,
)
from nika_core.product_factory_operations_contracts import (
    DeployableService,
    MaintenanceAction,
    MaintenanceRequest,
    MaintenanceResult,
    MaintenanceState,
    ProductOperationsError,
    ServiceHealth,
    ServiceObservation,
    ServiceReplica,
)
from nika_core.product_factory_operations_idempotency import (
    RuntimeIdempotencyMaintenanceJournal,
)
from nika_core.runtime.idempotency import IdempotencyLedger, IdempotencyStatus

SHA = "a" * 40
NOW = datetime(2026, 8, 24, 7, 30, tzinfo=UTC)


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
        reason="approved exact-service maintenance",
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


def _snapshot(result: MaintenanceResult) -> ProductOperationsSnapshot:
    service = _service()
    request = _request()
    return ProductOperationsSnapshot(
        project_id="project-a",
        services=(
            ServiceRecord(
                service=service,
                health=ServiceHealth.HEALTHY,
                maintenance=MaintenanceState.RESTARTING,
                observation=_observation(),
            ),
        ),
        maintenance_records=(MaintenanceRecord(request, result),),
        revoked_credentials=(),
        unavailable_nodes=(),
    )


def _journal(tmp_path):
    store = SQLiteStore(tmp_path / "pf8-snapshot-journal.db")
    store.initialize()
    task = TaskQueue(store).create(
        workspace_id="workspace-pf8",
        agent_id="agent-pf8",
        payload={"project_id": "project-a"},
    )
    ledger = IdempotencyLedger(store)
    journal = RuntimeIdempotencyMaintenanceJournal(ledger, task_id=task.task_id)
    return task.task_id, ledger, journal


def test_restore_rejects_applied_snapshot_when_canonical_journal_has_no_effect(tmp_path) -> None:
    task_id, ledger, journal = _journal(tmp_path)
    forged = MaintenanceResult(True, False, ("snapshot:forged-applied",))
    coordinator = ProductOperationsCoordinator(
        "project-a",
        approval_authority=ExactApprovalAuthority(),
        effect_journal=journal,
    )

    with pytest.raises(ProductOperationsError, match="maintenance effect"):
        coordinator.restore(_snapshot(forged))

    assert ledger.list_for_task(task_id) == ()


def test_restore_rejects_snapshot_result_that_conflicts_with_completed_journal(tmp_path) -> None:
    _, ledger, journal = _journal(tmp_path)
    service = _service()
    request = _request()
    reservation = journal.reserve(
        project_id="project-a",
        service=service,
        request=request,
    )
    durable = MaintenanceResult(True, False, ("provider:durable",))
    journal.complete(reservation.operation_key, durable)
    forged = MaintenanceResult(True, False, ("snapshot:forged",))
    coordinator = ProductOperationsCoordinator(
        "project-a",
        approval_authority=ExactApprovalAuthority(),
        effect_journal=journal,
    )

    with pytest.raises(ProductOperationsError, match="maintenance effect"):
        coordinator.restore(_snapshot(forged))

    record = ledger.require(reservation.operation_key)
    assert record.status is IdempotencyStatus.COMPLETED


@pytest.mark.parametrize("durable_state", ["pending", "uncertain"])
def test_restore_rejects_resolved_snapshot_while_journal_is_unresolved(
    tmp_path,
    durable_state: str,
) -> None:
    _, ledger, journal = _journal(tmp_path)
    service = _service()
    request = _request()
    reservation = journal.reserve(
        project_id="project-a",
        service=service,
        request=request,
    )
    if durable_state == "uncertain":
        journal.mark_uncertain(reservation.operation_key)
    before = ledger.require(reservation.operation_key).status
    coordinator = ProductOperationsCoordinator(
        "project-a",
        approval_authority=ExactApprovalAuthority(),
        effect_journal=journal,
    )

    with pytest.raises(ProductOperationsError, match="maintenance effect"):
        coordinator.restore(
            _snapshot(MaintenanceResult(True, False, ("snapshot:resolved",)))
        )

    assert ledger.require(reservation.operation_key).status is before
