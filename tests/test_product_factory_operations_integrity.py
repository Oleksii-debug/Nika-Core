from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from nika_core.product_factory_operations import ProductOperationsCoordinator
from nika_core.product_factory_operations_contracts import (
    DeployableService,
    MaintenanceAction,
    MaintenanceRequest,
    MaintenanceResult,
    MaintenanceState,
    ProductOperationsError,
    RollbackObservation,
    ServiceHealth,
    ServiceObservation,
    ServiceReplica,
)

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)


def sha(value: int) -> str:
    return f"{value:040x}"[-40:]


def service(
    service_id: str,
    *,
    release: int = 1,
    node: str | None = None,
    credential: str | None = None,
) -> DeployableService:
    return DeployableService(
        service_id,
        "project-a",
        "prod-eu",
        sha(release),
        0,
        (ServiceReplica(f"{service_id}-r1", node or f"node-{service_id}"),),
        1,
        (),
        () if credential is None else (credential,),
    )


def healthy(service_value: DeployableService, *, observed_at: datetime = NOW) -> ServiceObservation:
    return ServiceObservation(
        service_value.service_id,
        service_value.release_sha,
        (service_value.replicas[0].replica_id,),
        (),
        (f"health://{service_value.service_id}/green",),
        observed_at,
    )


def failed(service_value: DeployableService, *, observed_at: datetime = NOW) -> ServiceObservation:
    return ServiceObservation(
        service_value.service_id,
        service_value.release_sha,
        (),
        (service_value.replicas[0].replica_id,),
        (f"health://{service_value.service_id}/failed",),
        observed_at,
    )


class Port:
    def __init__(self, *, uncertain: bool = False) -> None:
        self.uncertain = uncertain
        self.applied: list[MaintenanceRequest] = []
        self.inspected: list[MaintenanceRequest] = []

    def apply(self, request: MaintenanceRequest) -> MaintenanceResult:
        self.applied.append(request)
        if self.uncertain:
            return MaintenanceResult(False, True, (f"provider://{request.request_id}/uncertain",))
        return MaintenanceResult(True, False, (f"provider://{request.request_id}/applied",))

    def inspect(self, request: MaintenanceRequest) -> MaintenanceResult:
        self.inspected.append(request)
        return MaintenanceResult(True, False, (f"provider://{request.request_id}/inspected",))


def request(service_id: str, request_id: str | None = None) -> MaintenanceRequest:
    return MaintenanceRequest(
        request_id or f"maint-{service_id}",
        service_id,
        MaintenanceAction.RESTART,
        "Approved evidence-bound service maintenance",
        (f"health://{service_id}/green",),
        approval_ref=f"approval://{service_id}/maintenance",
    )


def test_maintenance_side_effect_rejects_missing_and_cross_service_evidence() -> None:
    port = Port()
    coordinator = ProductOperationsCoordinator("project-a", port)
    api = service("api", release=10)
    web = service("web", release=11)
    coordinator.register(api)
    coordinator.register(web)

    with pytest.raises(ProductOperationsError, match="requires approved service"):
        coordinator.request_maintenance(request("api"))
    assert port.applied == []

    coordinator.record_observation(healthy(api))
    coordinator.record_observation(healthy(web))
    forged = replace(request("api"), evidence_refs=("health://web/green",))
    with pytest.raises(ProductOperationsError, match="not bound to the requested service"):
        coordinator.request_maintenance(forged)
    assert port.applied == []

    saved = coordinator.request_maintenance(request("api"))
    assert saved.result.applied is True
    assert [item.service_id for item in port.applied] == ["api"]


def test_observation_lineage_rejects_rewind_and_same_time_conflict() -> None:
    coordinator = ProductOperationsCoordinator("project-a")
    api = service("api", release=20)
    coordinator.register(api)
    first = healthy(api)
    saved = coordinator.record_observation(first)
    assert coordinator.record_observation(first) is saved

    with pytest.raises(ProductOperationsError, match="timestamp conflicts"):
        coordinator.record_observation(
            replace(
                first,
                healthy_replica_ids=(),
                failed_replica_ids=(api.replicas[0].replica_id,),
                evidence_refs=("health://api/conflict",),
            )
        )
    with pytest.raises(ProductOperationsError, match="cannot rewind"):
        coordinator.record_observation(
            replace(first, observed_at=NOW - timedelta(seconds=1))
        )


def test_rollback_is_time_ordered_and_exact_replay_is_idempotent() -> None:
    coordinator = ProductOperationsCoordinator("project-a")
    api = service("api", release=30)
    coordinator.register(api)
    coordinator.record_observation(failed(api))
    early = RollbackObservation(
        "api",
        api.release_sha,
        sha(29),
        True,
        ("rollback://api/early",),
        NOW - timedelta(seconds=1),
    )
    with pytest.raises(ProductOperationsError, match="cannot predate"):
        coordinator.record_rollback(early)

    proof = replace(
        early,
        evidence_refs=("rollback://api/verified",),
        observed_at=NOW + timedelta(seconds=1),
    )
    saved = coordinator.record_rollback(proof)
    assert saved.health is ServiceHealth.ROLLED_BACK
    assert coordinator.record_rollback(proof) is saved
    with pytest.raises(ProductOperationsError, match="conflicts with prior"):
        coordinator.record_rollback(replace(proof, restored_release_sha=sha(28)))


