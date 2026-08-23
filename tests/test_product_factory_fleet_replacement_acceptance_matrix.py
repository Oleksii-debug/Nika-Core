from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from pf3_fleet_replacement_support import (
    _authorized_plan,
    _digest,
    _fixture,
    _node,
    _submit,
)

from nika_core.product_factory_deployment import NodeCapabilities, Platform
from nika_core.product_factory_fleet_replacement import (
    DurableReplacementDispatch,
    FleetReplacementCoordinator,
    FleetReplacementError,
    FleetReplacementState,
    ReplicaReplacementResult,
    ReplicaReplacementState,
    fleet_replacement_request_fingerprint,
)


def test_target_selection_enforces_service_anti_affinity_and_max_replicas_per_node() -> None:
    coordinator, _, _, _, _, placements = _fixture(
        service_count=4,
        replica_count=3,
        node_count=8,
    )
    key = ("service-000", "service-000-replica-0")
    sibling_nodes = {
        node_id
        for (service_id, replica_id), (_, node_id) in placements.items()
        if service_id == "service-000" and replica_id != key[1]
    }
    initial_counts: dict[str, int] = {}
    for _, node_id in placements.values():
        initial_counts[node_id] = initial_counts.get(node_id, 0) + 1

    plan, authority, review_ref = _authorized_plan(
        placements,
        (key,),
        max_replicas_per_node=2,
    )
    _submit(coordinator, plan, authority, review_ref)
    finished = coordinator.advance(
        plan.plan_id,
        now=datetime(2026, 8, 23, 12, tzinfo=UTC),
    )

    record = finished.replacements[0]
    assert finished.state is FleetReplacementState.SUCCEEDED
    assert record.target_node_id is not None
    assert record.target_node_id not in sibling_nodes
    assert record.target_node_id != placements[key][1]
    assert initial_counts.get(record.target_node_id, 0) < plan.max_replicas_per_node


def test_target_selection_reuses_platform_capability_and_resource_constraints() -> None:
    coordinator, _, _, nodes, _, placements = _fixture(
        service_count=1,
        replica_count=3,
        node_count=5,
    )
    windows_candidate = _node(1)
    nodes.register(
        replace(
            windows_candidate,
            identity=replace(windows_candidate.identity, platform=Platform.WINDOWS),
        )
    )
    linux_without_replacement = _node(2)
    nodes.register(
        replace(
            linux_without_replacement,
            capabilities=NodeCapabilities(
                frozenset({"deploy"}),
                frozenset({"ansible"}),
            ),
        )
    )
    nodes.register(_node(3, memory_mb=128))
    nodes.register(_node(4))

    key = ("service-000", "service-000-replica-0")
    plan, authority, review_ref = _authorized_plan(placements, (key,))
    _submit(coordinator, plan, authority, review_ref)
    finished = coordinator.advance(
        plan.plan_id,
        now=datetime(2026, 8, 23, 12, tzinfo=UTC),
    )

    assert finished.state is FleetReplacementState.SUCCEEDED
    assert finished.replacements[0].target_node_id == "node-4"


def test_min_healthy_replicas_blocks_second_healthy_source_disruption() -> None:
    coordinator, _, operations, _, port, placements = _fixture(
        service_count=1,
        replica_count=3,
        node_count=6,
    )
    operations.record_node_availability("node-1", available=False)
    key = ("service-000", "service-000-replica-0")
    plan, authority, review_ref = _authorized_plan(
        placements,
        (key,),
        max_unavailable=10,
    )
    _submit(coordinator, plan, authority, review_ref)

    blocked = coordinator.advance(
        plan.plan_id,
        now=datetime(2026, 8, 23, 12, tzinfo=UTC),
    )
    assert blocked.state is FleetReplacementState.BLOCKED
    assert blocked.replacements[0].state is ReplicaReplacementState.BLOCKED_BUDGET
    assert port.calls == []


def test_already_unavailable_source_is_allowed_to_heal_with_zero_new_disruption_budget() -> None:
    coordinator, _, operations, _, port, placements = _fixture(
        service_count=1,
        replica_count=3,
        node_count=6,
    )
    key = ("service-000", "service-000-replica-0")
    source_node_id = placements[key][1]
    operations.record_node_availability(source_node_id, available=False)
    plan, authority, review_ref = _authorized_plan(
        placements,
        (key,),
        max_unavailable=0,
    )
    _submit(coordinator, plan, authority, review_ref)

    finished = coordinator.advance(
        plan.plan_id,
        now=datetime(2026, 8, 23, 12, tzinfo=UTC),
    )
    assert finished.state is FleetReplacementState.SUCCEEDED
    assert finished.replacements[0].target_node_id != source_node_id
    assert len([call for call in port.calls if call[0] == "apply"]) == 1


