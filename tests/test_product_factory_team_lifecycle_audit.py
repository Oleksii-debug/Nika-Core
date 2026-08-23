from __future__ import annotations

from nika_core.product_factory_orchestration import (
    ComponentBrief,
    ProjectScale,
    TeamCompositionRequest,
)
from nika_core.product_factory_team_lifecycle import (
    DynamicTeamLifecycle,
    RoleAssignmentStatus,
    TeamLifecycleSnapshot,
)


def _request() -> TeamCompositionRequest:
    return TeamCompositionRequest(
        project_id="project:team-lifecycle-audit",
        components=(ComponentBrief(component_id="backend", kind="backend"),),
        acceptance_criteria=("Deterministic recovery evidence",),
        permission_ceiling=frozenset({"read_source", "write_source", "run_tests"}),
        scale=ProjectScale.MEDIUM,
        evidence_refs=("requirements:audit:v1",),
    )


def test_replacement_keeps_unavailable_and_replacement_evidence_separate() -> None:
    lifecycle = DynamicTeamLifecycle()
    snapshot = lifecycle.compose(_request())
    original = next(
        assignment
        for assignment in snapshot.current_assignments
        if assignment.role.capabilities == ("implementation",)
    )

    unavailable = lifecycle.mark_unavailable(
        snapshot,
        role_id=original.role.role_id,
        status=RoleAssignmentStatus.FAILED,
        reason="worker exited before producing trusted output",
        evidence_refs=("worker:event:crash", "log:worker:17"),
    )
    replaced = lifecycle.replace_unavailable(
        unavailable,
        role_id=original.role.role_id,
        reason="replacement approved after failure review",
        evidence_refs=("decision:replacement:17",),
    )

    historical = next(
        assignment
        for assignment in replaced.assignments
        if assignment.assignment_id == original.assignment_id
    )
    current = next(
        assignment
        for assignment in replaced.current_assignments
        if assignment.role.role_id == original.role.role_id
    )

    assert historical.status is RoleAssignmentStatus.REPLACED
    assert historical.transition_reason == (
        "failed: worker exited before producing trusted output"
    )
    assert historical.evidence_refs == ("worker:event:crash", "log:worker:17")
    assert current.status is RoleAssignmentStatus.ACTIVE
    assert current.transition_reason == "replacement approved after failure review"
    assert current.evidence_refs == ("decision:replacement:17",)
    assert current.replaces_assignment_id == historical.assignment_id


def test_restart_round_trip_preserves_both_sides_of_replacement_audit_trail() -> None:
    lifecycle = DynamicTeamLifecycle()
    snapshot = lifecycle.compose(_request())
    original = next(
        assignment
        for assignment in snapshot.current_assignments
        if assignment.role.capabilities == ("implementation",)
    )
    snapshot = lifecycle.mark_unavailable(
        snapshot,
        role_id=original.role.role_id,
        status=RoleAssignmentStatus.BLOCKED,
        reason="required dependency unavailable",
        evidence_refs=("dependency:blocked:42",),
    )
    snapshot = lifecycle.replace_unavailable(
        snapshot,
        role_id=original.role.role_id,
        reason="approved replacement can continue independent work",
        evidence_refs=("decision:replacement:42",),
    )

    recovered = TeamLifecycleSnapshot.from_json(snapshot.to_json())
    historical = next(
        assignment
        for assignment in recovered.assignments
        if assignment.assignment_id == original.assignment_id
    )
    current = next(
        assignment
        for assignment in recovered.current_assignments
        if assignment.role.role_id == original.role.role_id
    )

    assert historical.transition_reason == "blocked: required dependency unavailable"
    assert historical.evidence_refs == ("dependency:blocked:42",)
    assert current.transition_reason == (
        "approved replacement can continue independent work"
    )
    assert current.evidence_refs == ("decision:replacement:42",)
