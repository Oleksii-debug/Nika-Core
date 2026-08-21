from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from pf3_fleet_replacement_support import _fixture, _plan, _sha

from nika_core.product_factory_fleet_replacement import (
    FleetReplacementCoordinator,
    FleetReplacementError,
    FleetReplacementState,
    ReplicaReplacementState,
)


def test_orphan_work_lease_after_restart_waits_until_expiry_then_reacquires() -> None:
    coordinator, fleet, operations, nodes, port, placements = _fixture(service_count=1)
    key = ("service-000", "service-000-replica-0")
    plan = _plan(placements, (key,))
    coordinator.submit(plan)
    snapshot = coordinator.snapshot()
    spec = plan.replacements[0]

    orphan = nodes.acquire(
        spec.request,
        now=datetime(2026, 8, 21, 12, tzinfo=UTC),
        lease_seconds=30,
    )
    restarted = FleetReplacementCoordinator(fleet, operations, nodes, port)
    restarted.restore(snapshot)
    blocked = restarted.advance(
        "replacement-001",
        now=datetime(2026, 8, 21, 12, 0, 10, tzinfo=UTC),
    )
    assert blocked.replacements[0].state is ReplicaReplacementState.WAITING_FOR_ORPHAN_LEASE
    assert port.calls == []
    assert any(lease.lease_id == orphan.lease_id for lease in nodes.snapshot().leases)

    finished = restarted.advance(
        "replacement-001",
        now=datetime(2026, 8, 21, 12, 0, 31, tzinfo=UTC),
    )
    assert finished.state is FleetReplacementState.SUCCEEDED
    assert nodes.snapshot().leases == ()


def test_uncertain_result_uses_inspect_and_never_blind_replays_apply() -> None:
    coordinator, _, _, _, port, placements = _fixture(service_count=1)
    key = ("service-000", "service-000-replica-0")
    request_id = "replacement-001:service-000:service-000-replica-0:replace:1"
    port.modes[request_id] = "uncertain"
    coordinator.submit(_plan(placements, (key,)))

    uncertain = coordinator.advance("replacement-001", now=datetime(2026, 8, 21, 12, tzinfo=UTC))
    assert uncertain.state is FleetReplacementState.RECONCILE_REQUIRED
    assert uncertain.replacements[0].state is ReplicaReplacementState.RECONCILE_REQUIRED

    reconciled = coordinator.advance(
        "replacement-001",
        now=datetime(2026, 8, 21, 12, 1, tzinfo=UTC),
    )
    assert reconciled.state is FleetReplacementState.SUCCEEDED
    assert port.calls.count(("apply", request_id)) == 1
    assert port.calls.count(("inspect", request_id)) == 1


def test_provider_exception_persists_exact_request_and_restart_inspects_only() -> None:
    coordinator, fleet, operations, nodes, port, placements = _fixture(service_count=1)
    key = ("service-000", "service-000-replica-0")
    request_id = "replacement-001:service-000:service-000-replica-0:replace:1"
    port.modes[request_id] = "raise"
    coordinator.submit(_plan(placements, (key,)))

    with pytest.raises(FleetReplacementError, match="inspection is required"):
        coordinator.advance("replacement-001", now=datetime(2026, 8, 21, 12, tzinfo=UTC))
    snapshot = coordinator.snapshot()
    assert snapshot.records[0][1][0].state is ReplicaReplacementState.RECONCILE_REQUIRED

    port.modes[request_id] = "success"
    restarted = FleetReplacementCoordinator(fleet, operations, nodes, port)
    restarted.restore(snapshot)
    finished = restarted.advance(
        "replacement-001",
        now=datetime(2026, 8, 21, 12, 1, tzinfo=UTC),
    )
    assert finished.state is FleetReplacementState.SUCCEEDED
    assert port.calls.count(("apply", request_id)) == 1
    assert port.calls.count(("inspect", request_id)) == 1


