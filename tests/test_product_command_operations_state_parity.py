from __future__ import annotations

from datetime import UTC, datetime

import pytest

from nika_core.product_command.command_center import (
    ProductCommandCenterScopeError,
    _validate_operations_scope,
)
from nika_core.product_factory_operations import (
    MaintenanceRecord,
    ProductOperationsSnapshot,
    ServiceRecord,
)
from nika_core.product_factory_operations_contracts import (
    DeployableService,
    MaintenanceAction,
    MaintenanceRequest,
    MaintenanceResult,
    RollbackObservation,
    ServiceHealth,
    ServiceObservation,
    ServiceReplica,
)

NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
SHA_A = "a" * 40
SHA_B = "b" * 40


def _service(
    service_id: str = "service-api",
    *,
    wave: int = 0,
    dependencies: tuple[str, ...] = (),
    credential_refs: tuple[str, ...] = (),
    nodes: tuple[str, ...] = ("node-a",),
) -> DeployableService:
    return DeployableService(
        service_id,
        "project-1",
        "stage",
        SHA_A,
        wave,
        tuple(
            ServiceReplica(f"{service_id}-replica-{index}", node)
            for index, node in enumerate(nodes)
        ),
        min_healthy_replicas=1,
        dependencies=dependencies,
        credential_refs=credential_refs,
    )


def _observation(
    service: DeployableService,
    *,
    healthy: tuple[int, ...] = (),
    failed: tuple[int, ...] = (),
) -> ServiceObservation:
    return ServiceObservation(
        service.service_id,
        service.release_sha,
        tuple(service.replicas[index].replica_id for index in healthy),
        tuple(service.replicas[index].replica_id for index in failed),
        (f"health://{service.service_id}",),
        NOW,
    )


def _rollback(service: DeployableService, *, succeeded: bool = True) -> RollbackObservation:
    return RollbackObservation(
        service.service_id,
        service.release_sha,
        SHA_B,
        succeeded,
        (f"rollback://{service.service_id}",),
        NOW,
    )


def test_operations_snapshot_requires_every_revoked_service_credential_blocker() -> None:
    secret_ref = "credential://provider/project-1/writer"
    service = _service(credential_refs=(secret_ref,))
    snapshot = ProductOperationsSnapshot(
        "project-1",
        (ServiceRecord(service, health=ServiceHealth.PENDING),),
        (),
        (secret_ref,),
        (),
    )

    with pytest.raises(
        ProductCommandCenterScopeError,
        match="omits revoked service credential blocker",
    ):
        _validate_operations_scope("project-1", snapshot)


def test_operations_snapshot_requires_dependency_from_earlier_wave() -> None:
    database = _service("service-db", wave=1)
    api = _service("service-api", wave=0, dependencies=("service-db",))
    snapshot = ProductOperationsSnapshot(
        "project-1",
        (ServiceRecord(database), ServiceRecord(api)),
        (),
        (),
        (),
    )

    with pytest.raises(ProductCommandCenterScopeError, match="earlier wave"):
        _validate_operations_scope("project-1", snapshot)


def test_operations_snapshot_rejects_unapproved_persisted_maintenance() -> None:
    service = _service()
    request = MaintenanceRequest(
        "maint-1",
        service.service_id,
        MaintenanceAction.RESTART,
        "Patch runtime",
        ("plan://maint-1",),
    )
    result = MaintenanceResult(True, False, ("provider://maint-1/applied",))
    snapshot = ProductOperationsSnapshot(
        "project-1",
        (ServiceRecord(service),),
        (MaintenanceRecord(request, result),),
        (),
        (),
    )

    with pytest.raises(ProductCommandCenterScopeError, match="explicit approval"):
        _validate_operations_scope("project-1", snapshot)


def test_operations_snapshot_rejects_forged_health_against_observation() -> None:
    service = _service()
    observation = _observation(service, healthy=(0,))
    snapshot = ProductOperationsSnapshot(
        "project-1",
        (
            ServiceRecord(
                service,
                health=ServiceHealth.DEGRADED,
                observation=observation,
            ),
        ),
        (),
        (),
        (),
    )

    with pytest.raises(ProductCommandCenterScopeError, match="service health disagrees"):
        _validate_operations_scope("project-1", snapshot)


def test_operations_snapshot_recomputes_health_with_node_loss() -> None:
    service = _service(nodes=("node-a", "node-b"))
    observation = _observation(service, healthy=(0, 1))
    snapshot = ProductOperationsSnapshot(
        "project-1",
        (
            ServiceRecord(
                service,
                health=ServiceHealth.HEALTHY,
                observation=observation,
                node_loss=(f"{service.service_id}-replica-0",),
            ),
        ),
        (),
        (),
        ("node-a",),
    )

    with pytest.raises(ProductCommandCenterScopeError, match="service health disagrees"):
        _validate_operations_scope("project-1", snapshot)


def test_operations_snapshot_accepts_rollback_then_credential_revocation() -> None:
    secret_ref = "credential://provider/project-1/writer"
    service = _service(credential_refs=(secret_ref,))
    observation = _observation(service, failed=(0,))
    snapshot = ProductOperationsSnapshot(
        "project-1",
        (
            ServiceRecord(
                service,
                health=ServiceHealth.BLOCKED,
                observation=observation,
                rollback=_rollback(service),
                blocked_credentials=(secret_ref,),
            ),
        ),
        (),
        (secret_ref,),
        (),
    )

    _validate_operations_scope("project-1", snapshot)


def test_operations_snapshot_accepts_rollback_history_after_credential_restore() -> None:
    service = _service(credential_refs=("credential://provider/project-1/writer",))
    observation = _observation(service, failed=(0,))
    snapshot = ProductOperationsSnapshot(
        "project-1",
        (
            ServiceRecord(
                service,
                health=ServiceHealth.ROLLBACK_REQUIRED,
                observation=observation,
                rollback=_rollback(service),
            ),
        ),
        (),
        (),
        (),
    )

    _validate_operations_scope("project-1", snapshot)


def test_operations_snapshot_rejects_rollback_without_prior_observation() -> None:
    service = _service()
    snapshot = ProductOperationsSnapshot(
        "project-1",
        (
            ServiceRecord(
                service,
                health=ServiceHealth.ROLLED_BACK,
                rollback=_rollback(service),
            ),
        ),
        (),
        (),
        (),
    )

    with pytest.raises(ProductCommandCenterScopeError, match="prior service observation"):
        _validate_operations_scope("project-1", snapshot)
