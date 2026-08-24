from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from pf3_fleet_replacement_support import _authorized_plan, _fixture, _submit

from nika_core.product_factory_fleet_replacement import FleetReplacementState


def _node_enabled(nodes, node_id: str) -> bool:
    matches = [
        node
        for node in nodes.snapshot().nodes
        if node.identity.node_id == node_id
    ]
    assert len(matches) == 1
    return matches[0].enabled


def test_source_cordon_is_released_when_no_other_disable_occurs() -> None:
    coordinator, _, _, nodes, _, placements = _fixture(
        service_count=1,
        replica_count=2,
        node_count=5,
    )
    key = ("service-000", "service-000-replica-0")
    source_node_id = placements[key][1]
    plan, authority, review_ref = _authorized_plan(placements, (key,))
    _submit(coordinator, plan, authority, review_ref)

    finished = coordinator.advance(
        plan.plan_id,
        now=datetime(2026, 8, 23, 12, tzinfo=UTC),
    )

    assert finished.state is FleetReplacementState.SUCCEEDED
    assert _node_enabled(nodes, source_node_id) is True


def test_source_cordon_does_not_erase_concurrent_external_disable(monkeypatch) -> None:
    coordinator, _, _, nodes, port, placements = _fixture(
        service_count=1,
        replica_count=2,
        node_count=5,
    )
    key = ("service-000", "service-000-replica-0")
    source_node_id = placements[key][1]
    plan, authority, review_ref = _authorized_plan(placements, (key,))
    original_apply = port.apply
    external_disable_seen = False

    def apply_after_external_disable(request):
        nonlocal external_disable_seen
        source = next(
            node
            for node in nodes.snapshot().nodes
            if node.identity.node_id == source_node_id
        )
        assert source.enabled is False
        nodes.register(replace(source, enabled=False))
        external_disable_seen = True
        return original_apply(request)

    monkeypatch.setattr(port, "apply", apply_after_external_disable)
    _submit(coordinator, plan, authority, review_ref)

    finished = coordinator.advance(
        plan.plan_id,
        now=datetime(2026, 8, 23, 12, tzinfo=UTC),
    )

    assert external_disable_seen is True
    assert finished.state is FleetReplacementState.SUCCEEDED
    assert _node_enabled(nodes, source_node_id) is False
