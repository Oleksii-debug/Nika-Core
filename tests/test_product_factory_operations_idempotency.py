from __future__ import annotations

import json
from dataclasses import replace
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
    ProductOperationsError,
    ServiceObservation,
    ServiceReplica,
)
from nika_core.product_factory_operations_idempotency import (
    RuntimeIdempotencyMaintenanceJournal,
)
from nika_core.runtime.idempotency import IdempotencyLedger, IdempotencyStatus

SHA_A = "a" * 40
SHA_B = "b" * 40
NOW = datetime(2026, 8, 24, 7, 0, tzinfo=UTC)


def _service(release_sha: str = SHA_A) -> DeployableService:
    return DeployableService(
        service_id="service-a",
        project_id="project-a",
        environment_id="prod-eu",
        release_sha=release_sha,
        wave=0,
        replicas=(ServiceReplica("replica-a", "node-a"),),
    )


def _observation(service: DeployableService) -> ServiceObservation:
    return ServiceObservation(
        service_id=service.service_id,
        release_sha=service.release_sha,
        healthy_replica_ids=("replica-a",),
        failed_replica_ids=(),
        evidence_refs=(f"health:{service.release_sha}",),
        observed_at=NOW,
    )


def _request(
    service: DeployableService,
    *,
    request_id: str = "maintenance-1",
) -> MaintenanceRequest:
    return MaintenanceRequest(
        request_id=request_id,
        service_id=service.service_id,
        action=MaintenanceAction.RESTART,
        reason="restart after approved health evidence",
        evidence_refs=(f"health:{service.release_sha}",),
        approval_ref=f"approval:{request_id}",
    )


class ExactApprovalAuthority:
    def __init__(self, service: DeployableService, request: MaintenanceRequest) -> None:
        self._service = service
        self._request = request

    def verify(
        self,
        *,
        project_id: str,
        service: DeployableService,
        request: MaintenanceRequest,
    ) -> bool:
        return (
            project_id == "project-a"
            and service == self._service
            and request == self._request
        )


class InspectablePort:
    def __init__(self) -> None:
        self.apply_calls = 0
        self.inspect_calls = 0
        self.external_applied = False

    def apply(self, request: MaintenanceRequest) -> MaintenanceResult:
        self.apply_calls += 1
        self.external_applied = True
        return MaintenanceResult(
            True,
            False,
            (f"provider:{request.request_id}:applied",),
        )

    def inspect(self, request: MaintenanceRequest) -> MaintenanceResult:
        self.inspect_calls += 1
        return MaintenanceResult(
            self.external_applied,
            False,
            (f"provider:{request.request_id}:inspected",),
        )


class CrashAfterEffectPort(InspectablePort):
    def apply(self, request: MaintenanceRequest) -> MaintenanceResult:
        self.apply_calls += 1
        self.external_applied = True
        raise SystemExit("simulated process loss after provider effect")


def _runtime_journal(tmp_path):
    store = SQLiteStore(tmp_path / "nika-pf8.db")
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
    port: InspectablePort,
    journal: RuntimeIdempotencyMaintenanceJournal,
    service: DeployableService,
    request: MaintenanceRequest,
) -> ProductOperationsCoordinator:
    coordinator = ProductOperationsCoordinator(
        "project-a",
        port=port,
        approval_authority=ExactApprovalAuthority(service, request),
        effect_journal=journal,
    )
    coordinator.register(service)
    coordinator.record_observation(_observation(service))
    return coordinator


def test_runtime_ledger_reservation_and_result_survive_adapter_recreation(tmp_path) -> None:
    store, task_id, ledger, journal = _runtime_journal(tmp_path)
    service = _service()
    request = _request(service)

    first = journal.reserve(
        project_id="project-a",
        service=service,
        request=request,
    )
    assert first.created is True
    assert first.state is MaintenanceEffectState.PENDING
    assert first.result is None

    restarted = RuntimeIdempotencyMaintenanceJournal(
        IdempotencyLedger(store),
        task_id=task_id,
    )
    replay = restarted.reserve(
        project_id="project-a",
        service=service,
        request=request,
    )
    assert replay.created is False
    assert replay.operation_key == first.operation_key
    assert replay.state is MaintenanceEffectState.PENDING

    result = MaintenanceResult(True, False, ("provider:completed",))
    restarted.complete(first.operation_key, result)
    assert ledger.require(first.operation_key).status is IdempotencyStatus.COMPLETED

    second_restart = RuntimeIdempotencyMaintenanceJournal(
        IdempotencyLedger(store),
        task_id=task_id,
    )
    completed = second_restart.reserve(
        project_id="project-a",
        service=service,
        request=request,
    )
    assert completed.created is False
    assert completed.state is MaintenanceEffectState.COMPLETED
    assert completed.result == result


