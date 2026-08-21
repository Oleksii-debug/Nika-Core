from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from nika_core.product_factory_deployment import (
    ExecutionRequest,
    NodeCapabilities,
    Platform,
    ResourceEnvelope,
)
from nika_core.product_factory_fleet_replacement import (
    EnvironmentReplacementBudget,
    FleetReplacementPlan,
    FleetReplacementState,
    ReplicaReplacementState,
)

from pf3_fleet_replacement_support import (
    _fixture,
    _node,
    _plan,
    _replacement_spec,
)


def test_replacement_moves_replica_to_another_node_and_restores_source_cordon() -> None:
    coordinator, _, _, nodes, port, placements = _fixture(service_count=2)
    key = ("service-000", "service-000-replica-0")
    plan = _plan(placements, (key,))
    coordinator.submit(plan)

    result = coordinator.advance("replacement-001", now=datetime(2026, 8, 21, 12, tzinfo=UTC))
    record = result.replacements[0]
    assert result.state is FleetReplacementState.SUCCEEDED
    assert record.state is ReplicaReplacementState.SUCCEEDED
    assert record.target_node_id != placements[key][1]
    assert record.pending_request is None
    assert port.calls == [("apply", "replacement-001:service-000:service-000-replica-0:replace:1")]
    assert next(
        node for node in nodes.snapshot().nodes if node.identity.node_id == placements[key][1]
    ).enabled
    assert nodes.snapshot().leases == ()


def test_retain_cordoned_node_keeps_source_disabled_after_success() -> None:
    coordinator, _, _, nodes, _, placements = _fixture(service_count=1)
    key = ("service-000", "service-000-replica-0")
    source = placements[key][1]
    coordinator.submit(_plan(placements, (key,), retain_cordoned_nodes=(source,)))

    coordinator.advance("replacement-001", now=datetime(2026, 8, 21, 12, tzinfo=UTC))
    assert not next(
        node for node in nodes.snapshot().nodes if node.identity.node_id == source
    ).enabled


def test_heterogeneous_target_matching_skips_nodes_without_required_resources() -> None:
    coordinator, _, _, nodes, _, placements = _fixture(service_count=1, node_count=4)
    nodes.register(replace(_node(1), resources=ResourceEnvelope(8, 256, 100_000)))
    nodes.register(
        replace(
            _node(2),
            capabilities=NodeCapabilities(
                frozenset({"deploy"}),
                frozenset({"ansible"}),
            ),
        )
    )
    key = ("service-000", "service-000-replica-0")
    operation_id, source = placements[key]
    spec = _replacement_spec(
        key[0],
        key[1],
        operation_id,
        source,
        memory_mb=1024,
    )
    plan = FleetReplacementPlan(
        "replacement-001",
        "project-social",
        "fleet-production",
        (spec,),
        (EnvironmentReplacementBudget("prod-eu", 2, 1),),
        100,
        "approval:maintenance-window-77",
        "heterogeneous replacement",
        ("change:replacement-001",),
    )
    coordinator.submit(plan)

    result = coordinator.advance("replacement-001", now=datetime(2026, 8, 21, 12, tzinfo=UTC))
    assert result.replacements[0].target_node_id == "node-3"


def test_target_selection_respects_node_placement_cap_and_service_anti_affinity() -> None:
    coordinator, _, _, _, _, placements = _fixture(
        service_count=2,
        replica_count=2,
        node_count=4,
    )
    key = ("service-000", "service-000-replica-0")
    coordinator.submit(_plan(placements, (key,), max_replicas_per_node=1))

    result = coordinator.advance(
        "replacement-001",
        now=datetime(2026, 8, 21, 12, tzinfo=UTC),
    )
    assert result.state is FleetReplacementState.SUCCEEDED
    assert result.replacements[0].target_node_id == "node-3"


