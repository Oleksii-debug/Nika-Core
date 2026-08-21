from __future__ import annotations

from datetime import UTC, datetime

import pytest

from nika_core.product_factory_operations import (
    MaintenanceState,
    ProductOperationsCoordinator,
)
from nika_core.product_factory_operations_contracts import (
    DeployableService,
    MaintenanceAction,
    MaintenanceRequest,
    MaintenanceResult,
    ProductOperationsError,
    RollbackObservation,
    ServiceHealth,
    ServiceObservation,
    ServiceReplica,
)


def _sha(value: int) -> str:
    return f"{value:040x}"[-40:]


def _service(
    service_id: str,
    *,
    wave: int = 0,
    nodes: tuple[str, ...] = ("node-a", "node-b"),
    minimum: int = 1,
    dependencies: tuple[str, ...] = (),
    credentials: tuple[str, ...] = (),
    sha: int = 1,
) -> DeployableService:
    return DeployableService(
        service_id,
        "p-social",
        "staging-eu",
        _sha(sha),
        wave,
        tuple(
            ServiceReplica(f"{service_id}-replica-{index}", node)
            for index, node in enumerate(nodes)
        ),
        minimum,
        dependencies,
        credentials,
    )


def _observation(
    service: DeployableService,
    healthy: tuple[int, ...],
    failed: tuple[int, ...] = (),
) -> ServiceObservation:
    return ServiceObservation(
        service.service_id,
        service.release_sha,
        tuple(service.replicas[index].replica_id for index in healthy),
        tuple(service.replicas[index].replica_id for index in failed),
        (f"health:{service.service_id}",),
        datetime(2026, 8, 21, tzinfo=UTC),
    )


class FakeOperationsPort:
    def __init__(self, *results: MaintenanceResult) -> None:
        self.results = list(results)
        self.applied: list[MaintenanceRequest] = []
        self.inspected: list[MaintenanceRequest] = []

    def apply(self, request: MaintenanceRequest) -> MaintenanceResult:
        self.applied.append(request)
        return self.results.pop(0)

    def inspect(self, request: MaintenanceRequest) -> MaintenanceResult:
        self.inspected.append(request)
        return self.results.pop(0)


def test_staged_wave_waits_for_exact_dependency_health() -> None:
    coordinator = ProductOperationsCoordinator("p-social")
    api = _service("api", wave=0, sha=10)
    web = _service("web", wave=1, dependencies=("api",), sha=11)
    chat = _service("chat", wave=1, dependencies=("api",), sha=12)
    for service in (api, web, chat):
        coordinator.register(service)

    assert [service.service_id for service in coordinator.ready_services()] == ["api"]
    coordinator.record_observation(_observation(api, (0, 1)))
    assert [service.service_id for service in coordinator.ready_services()] == ["chat", "web"]


def test_partial_node_loss_isolated_from_parallel_service() -> None:
    coordinator = ProductOperationsCoordinator("p-social")
    api = _service("api", nodes=("node-a", "node-b"), sha=20)
    search = _service("search", nodes=("node-c", "node-d"), sha=21)
    for service in (api, search):
        coordinator.register(service)
        coordinator.record_observation(_observation(service, (0, 1)))

    coordinator.record_node_availability("node-a", available=False)
    records = {record.service.service_id: record for record in coordinator.snapshot().services}
    assert records["api"].health is ServiceHealth.DEGRADED
    assert records["api"].node_loss == ("api-replica-0",)
    assert records["search"].health is ServiceHealth.HEALTHY


def test_failed_service_rolls_back_without_corrupting_unrelated_state() -> None:
    coordinator = ProductOperationsCoordinator("p-social")
    messages = _service("messages", nodes=("node-a",), sha=30)
    profiles = _service("profiles", nodes=("node-b",), sha=31)
    coordinator.register(messages)
    coordinator.register(profiles)
    coordinator.record_observation(_observation(profiles, (0,)))
    failed = coordinator.record_observation(_observation(messages, (), (0,)))
    assert failed.health is ServiceHealth.ROLLBACK_REQUIRED

    with pytest.raises(ProductOperationsError, match="failed release SHA mismatch"):
        coordinator.record_rollback(
            RollbackObservation(
                "messages",
                _sha(999),
                _sha(29),
                True,
                ("rollback:wrong",),
                datetime(2026, 8, 21, tzinfo=UTC),
            )
        )
    rolled_back = coordinator.record_rollback(
        RollbackObservation(
            "messages",
            messages.release_sha,
            _sha(29),
            True,
            ("rollback:messages",),
            datetime(2026, 8, 21, tzinfo=UTC),
        )
    )
    assert rolled_back.health is ServiceHealth.ROLLED_BACK
    assert coordinator.health_summary().healthy == ("profiles", "messages")


