from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from pf3_fleet_replacement_support import (
    _authorized_plan,
    _fixture,
    _sha,
    _submit,
)

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_factory_fleet_replacement import (
    FleetReplacementCoordinator,
    FleetReplacementError,
    FleetReplacementState,
    ReplicaReplacementState,
)
from nika_core.product_factory_fleet_replacement_durability import (
    SQLiteFleetReplacementDispatchJournal,
)


def test_restart_requires_external_review_authority() -> None:
    coordinator, fleet, operations, nodes, port, placements = _fixture(service_count=1)
    key = ("service-000", "service-000-replica-0")
    plan, authority, review_ref = _authorized_plan(placements, (key,))
    _submit(coordinator, plan, authority, review_ref)
    snapshot = coordinator.snapshot()
    journal = coordinator.dispatch_journal

    restarted = FleetReplacementCoordinator(fleet, operations, nodes, port, journal)
    with pytest.raises(FleetReplacementError, match="independent trusted review authority"):
        restarted.restore(snapshot, review_authorities=())
    assert port.calls == []

    restarted.restore(snapshot, review_authorities=((authority, review_ref),))
    result = restarted.advance(
        plan.plan_id,
        now=datetime(2026, 8, 23, 12, tzinfo=UTC),
    )
    assert result.state is FleetReplacementState.SUCCEEDED


def test_orphan_work_lease_after_restart_waits_until_expiry_then_reacquires() -> None:
    coordinator, fleet, operations, nodes, port, placements = _fixture(service_count=1)
    key = ("service-000", "service-000-replica-0")
    plan, authority, review_ref = _authorized_plan(placements, (key,))
    _submit(coordinator, plan, authority, review_ref)
    snapshot = coordinator.snapshot()
    spec = plan.replacements[0]

    orphan = nodes.acquire(
        spec.request,
        now=datetime(2026, 8, 23, 12, tzinfo=UTC),
        lease_seconds=30,
    )
    restarted = FleetReplacementCoordinator(
        fleet,
        operations,
        nodes,
        port,
        coordinator.dispatch_journal,
    )
    restarted.restore(snapshot, review_authorities=((authority, review_ref),))
    blocked = restarted.advance(
        plan.plan_id,
        now=datetime(2026, 8, 23, 12, 0, 10, tzinfo=UTC),
    )
    assert blocked.replacements[0].state is ReplicaReplacementState.WAITING_FOR_ORPHAN_LEASE
    assert port.calls == []
    assert any(lease.lease_id == orphan.lease_id for lease in nodes.snapshot().leases)

    finished = restarted.advance(
        plan.plan_id,
        now=datetime(2026, 8, 23, 12, 0, 31, tzinfo=UTC),
    )
    assert finished.state is FleetReplacementState.SUCCEEDED
    assert nodes.snapshot().leases == ()


def test_uncertain_result_uses_inspect_and_never_blind_replays_apply() -> None:
    coordinator, _, _, _, port, placements = _fixture(service_count=1)
    key = ("service-000", "service-000-replica-0")
    plan, authority, review_ref = _authorized_plan(placements, (key,))
    request_id = f"{plan.plan_id}:service-000:service-000-replica-0:replace:1"
    port.modes[request_id] = "uncertain"
    _submit(coordinator, plan, authority, review_ref)

    uncertain = coordinator.advance(
        plan.plan_id,
        now=datetime(2026, 8, 23, 12, tzinfo=UTC),
    )
    assert uncertain.state is FleetReplacementState.RECONCILE_REQUIRED

    reconciled = coordinator.advance(
        plan.plan_id,
        now=datetime(2026, 8, 23, 12, 1, tzinfo=UTC),
    )
    assert reconciled.state is FleetReplacementState.SUCCEEDED
    assert port.calls.count(("apply", request_id)) == 1
    assert port.calls.count(("inspect", request_id)) == 1


def test_provider_exception_persists_exact_request_and_restart_inspects_only() -> None:
    coordinator, fleet, operations, nodes, port, placements = _fixture(service_count=1)
    key = ("service-000", "service-000-replica-0")
    plan, authority, review_ref = _authorized_plan(placements, (key,))
    request_id = f"{plan.plan_id}:service-000:service-000-replica-0:replace:1"
    port.modes[request_id] = "raise"
    _submit(coordinator, plan, authority, review_ref)

    with pytest.raises(FleetReplacementError, match="inspection is required"):
        coordinator.advance(
            plan.plan_id,
            now=datetime(2026, 8, 23, 12, tzinfo=UTC),
        )
    snapshot = coordinator.snapshot()
    assert snapshot.records[0][1][0].state is ReplicaReplacementState.RECONCILE_REQUIRED

    port.modes[request_id] = "success"
    restarted = FleetReplacementCoordinator(
        fleet,
        operations,
        nodes,
        port,
        coordinator.dispatch_journal,
    )
    restarted.restore(snapshot, review_authorities=((authority, review_ref),))
    finished = restarted.advance(
        plan.plan_id,
        now=datetime(2026, 8, 23, 12, 1, tzinfo=UTC),
    )
    assert finished.state is FleetReplacementState.SUCCEEDED
    assert port.calls.count(("apply", request_id)) == 1
    assert port.calls.count(("inspect", request_id)) == 1


