"""AUD02 QA_ONLY oracle for DEV10 maintenance approval authority."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from nika_core.product_factory_operations import ProductOperationsCoordinator
from nika_core.product_factory_operations_contracts import (
    DeployableService,
    MaintenanceAction,
    MaintenanceRequest,
    MaintenanceResult,
    ProductOperationsError,
    ServiceObservation,
    ServiceReplica,
)

SHA = "a" * 40
NOW = datetime(2026, 8, 23, 20, 30, tzinfo=UTC)


class _SideEffectPort:
    def __init__(self) -> None:
        self.apply_calls = 0

    def apply(self, request: MaintenanceRequest) -> MaintenanceResult:
        del request
        self.apply_calls += 1
        return MaintenanceResult(applied=True, uncertain=False, evidence_refs=("provider:applied",))

    def inspect(self, request: MaintenanceRequest) -> MaintenanceResult:
        del request
        return MaintenanceResult(applied=False, uncertain=False, evidence_refs=("provider:inspect",))


def test_caller_constructed_approval_string_cannot_authorize_maintenance_side_effect() -> None:
    port = _SideEffectPort()
    coordinator = ProductOperationsCoordinator("project-a", port=port)
    coordinator.register(
        DeployableService(
            service_id="service-a",
            project_id="project-a",
            environment_id="staging",
            release_sha=SHA,
            wave=0,
            replicas=(ServiceReplica("replica-a", "node-a"),),
        )
    )
    coordinator.record_observation(
        ServiceObservation(
            service_id="service-a",
            release_sha=SHA,
            healthy_replica_ids=("replica-a",),
            failed_replica_ids=(),
            evidence_refs=("health:service-a",),
            observed_at=NOW,
        )
    )

    forged = MaintenanceRequest(
        request_id="maintenance-forged-approval",
        service_id="service-a",
        action=MaintenanceAction.RESTART,
        reason="candidate asks to restart",
        evidence_refs=("health:service-a",),
        approval_ref="candidate-controlled:approved:R4",
    )

    with pytest.raises(ProductOperationsError):
        coordinator.request_maintenance(forged)

    assert port.apply_calls == 0, "caller-controlled approval text reached maintenance side effect"