def test_capacity_and_capability_drift_waits_without_provider_side_effect() -> None:
    coordinator, _, _, nodes, port, placements = _fixture(service_count=1, node_count=4)
    for index in (1, 2, 3):
        nodes.register(
            replace(
                _node(index),
                capabilities=NodeCapabilities(
                    frozenset({"deploy"}), frozenset({"ansible"})
                ),
            )
        )
    key = ("service-000", "service-000-replica-0")
    coordinator.submit(_plan(placements, (key,)))

    blocked = coordinator.advance("replacement-001", now=datetime(2026, 8, 21, 12, tzinfo=UTC))
    assert blocked.state is FleetReplacementState.BLOCKED
    assert blocked.replacements[0].state is ReplicaReplacementState.WAITING_FOR_CAPACITY
    assert port.calls == []

    nodes.register(_node(3))
    finished = coordinator.advance(
        "replacement-001",
        now=datetime(2026, 8, 21, 12, 1, tzinfo=UTC),
    )
    assert finished.state is FleetReplacementState.SUCCEEDED


def test_environment_budget_blocks_new_healthy_source_disruption() -> None:
    coordinator, _, operations, _, port, placements = _fixture(service_count=2)
    operations.record_node_availability("node-2", available=False)
    key = ("service-000", "service-000-replica-0")
    coordinator.submit(_plan(placements, (key,), max_unavailable=1))

    blocked = coordinator.advance("replacement-001", now=datetime(2026, 8, 21, 12, tzinfo=UTC))
    assert blocked.replacements[0].state is ReplicaReplacementState.BLOCKED_BUDGET
    assert port.calls == []


def test_replacement_from_already_lost_node_is_allowed_to_heal_budget_breach() -> None:
    coordinator, _, operations, _, _, placements = _fixture(service_count=2)
    key = ("service-000", "service-000-replica-0")
    source = placements[key][1]
    operations.record_node_availability(source, available=False)
    operations.record_node_availability("node-2", available=False)
    coordinator.submit(_plan(placements, (key,), max_unavailable=0))

    healed = coordinator.advance("replacement-001", now=datetime(2026, 8, 21, 12, tzinfo=UTC))
    assert healed.replacements[0].state is ReplicaReplacementState.SUCCEEDED


def test_credential_revocation_blocks_provider_until_restored() -> None:
    coordinator, _, operations, _, port, placements = _fixture(service_count=1)
    key = ("service-000", "service-000-replica-0")
    coordinator.submit(_plan(placements, (key,)))
    assert operations.revoke_credential("cred:deploy") == ("service-000",)

    blocked = coordinator.advance("replacement-001", now=datetime(2026, 8, 21, 12, tzinfo=UTC))
    assert blocked.replacements[0].state is ReplicaReplacementState.BLOCKED_CREDENTIAL
    assert port.calls == []

    operations.restore_credential("cred:deploy")
    finished = coordinator.advance(
        "replacement-001",
        now=datetime(2026, 8, 21, 12, 1, tzinfo=UTC),
    )
    assert finished.state is FleetReplacementState.SUCCEEDED


def test_active_source_work_lease_cordons_and_blocks_without_provider_call() -> None:
    coordinator, _, _, nodes, port, placements = _fixture(service_count=1)
    key = ("service-000", "service-000-replica-0")
    source = placements[key][1]
    lease = nodes.acquire(
        ExecutionRequest(
            "project-social",
            "other-active-work",
            Platform.LINUX,
            frozenset({"replacement"}),
            frozenset({"ansible"}),
            ResourceEnvelope(1, 128, 128),
        ),
        now=datetime(2026, 8, 21, 12, tzinfo=UTC),
    )
    assert lease.node_id == source
    coordinator.submit(_plan(placements, (key,)))

    blocked = coordinator.advance(
        "replacement-001",
        now=datetime(2026, 8, 21, 12, 0, 1, tzinfo=UTC),
    )
    assert blocked.replacements[0].state is ReplicaReplacementState.WAITING_FOR_SOURCE_LEASE
    assert port.calls == []
    assert not next(
        node for node in nodes.snapshot().nodes if node.identity.node_id == source
    ).enabled

    nodes.release(lease.lease_id)
    finished = coordinator.advance(
        "replacement-001",
        now=datetime(2026, 8, 21, 12, 0, 2, tzinfo=UTC),
    )
    assert finished.state is FleetReplacementState.SUCCEEDED
