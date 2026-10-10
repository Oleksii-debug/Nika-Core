from __future__ import annotations

from datetime import UTC, datetime

from pf3_fleet_replacement_support import _authorized_plan, _fixture, _submit

from nika_core.product_factory_fleet_replacement import (
    FleetReplacementState,
    ReplicaReplacementState,
)


def test_rejected_replica_is_isolated_and_unrelated_replacement_continues() -> None:
    coordinator, _, _, nodes, port, placements = _fixture(service_count=2)
    first = ("service-000", "service-000-replica-0")
    second = ("service-001", "service-001-replica-0")
    plan, authority, review_ref = _authorized_plan(placements, (first, second))
    first_request_id = f"{plan.plan_id}:service-000:service-000-replica-0:replace:1"
    second_request_id = f"{plan.plan_id}:service-001:service-001-replica-0:replace:1"
    port.modes[first_request_id] = "reject"
    _submit(coordinator, plan, authority, review_ref)

    after_reject = coordinator.advance(
        plan.plan_id,
        now=datetime(2026, 8, 23, 12, tzinfo=UTC),
    )
    assert after_reject.state is FleetReplacementState.IN_PROGRESS
    assert after_reject.replacements[0].state is ReplicaReplacementState.FAILED
    assert after_reject.replacements[1].state is ReplicaReplacementState.PENDING

    final = coordinator.advance(
        plan.plan_id,
        now=datetime(2026, 8, 23, 12, 0, 1, tzinfo=UTC),
    )
    assert final.state is FleetReplacementState.PARTIAL_FAILURE
    assert final.replacements[0].state is ReplicaReplacementState.FAILED
    assert final.replacements[1].state is ReplicaReplacementState.SUCCEEDED
    assert port.calls.count(("apply", first_request_id)) == 1
    assert port.calls.count(("apply", second_request_id)) == 1
    assert nodes.snapshot().leases == ()