def test_runtime_ledger_rejects_rebound_request_and_fake_task_identity(tmp_path) -> None:
    store, _, _, journal = _runtime_journal(tmp_path)
    service = _service()
    request = _request(service)
    journal.reserve(project_id="project-a", service=service, request=request)

    rebound_service = _service(SHA_B)
    rebound_request = replace(
        request,
        evidence_refs=(f"health:{SHA_B}",),
    )
    with pytest.raises(ProductOperationsError, match="conflicts with durable runtime authority"):
        journal.reserve(
            project_id="project-a",
            service=rebound_service,
            request=rebound_request,
        )

    fake_task = RuntimeIdempotencyMaintenanceJournal(
        IdempotencyLedger(store),
        task_id="candidate-created-task",
    )
    with pytest.raises(ProductOperationsError, match="durable runtime authority"):
        fake_task.reserve(
            project_id="project-a",
            service=service,
            request=_request(service, request_id="maintenance-fake-task"),
        )


def test_process_loss_after_effect_restarts_with_inspection_not_redispatch(tmp_path) -> None:
    store, task_id, ledger, journal = _runtime_journal(tmp_path)
    service = _service()
    request = _request(service)
    port = CrashAfterEffectPort()
    first = _coordinator(
        port=port,
        journal=journal,
        service=service,
        request=request,
    )

    with pytest.raises(SystemExit, match="simulated process loss"):
        first.request_maintenance(request)

    operation = ledger.list_for_task(task_id)[0]
    assert operation.status is IdempotencyStatus.UNCERTAIN
    assert port.apply_calls == 1
    assert port.external_applied is True

    restarted_journal = RuntimeIdempotencyMaintenanceJournal(
        IdempotencyLedger(store),
        task_id=task_id,
    )
    restarted = _coordinator(
        port=port,
        journal=restarted_journal,
        service=service,
        request=request,
    )
    saved = restarted.request_maintenance(request)

    assert saved.reconciled is True
    assert saved.result.applied is True
    assert port.apply_calls == 1
    assert port.inspect_calls == 1
    assert ledger.list_for_task(task_id)[0].status is IdempotencyStatus.COMPLETED


def test_pending_after_hard_process_loss_is_inspected_without_provider_replay(tmp_path) -> None:
    store, task_id, ledger, journal = _runtime_journal(tmp_path)
    service = _service()
    request = _request(service)
    reservation = journal.reserve(
        project_id="project-a",
        service=service,
        request=request,
    )
    assert reservation.state is MaintenanceEffectState.PENDING

    port = InspectablePort()
    port.external_applied = True
    restarted = _coordinator(
        port=port,
        journal=RuntimeIdempotencyMaintenanceJournal(
            IdempotencyLedger(store),
            task_id=task_id,
        ),
        service=service,
        request=request,
    )
    saved = restarted.request_maintenance(request)

    assert saved.reconciled is True
    assert saved.result.applied is True
    assert port.apply_calls == 0
    assert port.inspect_calls == 1
    assert ledger.require(reservation.operation_key).status is IdempotencyStatus.COMPLETED


def test_completed_effect_before_local_save_restores_without_provider_call(tmp_path) -> None:
    store, task_id, _, journal = _runtime_journal(tmp_path)
    service = _service()
    request = _request(service)
    reservation = journal.reserve(
        project_id="project-a",
        service=service,
        request=request,
    )
    durable_result = MaintenanceResult(True, False, ("provider:durable-result",))
    journal.complete(reservation.operation_key, durable_result)

    port = InspectablePort()
    restarted = _coordinator(
        port=port,
        journal=RuntimeIdempotencyMaintenanceJournal(
            IdempotencyLedger(store),
            task_id=task_id,
        ),
        service=service,
        request=request,
    )
    saved = restarted.request_maintenance(request)

    assert saved.result == durable_result
    assert saved.reconciled is True
    assert port.apply_calls == 0
    assert port.inspect_calls == 0


def test_corrupt_durable_result_fails_closed_without_provider_dispatch(tmp_path) -> None:
    store, task_id, ledger, journal = _runtime_journal(tmp_path)
    service = _service()
    request = _request(service)
    reservation = journal.reserve(
        project_id="project-a",
        service=service,
        request=request,
    )
    journal.complete(
        reservation.operation_key,
        MaintenanceResult(True, False, ("provider:valid",)),
    )
    with store.connection() as conn:
        conn.execute(
            "UPDATE idempotency_records SET result_json = ? WHERE operation_key = ?",
            (
                json.dumps({"schema": "forged-result"}),
                reservation.operation_key,
            ),
        )

    port = InspectablePort()
    restarted = _coordinator(
        port=port,
        journal=RuntimeIdempotencyMaintenanceJournal(
            ledger,
            task_id=task_id,
        ),
        service=service,
        request=request,
    )
    with pytest.raises(ProductOperationsError, match="durable maintenance result"):
        restarted.request_maintenance(request)

    assert port.apply_calls == 0
    assert port.inspect_calls == 0
