from __future__ import annotations

from datetime import UTC, datetime

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
    ServiceObservation,
    ServiceReplica,
)
from nika_core.product_factory_operations_idempotency import (
    RuntimeIdempotencyMaintenanceJournal,
)
from nika_core.runtime.idempotency import IdempotencyLedger, IdempotencyStatus

SHA = "b" * 40
NOW = datetime(2026, 8, 25, 7, 45, tzinfo=UTC)


def _service() -> DeployableService:
    return DeployableService(
        service_id="service-qa",
        project_id="project-qa",
        environment_id="prod-qa",
        release_sha=SHA,
        wave=0,
        replicas=(ServiceReplica("replica-qa", "node-qa"),),
    )


def _observation() -> ServiceObservation:
    return ServiceObservation(
        service_id="service-qa",
        release_sha=SHA,
        healthy_replica_ids=("replica-qa",),
        failed_replica_ids=(),
        evidence_refs=("health:service-qa",),
        observed_at=NOW,
    )


def _request() -> MaintenanceRequest:
    return MaintenanceRequest(
        request_id="maintenance-qa",
        service_id="service-qa",
        action=MaintenanceAction.RESTART,
        reason="independent QA uncertain restart replay",
        evidence_refs=("health:service-qa",),
        approval_ref="approval:maintenance-qa",
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
            project_id == "project-qa"
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
            (f"qa-provider:{request.request_id}:uncertain",),
        )

    def inspect(self, request: MaintenanceRequest) -> MaintenanceResult:
        self.inspect_calls += 1
        return MaintenanceResult(
            True,
            False,
            (f"qa-provider:{request.request_id}:confirmed",),
        )


def test_uncertain_snapshot_restart_reconciles_without_redispatch(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "qa-pf8-uncertain-restart.db")
    store.initialize()
    task = TaskQueue(store).create(
        workspace_id="workspace-qa-pf8",
        agent_id="agent-qa-pf8",
        payload={"project_id": "project-qa"},
    )
    ledger = IdempotencyLedger(store)
    journal = RuntimeIdempotencyMaintenanceJournal(
        ledger,
        task_id=task.task_id,
    )
    port = UncertainThenResolvedPort()
    first = ProductOperationsCoordinator(
        "project-qa",
        port=port,
        approval_authority=ExactApprovalAuthority(),
        effect_journal=journal,
    )
    first.register(_service())
    first.record_observation(_observation())

    uncertain = first.request_maintenance(_request())
    assert uncertain.result.applied is False
    assert uncertain.result.uncertain is True
    assert first.snapshot().services[0].maintenance is MaintenanceState.PAUSED
    operation = ledger.list_for_task(task.task_id)[0]
    assert operation.status is IdempotencyStatus.UNCERTAIN
    assert port.apply_calls == 1
    assert port.inspect_calls == 0

    saved = first.snapshot()
    restarted_journal = RuntimeIdempotencyMaintenanceJournal(
        IdempotencyLedger(store),
        task_id=task.task_id,
    )
    restarted = ProductOperationsCoordinator(
        "project-qa",
        port=port,
        approval_authority=ExactApprovalAuthority(),
        effect_journal=restarted_journal,
    )
    restarted.restore(saved)

    restored_effect = restarted_journal.lookup(
        project_id="project-qa",
        service=_service(),
        request=_request(),
    )
    assert restored_effect is not None
    assert restored_effect.state is MaintenanceEffectState.UNCERTAIN
    assert restarted.snapshot().services[0].maintenance is MaintenanceState.PAUSED
    assert port.apply_calls == 1
    assert port.inspect_calls == 0

    reconciled = restarted.reconcile_maintenance("maintenance-qa")
    assert reconciled.reconciled is True
    assert reconciled.result.applied is True
    assert reconciled.result.uncertain is False
    assert restarted.snapshot().services[0].maintenance is MaintenanceState.RESTARTING
    assert ledger.require(operation.operation_key).status is IdempotencyStatus.COMPLETED
    assert port.apply_calls == 1
    assert port.inspect_calls == 1

    final = ProductOperationsCoordinator(
        "project-qa",
        port=port,
        approval_authority=ExactApprovalAuthority(),
        effect_journal=RuntimeIdempotencyMaintenanceJournal(
            IdempotencyLedger(store),
            task_id=task.task_id,
        ),
    )
    final.restore(restarted.snapshot())
    replay = final.request_maintenance(_request())
    assert replay == reconciled
    assert port.apply_calls == 1
    assert port.inspect_calls == 1