def test_credential_revocation_mid_project_blocks_only_dependents_and_survives_restart() -> None:
    coordinator = ProductOperationsCoordinator("p-social")
    api = _service("api", credentials=("secret:api",), sha=40)
    media = _service("media", credentials=("secret:media",), sha=41)
    for service in (api, media):
        coordinator.register(service)
        coordinator.record_observation(_observation(service, (0, 1)))

    assert coordinator.revoke_credential("secret:api") == ("api",)
    snapshot = coordinator.snapshot()
    assert snapshot.revoked_credentials == ("secret:api",)
    assert "leases" not in snapshot.__dataclass_fields__
    assert "handles" not in snapshot.__dataclass_fields__

    restarted = ProductOperationsCoordinator("p-social")
    restarted.restore(snapshot)
    records = {record.service.service_id: record for record in restarted.snapshot().services}
    assert records["api"].health is ServiceHealth.BLOCKED
    assert records["media"].health is ServiceHealth.HEALTHY
    assert restarted.restore_credential("secret:api") == ("api",)
    assert restarted.snapshot().services[0].health is ServiceHealth.HEALTHY


def test_observation_is_exact_sha_bound() -> None:
    coordinator = ProductOperationsCoordinator("p-social")
    api = _service("api", sha=50)
    coordinator.register(api)
    wrong = ServiceObservation(
        "api",
        _sha(51),
        ("api-replica-0",),
        (),
        ("health:wrong",),
        datetime(2026, 8, 21, tzinfo=UTC),
    )
    with pytest.raises(ProductOperationsError, match="release SHA mismatch"):
        coordinator.record_observation(wrong)


def test_maintenance_requires_approval_and_uncertain_result_reconciles() -> None:
    port = FakeOperationsPort(
        MaintenanceResult(False, True, ("fake:uncertain",)),
        MaintenanceResult(True, False, ("fake:inspected",)),
    )
    coordinator = ProductOperationsCoordinator("p-social", port)
    api = _service("api", sha=60)
    coordinator.register(api)
    unapproved = MaintenanceRequest(
        "maint-1",
        "api",
        MaintenanceAction.RESTART,
        "dependency upgrade",
        ("plan:maint-1",),
    )
    with pytest.raises(ProductOperationsError, match="explicit approval"):
        coordinator.request_maintenance(unapproved)
    assert port.applied == []

    approved = MaintenanceRequest(
        "maint-1",
        "api",
        MaintenanceAction.RESTART,
        "dependency upgrade",
        ("plan:maint-1",),
        approval_ref="approval:operator-42",
    )
    first = coordinator.request_maintenance(approved)
    assert first.result.uncertain is True
    assert coordinator.snapshot().services[0].maintenance is MaintenanceState.PAUSED
    reconciled = coordinator.reconcile_maintenance("maint-1")
    assert reconciled.reconciled is True
    assert coordinator.snapshot().services[0].maintenance is MaintenanceState.RESTARTING
    assert port.applied == [approved]
    assert port.inspected == [approved]


def test_sixty_service_social_product_fixture_is_deterministic_across_failures_and_restart() -> None:
    coordinator = ProductOperationsCoordinator("p-social")
    services: list[DeployableService] = []
    for index in range(60):
        wave = index // 20
        dependency = () if wave == 0 else (f"service-{index - 20:02d}",)
        service = _service(
            f"service-{index:02d}",
            wave=wave,
            nodes=(f"node-{index % 8}", f"node-{(index + 1) % 8}"),
            dependencies=dependency,
            credentials=(f"secret:{index % 6}",),
            sha=1000 + index,
        )
        services.append(service)
        coordinator.register(service)

    assert len(coordinator.ready_services()) == 20
    for service in services[:20]:
        coordinator.record_observation(_observation(service, (0, 1)))
    assert len(coordinator.ready_services()) == 20

    coordinator.record_node_availability("node-3", available=False)
    node_affected = {
        record.service.service_id
        for record in coordinator.snapshot().services
        if record.node_loss
    }
    assert 0 < len(node_affected) < 60

    credential_affected = set(coordinator.revoke_credential("secret:2"))
    assert len(credential_affected) == 10
    snapshot = coordinator.snapshot()
    restarted = ProductOperationsCoordinator("p-social")
    restarted.restore(snapshot)
    assert restarted.snapshot() == snapshot
    assert set(restarted.health_summary().blocked) == credential_affected
