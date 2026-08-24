from __future__ import annotations

import json

import pytest

from nika_core import (
    product_factory_orchestration as orchestration,
)
from nika_core import (
    product_factory_team_lifecycle as team_lifecycle,
)

CEILING = frozenset({"read_source", "write_source", "run_tests"})


def _request() -> orchestration.TeamCompositionRequest:
    return orchestration.TeamCompositionRequest(
        project_id="project:oneshot02-team-identity",
        components=(
            orchestration.ComponentBrief(
                component_id="core",
                kind="backend",
                risk_tags=frozenset(),
            ),
        ),
        acceptance_criteria=("Deterministic restart identity",),
        permission_ceiling=CEILING,
        scale=orchestration.ProjectScale.SMALL,
        evidence_refs=("oneshot02:team-identity",),
    )


def test_restart_rejects_forged_generation_zero_assignment_identity() -> None:
    snapshot = team_lifecycle.DynamicTeamLifecycle().compose(_request())
    payload = json.loads(snapshot.to_json())
    payload["assignments"][0]["assignment_id"] = "forged:assignment:zero"

    with pytest.raises(team_lifecycle.TeamLifecycleError):
        team_lifecycle.TeamLifecycleSnapshot.from_json(json.dumps(payload))


def test_restart_rejects_consistently_rewritten_replacement_chain_identities() -> None:
    lifecycle = team_lifecycle.DynamicTeamLifecycle()
    snapshot = lifecycle.compose(_request())
    target = snapshot.current_assignments[0]
    snapshot = lifecycle.mark_unavailable(
        snapshot,
        role_id=target.role.role_id,
        status=team_lifecycle.RoleAssignmentStatus.FAILED,
        reason="adversarial restart fixture",
        evidence_refs=("oneshot02:failed",),
    )
    snapshot = lifecycle.replace_unavailable(
        snapshot,
        role_id=target.role.role_id,
        reason="adversarial replacement fixture",
        evidence_refs=("oneshot02:replace",),
    )

    payload = json.loads(snapshot.to_json())
    chain = [
        item
        for item in payload["assignments"]
        if item["role"]["role_id"] == target.role.role_id
    ]
    assert len(chain) == 2
    predecessor = next(item for item in chain if item["generation"] == 0)
    replacement = next(item for item in chain if item["generation"] == 1)

    predecessor["assignment_id"] = "forged:assignment:predecessor"
    replacement["assignment_id"] = "forged:assignment:replacement"
    replacement["replaces_assignment_id"] = predecessor["assignment_id"]

    with pytest.raises(team_lifecycle.TeamLifecycleError):
        team_lifecycle.TeamLifecycleSnapshot.from_json(json.dumps(payload))