def test_process_death_after_effect_recovers_from_sqlite_and_inspects_only(tmp_path) -> None:
    path = tmp_path / "fleet-replacement.db"
    store = SQLiteStore(path)
    journal = SQLiteFleetReplacementDispatchJournal(store)
    coordinator, fleet, operations, nodes, port, placements = _fixture(
        service_count=1,
        dispatch_journal=journal,
    )
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
    assert port.calls.count(("apply", request_id)) == 1

    port.modes[request_id] = "success"
    reopened_journal = SQLiteFleetReplacementDispatchJournal(SQLiteStore(path))
    restarted = FleetReplacementCoordinator(
        fleet,
        operations,
        nodes,
        port,
        reopened_journal,
    )
    restarted.restore(stale_snapshot, review_authorities=((authority, review_ref),))
    pending = restarted.get(plan.plan_id).replacements[0]
    assert pending.state is ReplicaReplacementState.RECONCILE_REQUIRED
    assert pending.pending_request is not None
    assert pending.pending_request.request_id == request_id

    finished = restarted.advance(
        plan.plan_id,
        now=datetime(2026, 8, 23, 12, 1, tzinfo=UTC),
    )
    assert finished.state is FleetReplacementState.SUCCEEDED
    assert port.calls.count(("apply", request_id)) == 1
    assert port.calls.count(("inspect", request_id)) == 1


def test_terminal_provider_evidence_recovers_stale_snapshot_without_reapply(tmp_path) -> None:
    path = tmp_path / "fleet-terminal.db"
    journal = SQLiteFleetReplacementDispatchJournal(SQLiteStore(path))
    coordinator, fleet, operations, nodes, port, placements = _fixture(
        service_count=1,
        dispatch_journal=journal,
    )
    key = ("service-000", "service-000-replica-0")
    plan, authority, review_ref = _authorized_plan(placements, (key,))
    request_id = f"{plan.plan_id}:service-000:service-000-replica-0:replace:1"
    _submit(coordinator, plan, authority, review_ref)
    stale_snapshot = coordinator.snapshot()

    assert coordinator.advance(
        plan.plan_id,
        now=datetime(2026, 8, 23, 12, tzinfo=UTC),
    ).state is FleetReplacementState.SUCCEEDED
    assert port.calls.count(("apply", request_id)) == 1

    restarted = FleetReplacementCoordinator(
        fleet,
        operations,
        nodes,
        port,
        SQLiteFleetReplacementDispatchJournal(SQLiteStore(path)),
    )
    restarted.restore(stale_snapshot, review_authorities=((authority, review_ref),))
    recovered = restarted.get(plan.plan_id)
    assert recovered.state is FleetReplacementState.SUCCEEDED
    assert port.calls.count(("apply", request_id)) == 1
    assert port.calls.count(("inspect", request_id)) == 0


def test_corrupt_durable_dispatch_fails_closed_before_provider_replay(tmp_path) -> None:
    path = tmp_path / "fleet-corrupt.db"
    store = SQLiteStore(path)
    journal = SQLiteFleetReplacementDispatchJournal(store)
    coordinator, fleet, operations, nodes, port, placements = _fixture(
        service_count=1,
        dispatch_journal=journal,
    )
    key = ("service-000", "service-000-replica-0")
    plan, authority, review_ref = _authorized_plan(placements, (key,))
    request_id = f"{plan.plan_id}:service-000:service-000-replica-0:replace:1"
    _submit(coordinator, plan, authority, review_ref)
    stale_snapshot = coordinator.snapshot()
    port.modes[request_id] = "hard-crash"
    with pytest.raises(SystemExit):
        coordinator.advance(
            plan.plan_id,
            now=datetime(2026, 8, 23, 12, tzinfo=UTC),
        )

    with store.connection() as conn:
        conn.execute(
            "UPDATE fleet_replacement_dispatches SET request_json = ? WHERE request_id = ?",
            ("{}", request_id),
        )
    calls_before = tuple(port.calls)
    restarted = FleetReplacementCoordinator(
        fleet,
        operations,
        nodes,
        port,
        SQLiteFleetReplacementDispatchJournal(SQLiteStore(path)),
    )
    with pytest.raises(FleetReplacementError, match="payload shape|checksum|corrupt"):
        restarted.restore(stale_snapshot, review_authorities=((authority, review_ref),))
    assert tuple(port.calls) == calls_before