class _WrongEvidencePort:
    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.calls: list[tuple[str, str]] = []

    def apply(self, request):
        self.calls.append(("apply", request.request_id))
        return self._result(request)

    def inspect(self, request):
        self.calls.append(("inspect", request.request_id))
        return self._result(request)

    def _result(self, request):
        observed_node_id = request.target_node_id
        artifact_digest = request.artifact_digest
        healthy = True
        if self.kind == "target":
            observed_node_id = "node-forged-evidence"
        elif self.kind == "artifact":
            artifact_digest = _digest(999_991)
        elif self.kind == "health":
            healthy = False
        else:  # pragma: no cover - test construction invariant
            raise AssertionError(f"unknown wrong-evidence kind: {self.kind}")
        return ReplicaReplacementResult(
            applied=True,
            uncertain=False,
            evidence_refs=(f"provider:wrong-{self.kind}",),
            observed_node_id=observed_node_id,
            release_version=request.release_version,
            release_sha=request.release_sha,
            artifact_digest=artifact_digest,
            healthy=healthy,
        )


@pytest.mark.parametrize("kind", ["target", "artifact", "health"])
def test_applied_result_requires_exact_target_artifact_and_health_evidence(kind: str) -> None:
    coordinator, _, _, _, _, placements = _fixture(service_count=1)
    key = ("service-000", "service-000-replica-0")
    plan, authority, review_ref = _authorized_plan(placements, (key,))
    port = _WrongEvidencePort(kind)
    coordinator.port = port
    request_id = f"{plan.plan_id}:service-000:service-000-replica-0:replace:1"
    _submit(coordinator, plan, authority, review_ref)

    with pytest.raises(FleetReplacementError, match="exact target/release health provenance"):
        coordinator.advance(
            plan.plan_id,
            now=datetime(2026, 8, 23, 12, tzinfo=UTC),
        )
    assert coordinator.get(plan.plan_id).replacements[0].state is (
        ReplicaReplacementState.RECONCILE_REQUIRED
    )
    assert port.calls.count(("apply", request_id)) == 1

    with pytest.raises(FleetReplacementError, match="exact target/release health provenance"):
        coordinator.advance(
            plan.plan_id,
            now=datetime(2026, 8, 23, 12, 1, tzinfo=UTC),
        )
    assert port.calls.count(("apply", request_id)) == 1
    assert port.calls.count(("inspect", request_id)) == 1


def test_recomputed_durable_environment_tamper_cannot_bootstrap_recovery_authority() -> None:
    coordinator, fleet, operations, nodes, port, placements = _fixture(service_count=1)
    key = ("service-000", "service-000-replica-0")
    plan, authority, review_ref = _authorized_plan(placements, (key,))
    request_id = f"{plan.plan_id}:service-000:service-000-replica-0:replace:1"
    _submit(coordinator, plan, authority, review_ref)
    stale_snapshot = coordinator.snapshot()
    port.modes[request_id] = "hard-crash"

    with pytest.raises(SystemExit, match="simulated process death"):
        coordinator.advance(
            plan.plan_id,
            now=datetime(2026, 8, 23, 12, tzinfo=UTC),
        )

    journal = coordinator.dispatch_journal
    assert journal is not None
    records = journal.records  # type: ignore[attr-defined]
    journal_key = next(iter(records))
    durable = records[journal_key]
    forged_request = replace(durable.request, environment_id="prod-forged")
    records[journal_key] = DurableReplacementDispatch(
        request=forged_request,
        attempt=durable.attempt,
        source_was_enabled=durable.source_was_enabled,
        request_checksum_sha256=fleet_replacement_request_fingerprint(forged_request),
    )

    restarted = FleetReplacementCoordinator(fleet, operations, nodes, port, journal)
    calls_before = tuple(port.calls)
    with pytest.raises(FleetReplacementError, match="exact trusted fleet identity"):
        restarted.restore(stale_snapshot, review_authorities=((authority, review_ref),))
    assert tuple(port.calls) == calls_before
