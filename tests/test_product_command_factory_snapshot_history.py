from __future__ import annotations

from datetime import UTC, datetime

import pytest

from nika_core.product_command.factory_snapshot_safety import (
    ProductFactorySnapshotIntegrityError,
)
from nika_core.product_command.factory_status_adapter import product_operations_status_entries
from nika_core.product_factory_operations import ProductOperationsSnapshot, ServiceRecord
from nika_core.product_factory_operations_contracts import (
    DeployableService,
    RollbackObservation,
    ServiceHealth,
    ServiceObservation,
    ServiceReplica,
)

NOW = datetime(2026, 8, 21, 12, 45, tzinfo=UTC)
SHA_A = "a" * 40
SHA_B = "b" * 40
SECRET_REF = "credential://provider/project-1/writer"


def _service(*, credential_refs: tuple[str, ...] = ()) -> DeployableService:
    return DeployableService(
        "service-api",
        "project-1",
        "stage",
        SHA_A,
        0,
        (ServiceReplica("replica-1", "node-a"),),
        min_healthy_replicas=1,
        credential_refs=credential_refs,
    )


def _failed_observation() -> ServiceObservation:
    return ServiceObservation(
        "service-api",
        SHA_A,
        (),
        ("replica-1",),
        ("health://service-api/failed",),
        NOW,
    )


def _rollback() -> RollbackObservation:
    return RollbackObservation(
        "service-api",
        SHA_A,
        SHA_B,
        True,
        ("rollback://service-api/restored",),
        NOW,
    )


def test_factory_projection_rejects_omitted_revoked_service_credential_blocker() -> None:
    service = _service(credential_refs=(SECRET_REF,))
    snapshot = ProductOperationsSnapshot(
        "project-1",
        (ServiceRecord(service, health=ServiceHealth.PENDING),),
        (),
        (SECRET_REF,),
        (),
    )

    with pytest.raises(
        ProductFactorySnapshotIntegrityError,
        match="omits revoked service credential blocker",
    ):
        product_operations_status_entries("project-1", snapshot)


def test_factory_projection_accepts_historical_successful_rollback_with_current_observation_state() -> None:
    service = _service()
    snapshot = ProductOperationsSnapshot(
        "project-1",
        (
            ServiceRecord(
                service,
                health=ServiceHealth.ROLLBACK_REQUIRED,
                observation=_failed_observation(),
                rollback=_rollback(),
            ),
        ),
        (),
        (),
        (),
    )

    entries = product_operations_status_entries("project-1", snapshot)

    assert entries[0].state == "rollback_required"
    assert any(
        item.reference == "rollback://service-api/restored"
        for item in entries[0].evidence
    )


def test_factory_projection_accepts_rollback_history_after_later_credential_revocation() -> None:
    service = _service(credential_refs=(SECRET_REF,))
    snapshot = ProductOperationsSnapshot(
        "project-1",
        (
            ServiceRecord(
                service,
                health=ServiceHealth.BLOCKED,
                observation=_failed_observation(),
                rollback=_rollback(),
                blocked_credentials=(SECRET_REF,),
            ),
        ),
        (),
        (SECRET_REF,),
        (),
    )

    entries = product_operations_status_entries("project-1", snapshot)

    assert entries[0].state == "blocked"


def test_factory_projection_rejects_rollback_history_without_prior_observation() -> None:
    service = _service()
    snapshot = ProductOperationsSnapshot(
        "project-1",
        (
            ServiceRecord(
                service,
                health=ServiceHealth.ROLLED_BACK,
                rollback=_rollback(),
            ),
        ),
        (),
        (),
        (),
    )

    with pytest.raises(
        ProductFactorySnapshotIntegrityError,
        match="lacks prior service observation",
    ):
        product_operations_status_entries("project-1", snapshot)