def test_wrong_provider_success_becomes_reconcile_required_not_blind_retry() -> None:
    coordinator, _, _, _, port, placements = _fixture(service_count=1)
    key = ("service-000", "service-000-replica-0")
    plan, authority, review_ref = _authorized_plan(placements, (key,))
    request_id = f"{plan.plan_id}:service-000:service-000-replica-0:replace:1"
    port.modes[request_id] = "wrong-release"
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


def test_wrong_release_version_success_becomes_reconcile_required() -> None:
    coordinator, _, _, _, port, placements = _fixture(service_count=1)
    key = ("service-000", "service-000-replica-0")
    plan, authority, review_ref = _authorized_plan(placements, (key,))
    request_id = f"{plan.plan_id}:service-000:service-000-replica-0:replace:1"
    port.modes[request_id] = "wrong-version"
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


def test_snapshot_restore_rejects_release_provenance_drift() -> None:
    coordinator, fleet, operations, nodes, port, placements = _fixture(service_count=1)
    key = ("service-000", "service-000-replica-0")
    plan, authority, review_ref = _authorized_plan(placements, (key,))
    _submit(coordinator, plan, authority, review_ref)
    snapshot = coordinator.snapshot()

    service = fleet.record.services[0]
    fleet.record = replace(
        fleet.record,
        services=(replace(service, release_sha=_sha(123_456)),),
    )
    restarted = FleetReplacementCoordinator(
        fleet,
        operations,
        nodes,
        port,
        coordinator.dispatch_journal,
    )
    with pytest.raises(FleetReplacementError, match="provenance|release/environment"):
        restarted.restore(snapshot, review_authorities=((authority, review_ref),))


def test_snapshot_restore_rejects_release_version_drift() -> None:
    coordinator, fleet, operations, nodes, port, placements = _fixture(service_count=1)
    key = ("service-000", "service-000-replica-0")
    plan, authority, review_ref = _authorized_plan(placements, (key,))
    _submit(coordinator, plan, authority, review_ref)
    snapshot = coordinator.snapshot()

    service_spec = fleet.record.plan.services[0]
    changed_replicas = tuple(
        replace(
            replica,
            intent=replace(
                replica.intent,
                release=replace(replica.intent.release, version="2.0.0"),
            ),
        )
        for replica in service_spec.replicas
    )
    changed_service_spec = replace(service_spec, replicas=changed_replicas)
    fleet.record = replace(
        fleet.record,
        plan=replace(fleet.record.plan, services=(changed_service_spec,)),
    )
    restarted = FleetReplacementCoordinator(
        fleet,
        operations,
        nodes,
        port,
        coordinator.dispatch_journal,
    )
    with pytest.raises(FleetReplacementError, match="provenance"):
        restarted.restore(snapshot, review_authorities=((authority, review_ref),))


def test_scale_60_services_180_replacements_two_environments_restart_and_node_loss() -> None:
    coordinator, fleet, operations, nodes, port, placements = _fixture(
        service_count=60,
        replica_count=3,
        node_count=12,
    )
    keys = tuple(sorted(placements))
    plan, authority, review_ref = _authorized_plan(
        placements,
        keys,
        max_unavailable=80,
        max_concurrent=4,
        max_replicas_per_node=40,
    )
    _submit(coordinator, plan, authority, review_ref)

    for _ in range(45):
        coordinator.advance(
            plan.plan_id,
            now=datetime(2026, 8, 23, 12, tzinfo=UTC),
        )
    operations.record_node_availability("node-8", available=False)
    operations.record_node_availability("node-9", available=False)
    snapshot = coordinator.snapshot()

    restarted = FleetReplacementCoordinator(
        fleet,
        operations,
        nodes,
        port,
        coordinator.dispatch_journal,
    )
    restarted.restore(snapshot, review_authorities=((authority, review_ref),))
    for step in range(300):
        record = restarted.get(plan.plan_id)
        if record.state in {
            FleetReplacementState.SUCCEEDED,
            FleetReplacementState.PARTIAL_FAILURE,
        }:
            break
        restarted.advance(
            plan.plan_id,
            now=datetime(2026, 8, 23, 12, tzinfo=UTC) + timedelta(seconds=step + 1),
        )

    final = restarted.get(plan.plan_id)
    assert final.state is FleetReplacementState.SUCCEEDED
    assert len(final.replacements) == 180
    assert all(item.state is ReplicaReplacementState.SUCCEEDED for item in final.replacements)
    assert len([call for call in port.calls if call[0] == "apply"]) == 180
    assert nodes.snapshot().leases == ()
