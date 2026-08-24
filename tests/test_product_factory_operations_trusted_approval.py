"""PF8 convergence regressions for trusted maintenance authority and retry serialization."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from threading import Event, Lock

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
from pf8_effect_journal_fake import MemoryEffectJournal

SHA_A = "a" * 40
SHA_B = "b" * 40
NOW = datetime(2026, 8, 23, 21, 0, tzinfo=UTC)


def _service(service_id: str, release_sha: str) -> DeployableService:
    return DeployableService(
        service_id=service_id,
        project_id="project-a",
        environment_id="prod-eu",
        release_sha=release_sha,
        wave=0,
        replicas=(ServiceReplica(f"{service_id}-replica", f"node-{service_id}"),),
    )


def _healthy(service: DeployableService) -> ServiceObservation:
    return ServiceObservation(
        service_id=service.service_id,
        release_sha=service.release_sha,
        healthy_replica_ids=(service.replicas[0].replica_id,),
        failed_replica_ids=(),
        evidence_refs=(f"health:{service.service_id}:{service.release_sha}",),
        observed_at=NOW,
    )


def _request(service: DeployableService, request_id: str = "maintenance-1") -> MaintenanceRequest:
    return MaintenanceRequest(
        request_id=request_id,
        service_id=service.service_id,
        action=MaintenanceAction.RESTART,
        reason="evidence-bound restart",
        evidence_refs=(f"health:{service.service_id}:{service.release_sha}",),
        approval_ref=f"approval:{service.service_id}:{request_id}",
    )


def _approval_key(
    project_id: str,
    service: DeployableService,
    request: MaintenanceRequest,
) -> tuple[object, ...]:
    return (
        project_id,
        service.service_id,
        service.environment_id,
        service.release_sha,
        request.request_id,
        request.service_id,
        request.action,
        request.reason,
        request.evidence_refs,
        request.approval_ref,
    )


class ExactApprovalAuthority:
    def __init__(self) -> None:
        self._allowed: set[tuple[object, ...]] = set()

    def allow(self, service: DeployableService, request: MaintenanceRequest) -> None:
        self._allowed.add(_approval_key("project-a", service, request))

    def verify(
        self,
        *,
        project_id: str,
        service: DeployableService,
        request: MaintenanceRequest,
    ) -> bool:
        return _approval_key(project_id, service, request) in self._allowed


class CountingPort:
    def __init__(self) -> None:
        self.apply_calls = 0
        self.inspect_calls = 0

    def apply(self, request: MaintenanceRequest) -> MaintenanceResult:
        self.apply_calls += 1
        return MaintenanceResult(
            applied=True,
            uncertain=False,
            evidence_refs=(f"provider:{request.request_id}:applied",),
        )

    def inspect(self, request: MaintenanceRequest) -> MaintenanceResult:
        self.inspect_calls += 1
        return MaintenanceResult(
            applied=True,
            uncertain=False,
            evidence_refs=(f"provider:{request.request_id}:inspected",),
        )


class BlockingPort(CountingPort):
    def __init__(self) -> None:
        super().__init__()
        self.started = Event()
        self.release = Event()
        self._count_lock = Lock()

    def apply(self, request: MaintenanceRequest) -> MaintenanceResult:
        with self._count_lock:
            self.apply_calls += 1
        self.started.set()
        assert self.release.wait(2), "test provider was not released"
        return MaintenanceResult(
            applied=True,
            uncertain=False,
            evidence_refs=(f"provider:{request.request_id}:applied",),
        )


def _coordinator(
    port: CountingPort,
    authority: ExactApprovalAuthority | None,
) -> tuple[ProductOperationsCoordinator, DeployableService]:
    coordinator = ProductOperationsCoordinator(
        "project-a",
        port=port,
        approval_authority=authority,
        effect_journal=MemoryEffectJournal(),
    )
    service = _service("service-a", SHA_A)
    coordinator.register(service)
    coordinator.record_observation(_healthy(service))
    return coordinator, service


def test_aud02_caller_constructed_approval_string_cannot_authorize_side_effect() -> None:
    port = CountingPort()
    coordinator, service = _coordinator(port, authority=None)
    forged = replace(
        _request(service),
        approval_ref="candidate-controlled:approved:R4",
    )

    with pytest.raises(ProductOperationsError, match="trusted approval authority"):
        coordinator.request_maintenance(forged)

    assert port.apply_calls == 0


def test_trusted_approval_is_bound_to_exact_service_release_and_request() -> None:
    port = CountingPort()
    authority = ExactApprovalAuthority()
    coordinator, service = _coordinator(port, authority)
    approved = _request(service)
    authority.allow(service, approved)

    changed_action = replace(approved, action=MaintenanceAction.DRAIN)
    with pytest.raises(ProductOperationsError, match="exact service/release/request"):
        coordinator.request_maintenance(changed_action)
    assert port.apply_calls == 0

    other = _service("service-b", SHA_B)
    coordinator.register(other)
    coordinator.record_observation(_healthy(other))
    cross_service = replace(
        approved,
        request_id="maintenance-cross-service",
        service_id=other.service_id,
        evidence_refs=_healthy(other).evidence_refs,
    )
    with pytest.raises(ProductOperationsError, match="exact service/release/request"):
        coordinator.request_maintenance(cross_service)
    assert port.apply_calls == 0

    saved = coordinator.request_maintenance(approved)
    assert saved.request == approved
    assert port.apply_calls == 1


def test_missing_durable_effect_journal_blocks_provider_dispatch() -> None:
    port = CountingPort()
    authority = ExactApprovalAuthority()
    coordinator = ProductOperationsCoordinator(
        "project-a",
        port=port,
        approval_authority=authority,
    )
    service = _service("service-a", SHA_A)
    coordinator.register(service)
    coordinator.record_observation(_healthy(service))
    approved = _request(service)
    authority.allow(service, approved)

    with pytest.raises(ProductOperationsError, match="durable host-bound effect journal"):
        coordinator.request_maintenance(approved)

    assert port.apply_calls == 0


def test_concurrent_exact_retry_dispatches_provider_once() -> None:
    port = BlockingPort()
    authority = ExactApprovalAuthority()
    coordinator, service = _coordinator(port, authority)
    approved = _request(service)
    authority.allow(service, approved)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(coordinator.request_maintenance, approved)
        assert port.started.wait(2), "first maintenance call did not reach provider"
        second = pool.submit(coordinator.request_maintenance, approved)
        port.release.set()
        first_result = first.result(timeout=2)
        second_result = second.result(timeout=2)

    assert first_result == second_result
    assert port.apply_calls == 1