def test_provider_success_requires_exact_target_release_digest_and_health() -> None:
    coordinator, _, _, _, port, placements = _fixture(service_count=1)
    key = ("service-000", "service-000-replica-0")
    request_id = "replacement-001:service-000:service-000-replica-0:replace:1"
    port.modes[request_id] = "wrong-release"
    coordinator.submit(_plan(placements, (key,)))

    with pytest.raises(FleetReplacementError, match="exact target/release health provenance"):
        coordinator.advance("replacement-001", now=datetime(2026, 8, 21, 12, tzinfo=UTC))
    assert coordinator.get("replacement-001").replacements[0].state is (
        ReplicaReplacementState.DISPATCHING
    )


def test_failed_replica_does_not_block_unrelated_service_replacement() -> None:
    coordinator, _, _, _, port, placements = _fixture(service_count=2)
    keys = (
        ("service-000", "service-000-replica-0"),
        ("service-001", "service-001-replica-0"),
    )
    first_request = "replacement-001:service-000:service-000-replica-0:replace:1"
    port.modes[first_request] = "reject"
    coordinator.submit(_plan(placements, keys))

    first = coordinator.advance("replacement-001", now=datetime(2026, 8, 21, 12, tzinfo=UTC))
    assert first.replacements[0].state is ReplicaReplacementState.FAILED
    second = coordinator.advance(
        "replacement-001",
        now=datetime(2026, 8, 21, 12, 1, tzinfo=UTC),
    )
    assert second.replacements[1].state is ReplicaReplacementState.SUCCEEDED
    assert second.state is FleetReplacementState.PARTIAL_FAILURE


def test_snapshot_restore_rejects_release_or_source_topology_drift() -> None:
    coordinator, fleet, operations, nodes, port, placements = _fixture(service_count=1)
    key = ("service-000", "service-000-replica-0")
    coordinator.submit(_plan(placements, (key,)))
    snapshot = coordinator.snapshot()

    service = fleet.record.services[0]
    fleet.record = replace(
        fleet.record,
        services=(replace(service, release_sha=_sha(123_456)),),
    )
    restarted = FleetReplacementCoordinator(fleet, operations, nodes, port)
    with pytest.raises(FleetReplacementError, match="provenance"):
        restarted.restore(snapshot)


def test_scale_60_services_180_replacements_two_environments_restart_and_node_loss() -> None:
    coordinator, fleet, operations, nodes, port, placements = _fixture(
        service_count=60,
        replica_count=3,
        node_count=12,
    )
    keys = tuple(sorted(placements))
    plan = _plan(
        placements,
        keys,
        max_unavailable=80,
        max_concurrent=4,
        max_replicas_per_node=40,
    )
    coordinator.submit(plan)

    for _ in range(45):
        coordinator.advance(
            "replacement-001",
            now=datetime(2026, 8, 21, 12, tzinfo=UTC),
        )
    operations.record_node_availability("node-8", available=False)
    operations.record_node_availability("node-9", available=False)
    snapshot = coordinator.snapshot()

    restarted = FleetReplacementCoordinator(fleet, operations, nodes, port)
    restarted.restore(snapshot)
    for step in range(300):
        record = restarted.get("replacement-001")
        if record.state in {
            FleetReplacementState.SUCCEEDED,
            FleetReplacementState.PARTIAL_FAILURE,
        }:
            break
        restarted.advance(
            "replacement-001",
            now=datetime(2026, 8, 21, 12, tzinfo=UTC) + timedelta(seconds=step + 1),
        )

    final = restarted.get("replacement-001")
    assert final.state is FleetReplacementState.SUCCEEDED
    assert len(final.replacements) == 180
    assert all(item.state is ReplicaReplacementState.SUCCEEDED for item in final.replacements)
    assert len([call for call in port.calls if call[0] == "apply"]) == 180
    assert nodes.snapshot().leases == ()
