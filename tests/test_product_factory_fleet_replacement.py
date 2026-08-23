from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from nika_core.product_factory_deployment import (
    ExecutionRequest,
    NodeCapabilities,
    Platform,
    ResourceEnvelope,
)
from nika_core.product_factory_fleet_replacement import (
    FleetReplacementError,
    FleetReplacementState,
    ReplicaReplacementState,
    fleet_replacement_plan_fingerprint,
)
from pf3_fleet_replacement_support import (
    REVIEW_REF,
    _authorized_plan,
    _fixture,
    _node,
    _submit,
)


def test_external_trusted_review_authority_is_required_before_provider_side_effect() -> None:
    coordinator, _, _, _, port, placements = _fixture(service_count=1)
    key = ("service-000", "service-000-replica-0")
    plan, authority, _ = _authorized_plan(placements, (key,))

    with pytest.raises(FleetReplacementError, match="review ref"):
        coordinator.submit(
            plan,
            review_authority=authority,
            review_ref="review:caller-invented",
        )
    assert port.calls == []

    _submit(coordinator, plan, authority)
    result = coordinator.advance(
        plan.plan_id,
        now=datetime(2026, 8, 23, 12, tzinfo=UTC),
    )
    assert result.state is FleetReplacementState.SUCCEEDED
    request = port.requests[0]
    assert request.review_ref == REVIEW_REF
    assert request.plan_fingerprint == fleet_replacement_plan_fingerprint(plan)
    assert f"fleet-replacement-plan:{request.plan_fingerprint}" in request.evidence_refs


def test_caller_authored_approval_ref_cannot_reauthorize_mutated_plan() -> None:
    coordinator, _, _, _, port, placements = _fixture(service_count=1)
    key = ("service-000", "service-000-replica-0")
    plan, authority, review_ref = _authorized_plan(placements, (key,))
    mutated = replace(plan, reason="caller-mutated replacement scope")

    with pytest.raises(FleetReplacementError, match="fingerprint"):
        coordinator.submit(
            mutated,
            review_authority=authority,
            review_ref=review_ref,
        )
    assert port.calls == []


def test_replacement_moves_replica_and_restores_source_cordon() -> None:
    coordinator, _, _, nodes, port, placements = _fixture(service_count=2)
    key = ("service-000", "service-000-replica-0")
    plan, authority, review_ref = _authorized_plan(placements, (key,))
    _submit(coordinator, plan, authority, review_ref)

    result = coordinator.advance(
        plan.plan_id,
        now=datetime(2026, 8, 23, 12, tzinfo=UTC),
    )
    record = result.replacements[0]
    assert result.state is FleetReplacementState.SUCCEEDED
    assert record.state is ReplicaReplacementState.SUCCEEDED
    assert record.target_node_id != placements[key][1]
    assert record.pending_request is None
    assert len([call for call in port.calls if call[0] == "apply"]) == 1
    assert next(
        node
        for node in nodes.snapshot().nodes
        if node.identity.node_id == placements[key][1]
    ).enabled
    assert nodes.snapshot().leases == ()


def test_capacity_drift_waits_without_provider_side_effect_then_recovers() -> None:
    coordinator, _, _, nodes, port, placements = _fixture(service_count=1, node_count=4)
    for index in (1, 2, 3):
        nodes.register(
            replace(
                _node(index),
                capabilities=NodeCapabilities(
                    frozenset({"deploy"}),
                    frozenset({"ansible"}),
                ),
            )
        )
    key = ("service-000", "service-000-replica-0")
    plan, authority, review_ref = _authorized_plan(placements, (key,))
    _submit(coordinator, plan, authority, review_ref)

    blocked = coordinator.advance(
        plan.plan_id,
        now=datetime(2026, 8, 23, 12, tzinfo=UTC),
    )
    assert blocked.replacements[0].state is ReplicaReplacementState.WAITING_FOR_CAPACITY
    assert port.calls == []

    nodes.register(_node(3))
    finished = coordinator.advance(
        plan.plan_id,
        now=datetime(2026, 8, 23, 12, 1, tzinfo=UTC),
    )
    assert finished.state is FleetReplacementState.SUCCEEDED


def test_environment_budget_blocks_new_healthy_source_disruption() -> None:
    coordinator, _, operations, _, port, placements = _fixture(service_count=2)
    operations.record_node_availability("node-2", available=False)
    key = ("service-000", "service-000-replica-0")
    plan, authority, review_ref = _authorized_plan(
        placements,
        (key,),
        max_unavailable=1,
    )
    _submit(coordinator, plan, authority, review_ref)

    blocked = coordinator.advance(
        plan.plan_id,
        now=datetime(2026, 8, 23, 12, tzinfo=UTC),
    )
    assert blocked.replacements[0].state is ReplicaReplacementState.BLOCKED_BUDGET
    assert port.calls == []


def test_credential_revocation_blocks_provider_until_restored() -> None:
    coordinator, _, operations, _, port, placements = _fixture(service_count=1)
    key = ("service-000", "service-000-replica-0")
    plan, authority, review_ref = _authorized_plan(placements, (key,))
    _submit(coordinator, plan, authority, review_ref)
    assert operations.revoke_credential("cred:deploy") == ("service-000",)

    blocked = coordinator.advance(
        plan.plan_id,
        now=datetime(2026, 8, 23, 12, tzinfo=UTC),
    )
    assert blocked.replacements[0].state is ReplicaReplacementState.BLOCKED_CREDENTIAL
    assert port.calls == []

    operations.restore_credential("cred:deploy")
    finished = coordinator.advance(
        plan.plan_id,
        now=datetime(2026, 8, 23, 12, 1, tzinfo=UTC),
    )
    assert finished.state is FleetReplacementState.SUCCEEDED


def test_active_source_work_lease_blocks_without_provider_call() -> None:
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
        now=datetime(2026, 8, 23, 12, tzinfo=UTC),
    )
    assert lease.node_id == source
    plan, authority, review_ref = _authorized_plan(placements, (key,))
    _submit(coordinator, plan, authority, review_ref)

    blocked = coordinator.advance(
        plan.plan_id,
        now=datetime(2026, 8, 23, 12, 0, 1, tzinfo=UTC),
    )
    assert blocked.replacements[0].state is ReplicaReplacementState.WAITING_FOR_SOURCE_LEASE
    assert port.calls == []

    nodes.release(lease.lease_id)
    finished = coordinator.advance(
        plan.plan_id,
        now=datetime(2026, 8, 23, 12, 0, 2, tzinfo=UTC),
    )
    assert finished.state is FleetReplacementState.SUCCEEDED
