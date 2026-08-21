from __future__ import annotations

import pytest

from nika_core.product_command.factory_snapshot_safety import (
    ProductFactorySnapshotIntegrityError,
    validate_rolling_maintenance_projection,
)
from nika_core.product_command.factory_status_adapter import rolling_maintenance_status_entries
from nika_core.product_factory_fleet_maintenance import (
    NodeMaintenanceAction,
    NodeMaintenanceRecord,
    NodeMaintenanceState,
    RollingMaintenancePlan,
    RollingMaintenanceSnapshot,
    ServiceMaintenanceBinding,
)

SHA = "a" * 40
DIGEST = "1" * 64
_ACTIONS = (
    NodeMaintenanceAction.DRAIN,
    NodeMaintenanceAction.RESTART,
    NodeMaintenanceAction.VERIFY,
    NodeMaintenanceAction.RESUME,
)


def _binding() -> ServiceMaintenanceBinding:
    return ServiceMaintenanceBinding(
        "service-api",
        "stage",
        SHA,
        DIGEST,
        ("replica-1",),
    )


def _plan() -> RollingMaintenancePlan:
    return RollingMaintenancePlan(
        "maintenance-plan-1",
        "project-1",
        "fleet-1",
        ("node-a",),
        "approval://project-1/maintenance-plan-1",
        "Patch execution node",
        ("maintenance://approved",),
    )


def _snapshot(record: NodeMaintenanceRecord) -> RollingMaintenanceSnapshot:
    plan = _plan()
    return RollingMaintenanceSnapshot(
        (plan,),
        ((plan.plan_id, (record,)),),
    )


def test_rolling_projection_rejects_succeeded_node_that_remains_cordoned() -> None:
    record = NodeMaintenanceRecord(
        "node-a",
        NodeMaintenanceState.SUCCEEDED,
        (_binding(),),
        completed_actions=_ACTIONS,
        cordoned=True,
    )

    with pytest.raises(
        ProductFactorySnapshotIntegrityError,
        match="cordon state disagrees",
    ):
        validate_rolling_maintenance_projection("project-1", _snapshot(record))


def test_rolling_projection_accepts_succeeded_node_after_resume_uncordons_it() -> None:
    record = NodeMaintenanceRecord(
        "node-a",
        NodeMaintenanceState.SUCCEEDED,
        (_binding(),),
        completed_actions=_ACTIONS,
        evidence_refs=("maintenance://resume-complete",),
        cordoned=False,
    )

    entries = rolling_maintenance_status_entries("project-1", _snapshot(record))

    assert entries[0].state == "succeeded"


def test_rolling_projection_rejects_verified_node_that_lost_cordon_before_resume() -> None:
    record = NodeMaintenanceRecord(
        "node-a",
        NodeMaintenanceState.VERIFIED,
        (_binding(),),
        completed_actions=_ACTIONS[:3],
        cordoned=False,
    )

    with pytest.raises(
        ProductFactorySnapshotIntegrityError,
        match="cordon state disagrees",
    ):
        validate_rolling_maintenance_projection("project-1", _snapshot(record))


def test_rolling_projection_rejects_pending_node_marked_cordoned() -> None:
    record = NodeMaintenanceRecord(
        "node-a",
        NodeMaintenanceState.PENDING,
        (_binding(),),
        cordoned=True,
    )

    with pytest.raises(
        ProductFactorySnapshotIntegrityError,
        match="cordon state disagrees",
    ):
        validate_rolling_maintenance_projection("project-1", _snapshot(record))


def test_pre_drain_credential_block_remains_valid_while_uncordoned() -> None:
    record = NodeMaintenanceRecord(
        "node-a",
        NodeMaintenanceState.BLOCKED_CREDENTIAL,
        (_binding(),),
        cordoned=False,
    )

    validate_rolling_maintenance_projection("project-1", _snapshot(record))


def test_zero_action_credential_block_may_preserve_prior_active_lease_cordon() -> None:
    record = NodeMaintenanceRecord(
        "node-a",
        NodeMaintenanceState.BLOCKED_CREDENTIAL,
        (_binding(),),
        cordoned=True,
    )

    validate_rolling_maintenance_projection("project-1", _snapshot(record))


def test_zero_action_quorum_block_may_be_uncordoned_or_preserve_prior_cordon() -> None:
    for cordoned in (False, True):
        record = NodeMaintenanceRecord(
            "node-a",
            NodeMaintenanceState.BLOCKED_QUORUM,
            (_binding(),),
            cordoned=cordoned,
        )
        validate_rolling_maintenance_projection("project-1", _snapshot(record))


def test_post_drain_credential_block_must_remain_cordoned() -> None:
    record = NodeMaintenanceRecord(
        "node-a",
        NodeMaintenanceState.BLOCKED_CREDENTIAL,
        (_binding(),),
        completed_actions=_ACTIONS[:1],
        cordoned=False,
    )

    with pytest.raises(
        ProductFactorySnapshotIntegrityError,
        match="cordon state disagrees",
    ):
        validate_rolling_maintenance_projection("project-1", _snapshot(record))


def test_quorum_block_cannot_claim_completed_drain_history() -> None:
    record = NodeMaintenanceRecord(
        "node-a",
        NodeMaintenanceState.BLOCKED_QUORUM,
        (_binding(),),
        completed_actions=_ACTIONS[:1],
        cordoned=True,
    )

    with pytest.raises(
        ProductFactorySnapshotIntegrityError,
        match="state disagrees with completed actions",
    ):
        validate_rolling_maintenance_projection("project-1", _snapshot(record))