def test_terminal_rollback_survives_node_and_credential_changes_and_rejects_late_observation() -> None:
    coordinator = ProductOperationsCoordinator("project-a")
    api = service("api", release=31, node="node-a", credential="credential://api")
    coordinator.register(api)
    coordinator.record_observation(failed(api))
    proof = RollbackObservation(
        "api",
        api.release_sha,
        sha(30),
        True,
        ("rollback://api/verified",),
        NOW + timedelta(seconds=1),
    )
    coordinator.record_rollback(proof)

    coordinator.record_node_availability("node-a", available=False)
    assert coordinator.snapshot().services[0].health is ServiceHealth.ROLLED_BACK

    coordinator.revoke_credential("credential://api")
    assert coordinator.snapshot().services[0].health is ServiceHealth.BLOCKED
    coordinator.restore_credential("credential://api")
    assert coordinator.snapshot().services[0].health is ServiceHealth.ROLLED_BACK

    with pytest.raises(ProductOperationsError, match="terminal rollback"):
        coordinator.record_observation(
            failed(api, observed_at=NOW + timedelta(seconds=2))
        )

    snapshot = coordinator.snapshot()
    restarted = ProductOperationsCoordinator("project-a")
    restarted.restore(snapshot)
    assert restarted.snapshot() == snapshot
    assert restarted.snapshot().services[0].health is ServiceHealth.ROLLED_BACK


def test_restore_rejects_derived_health_credential_and_node_loss_tamper_atomically() -> None:
    coordinator = ProductOperationsCoordinator("project-a")
    api = service("api", release=40, node="node-a", credential="credential://api")
    coordinator.register(api)
    coordinator.record_observation(healthy(api))
    coordinator.record_node_availability("node-a", available=False)
    coordinator.revoke_credential("credential://api")
    snapshot = coordinator.snapshot()

    target = ProductOperationsCoordinator("project-a")
    sentinel = service("sentinel", release=41)
    target.register(sentinel)

    original = snapshot.services[0]
    corruptions = (
        replace(original, health=ServiceHealth.HEALTHY),
        replace(original, blocked_credentials=()),
        replace(original, node_loss=()),
    )
    messages = ("health", "blocked credential", "node-loss")
    for corrupted, message in zip(corruptions, messages, strict=True):
        with pytest.raises(ProductOperationsError, match=message):
            target.restore(replace(snapshot, services=(corrupted,)))
        assert target.snapshot().services[0].service.service_id == "sentinel"


def test_restore_rejects_maintenance_without_service_evidence_or_approval() -> None:
    port = Port()
    coordinator = ProductOperationsCoordinator("project-a", port)
    api = service("api", release=50)
    coordinator.register(api)
    coordinator.record_observation(healthy(api))
    coordinator.request_maintenance(request("api"))
    snapshot = coordinator.snapshot()
    maintenance = snapshot.maintenance_records[0]

    bad_evidence = replace(
        maintenance,
        request=replace(
            maintenance.request,
            evidence_refs=("health://other/green",),
        ),
    )
    with pytest.raises(ProductOperationsError, match="not bound"):
        ProductOperationsCoordinator("project-a").restore(
            replace(snapshot, maintenance_records=(bad_evidence,))
        )

    no_approval = replace(
        maintenance,
        request=replace(maintenance.request, approval_ref=None),
    )
    with pytest.raises(ProductOperationsError, match="lacks durable approval"):
        ProductOperationsCoordinator("project-a").restore(
            replace(snapshot, maintenance_records=(no_approval,))
        )

    wrong_service = replace(
        maintenance,
        request=replace(maintenance.request, service_id="missing"),
    )
    with pytest.raises(ProductOperationsError, match="unknown service"):
        ProductOperationsCoordinator("project-a").restore(
            replace(snapshot, maintenance_records=(wrong_service,))
        )


def test_restore_rejects_forged_maintenance_state_without_record() -> None:
    coordinator = ProductOperationsCoordinator("project-a")
    api = service("api", release=60)
    coordinator.register(api)
    coordinator.record_observation(healthy(api))
    snapshot = coordinator.snapshot()
    forged = replace(snapshot.services[0], maintenance=MaintenanceState.RESTARTING)

    with pytest.raises(ProductOperationsError, match="lacks durable request evidence"):
        ProductOperationsCoordinator("project-a").restore(
            replace(snapshot, services=(forged,))
        )


def test_contracts_reject_boolean_numeric_and_duplicate_evidence_identity() -> None:
    with pytest.raises(ProductOperationsError, match="wave/replicas"):
        replace(service("api"), wave=True)
    with pytest.raises(ProductOperationsError, match="minimum healthy"):
        replace(service("api"), min_healthy_replicas=True)
    with pytest.raises(ProductOperationsError, match="must not contain duplicates"):
        replace(
            healthy(service("api")),
            evidence_refs=("health://api/green", "health://api/green"),
        )
    with pytest.raises(ProductOperationsError, match="flags must be boolean"):
        MaintenanceResult(1, False, ("provider://result",))  # type: ignore[arg-type]


def test_fifty_service_maintenance_remains_service_isolated_after_restart() -> None:
    port = Port()
    coordinator = ProductOperationsCoordinator("project-a", port)
    maintained: set[str] = set()
    for index in range(50):
        service_id = f"svc-{index:02d}"
        value = service(service_id, release=1000 + index)
        coordinator.register(value)
        coordinator.record_observation(healthy(value))
        if index % 10 == 0:
            maintained.add(service_id)
            coordinator.request_maintenance(request(service_id))

    snapshot = coordinator.snapshot()
    restarted = ProductOperationsCoordinator("project-a")
    restarted.restore(snapshot)
    records = {item.service.service_id: item for item in restarted.snapshot().services}

    assert len(records) == 50
    assert len(snapshot.maintenance_records) == 5
    assert {
        service_id
        for service_id, record in records.items()
        if record.maintenance is MaintenanceState.RESTARTING
    } == maintained
    assert all(record.health is ServiceHealth.HEALTHY for record in records.values())
