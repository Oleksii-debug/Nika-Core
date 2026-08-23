from __future__ import annotations

import json

import pytest

from nika_core.product_factory_orchestration import (
    ComponentBrief,
    ProjectScale,
    TeamCompositionRequest,
)
from nika_core.product_factory_team_lifecycle import (
    DynamicTeamLifecycle,
    RoleAssignmentStatus,
    TeamLifecycleError,
    TeamLifecycleSnapshot,
)

FULL_CEILING = frozenset(
    {"read_project", "update_project", "read_source", "write_source", "run_tests", "build_release"}
)
KINDS = ("backend", "web", "desktop", "data", "infra")


def _request(
    component_count: int,
    scale: ProjectScale,
    *,
    ceiling: frozenset[str] = FULL_CEILING,
    requested_specializations: tuple[str, ...] = (),
) -> TeamCompositionRequest:
    return TeamCompositionRequest(
        project_id="project:team-lifecycle",
        components=tuple(
            ComponentBrief(
                component_id=f"component-{index:03d}",
                kind=KINDS[index % len(KINDS)],
                risk_tags=(
                    frozenset({"accessibility"})
                    if index % 17 == 0
                    else frozenset({"security"})
                    if index % 19 == 0
                    else frozenset()
                ),
            )
            for index in range(component_count)
        ),
        acceptance_criteria=(
            "Every component has deterministic tests",
            "Accessible surfaces require independent review",
        ),
        permission_ceiling=ceiling,
        scale=scale,
        requested_specializations=requested_specializations,
        evidence_refs=("requirements:team-lifecycle:v1",),
    )


@pytest.mark.parametrize(
    ("component_count", "scale"),
    (
        (1, ProjectScale.SMALL),
        (5, ProjectScale.MEDIUM),
        (25, ProjectScale.LARGE),
        (100, ProjectScale.LARGE),
    ),
)
def test_lifecycle_scale_fixtures_are_deterministic_and_cover_every_component(
    component_count: int,
    scale: ProjectScale,
) -> None:
    lifecycle = DynamicTeamLifecycle()
    request = _request(component_count, scale)

    first = lifecycle.compose(request)
    second = lifecycle.compose(request)

    assert first == second
    assert first.to_json() == second.to_json()
    current = first.current_assignments
    assert current
    assert all(item.status is RoleAssignmentStatus.ACTIVE for item in current)
    assert all(item.role.permissions <= FULL_CEILING for item in current)

    semantic_keys = {
        (item.role.capabilities, item.role.component_ids, item.role.independent_review)
        for item in current
    }
    assert len(semantic_keys) == len(current)

    covered = {
        component_id
        for item in current
        for component_id in item.role.component_ids
    }
    assert covered == {f"component-{index:03d}" for index in range(component_count)}

    if scale is ProjectScale.SMALL:
        assert len(current) == 2
    if scale is ProjectScale.LARGE:
        implementation = [
            item for item in current if item.role.capabilities == ("implementation",)
        ]
        assert len(implementation) == component_count


def test_specialist_addition_is_idempotent_and_preserves_existing_assignments() -> None:
    lifecycle = DynamicTeamLifecycle()
    snapshot = lifecycle.compose(_request(5, ProjectScale.MEDIUM))
    original = snapshot.assignments

    expanded = lifecycle.add_specialist(
        snapshot,
        specialization="Localization",
        component_ids=("component-003", "component-001"),
        requested_permissions=frozenset(
            {"read_source", "write_source", "deploy_production"}
        ),
        reason="Ukrainian release needs dedicated localization ownership",
        evidence_refs=("decision:localization:v1",),
    )
    replayed = lifecycle.add_specialist(
        expanded,
        specialization="localization",
        component_ids=("component-001", "component-003"),
        requested_permissions=frozenset(
            {"read_source", "write_source", "deploy_production"}
        ),
        reason="Ukrainian release needs dedicated localization ownership",
        evidence_refs=("decision:localization:v1",),
    )

    assert expanded.assignments[: len(original)] == original
    assert replayed is expanded
    specialists = [
        item
        for item in replayed.current_assignments
        if item.role.capabilities == ("localization",)
    ]
    assert len(specialists) == 1
    specialist = specialists[0]
    assert specialist.role.component_ids == ("component-001", "component-003")
    assert specialist.role.permissions == frozenset({"read_source", "write_source"})
    assert "deploy_production" not in specialist.role.permissions


@pytest.mark.parametrize(
    "status",
    (RoleAssignmentStatus.BLOCKED, RoleAssignmentStatus.FAILED),
)
def test_unavailable_assignment_replacement_preserves_logical_role_and_ownership(
    status: RoleAssignmentStatus,
) -> None:
    lifecycle = DynamicTeamLifecycle()
    snapshot = lifecycle.compose(_request(5, ProjectScale.MEDIUM))
    original = next(
        item
        for item in snapshot.current_assignments
        if item.role.capabilities == ("implementation",)
    )

    unavailable = lifecycle.mark_unavailable(
        snapshot,
        role_id=original.role.role_id,
        status=status,
        reason="worker stopped before producing trusted evidence",
        evidence_refs=("worker:event:unavailable",),
    )
    replaced = lifecycle.replace_unavailable(
        unavailable,
        role_id=original.role.role_id,
        reason="replace unavailable worker without changing role authority",
        evidence_refs=("decision:replacement:v1",),
    )

    current = next(
        item
        for item in replaced.current_assignments
        if item.role.role_id == original.role.role_id
    )
    history = [
        item for item in replaced.assignments if item.role.role_id == original.role.role_id
    ]
    assert len(history) == 2
    assert history[0].status is RoleAssignmentStatus.REPLACED
    assert current.status is RoleAssignmentStatus.ACTIVE
    assert current.generation == 1
    assert current.assignment_id != original.assignment_id
    assert current.replaces_assignment_id == original.assignment_id
    assert current.role == original.role
    assert replaced.to_team_plan().roles.count(original.role) == 1


def test_active_assignment_cannot_be_replaced_without_unavailable_transition() -> None:
    lifecycle = DynamicTeamLifecycle()
    snapshot = lifecycle.compose(_request(1, ProjectScale.SMALL))
    role_id = snapshot.current_assignments[0].role.role_id

    with pytest.raises(TeamLifecycleError, match="blocked or failed"):
        lifecycle.replace_unavailable(
            snapshot,
            role_id=role_id,
            reason="replacement without lifecycle evidence is forbidden",
            evidence_refs=("decision:invalid-replacement",),
        )


def test_same_request_recomposition_is_a_true_noop() -> None:
    lifecycle = DynamicTeamLifecycle()
    request = _request(5, ProjectScale.MEDIUM)
    snapshot = lifecycle.compose(request)

    recomposed = lifecycle.recompose(snapshot, request)

    assert recomposed is snapshot
    assert recomposed.revision == 0


def test_recomposition_adds_specialization_without_corrupting_existing_ownership() -> None:
    lifecycle = DynamicTeamLifecycle()
    original = lifecycle.compose(_request(5, ProjectScale.MEDIUM))
    original_current = {
        (
            assignment.role.capabilities,
            assignment.role.component_ids,
            assignment.role.independent_review,
        ): assignment
        for assignment in original.current_assignments
    }

    evolved = lifecycle.recompose(
        original,
        _request(
            5,
            ProjectScale.MEDIUM,
            requested_specializations=("protocol-specialist",),
        ),
    )

    assert evolved.revision == 1
    protocol_roles = [
        item
        for item in evolved.current_assignments
        if item.role.capabilities == ("protocol-specialist",)
    ]
    assert len(protocol_roles) == 1
    for key, assignment in original_current.items():
        current = next(
            item
            for item in evolved.current_assignments
            if (
                item.role.capabilities,
                item.role.component_ids,
                item.role.independent_review,
            )
            == key
        )
        assert current.assignment_id == assignment.assignment_id
        assert current.role.role_id == assignment.role.role_id


def test_recomposition_can_narrow_but_not_silently_widen_permission_ceiling() -> None:
    lifecycle = DynamicTeamLifecycle()
    original = lifecycle.compose(
        _request(
            5,
            ProjectScale.MEDIUM,
            ceiling=frozenset({"read_source", "write_source", "run_tests"}),
        )
    )
    narrowed_ceiling = frozenset({"read_source", "run_tests"})

    narrowed = lifecycle.recompose(
        original,
        _request(5, ProjectScale.MEDIUM, ceiling=narrowed_ceiling),
    )

    assert narrowed.permission_ceiling == narrowed_ceiling
    assert all(
        assignment.role.permissions <= narrowed_ceiling
        for assignment in narrowed.current_assignments
    )

    with pytest.raises(TeamLifecycleError, match="cannot widen"):
        lifecycle.recompose(
            narrowed,
            _request(
                5,
                ProjectScale.MEDIUM,
                ceiling=frozenset({"read_source", "write_source", "run_tests"}),
            ),
        )


def test_versioned_restart_payload_round_trips_replacement_history_exactly() -> None:
    lifecycle = DynamicTeamLifecycle()
    snapshot = lifecycle.compose(_request(25, ProjectScale.LARGE))
    role = next(
        item
        for item in snapshot.current_assignments
        if item.role.capabilities == ("implementation",)
    )
    snapshot = lifecycle.mark_unavailable(
        snapshot,
        role_id=role.role.role_id,
        status=RoleAssignmentStatus.FAILED,
        reason="worker crash",
        evidence_refs=("worker:crash:1",),
    )
    snapshot = lifecycle.replace_unavailable(
        snapshot,
        role_id=role.role.role_id,
        reason="restart with replacement worker",
        evidence_refs=("decision:replacement:1",),
    )

    serialized = snapshot.to_json()
    recovered = TeamLifecycleSnapshot.from_json(serialized)

    assert recovered == snapshot
    assert recovered.to_json() == serialized
    assert recovered.to_team_plan() == snapshot.to_team_plan()


@pytest.mark.parametrize(
    "mutation",
    ("schema", "unknown_key", "permission_escape", "duplicate_semantic_role", "bad_predecessor"),
)
def test_corrupted_restart_payload_fails_closed(mutation: str) -> None:
    lifecycle = DynamicTeamLifecycle()
    snapshot = lifecycle.compose(_request(5, ProjectScale.MEDIUM))
    payload = json.loads(snapshot.to_json())

    if mutation == "schema":
        payload["schema_version"] = 999
    elif mutation == "unknown_key":
        payload["unexpected"] = True
    elif mutation == "permission_escape":
        payload["assignments"][0]["role"]["permissions"].append("admin_project")
    elif mutation == "duplicate_semantic_role":
        duplicate = json.loads(json.dumps(payload["assignments"][0]))
        duplicate["assignment_id"] = "assignment:duplicate"
        duplicate["role"]["role_id"] = "role:duplicate"
        payload["assignments"].append(duplicate)
    elif mutation == "bad_predecessor":
        payload["assignments"][0]["generation"] = 1
        payload["assignments"][0]["replaces_assignment_id"] = "missing:assignment"
    else:
        raise AssertionError(f"unhandled mutation {mutation}")

    with pytest.raises(TeamLifecycleError):
        TeamLifecycleSnapshot.from_json(json.dumps(payload))


def test_retired_specialization_is_recorded_in_history_instead_of_silent_deletion() -> None:
    lifecycle = DynamicTeamLifecycle()
    specialized = lifecycle.compose(
        _request(
            5,
            ProjectScale.MEDIUM,
            requested_specializations=("protocol-specialist",),
        )
    )
    specialist = next(
        item
        for item in specialized.current_assignments
        if item.role.capabilities == ("protocol-specialist",)
    )

    recomposed = lifecycle.recompose(
        specialized,
        _request(5, ProjectScale.MEDIUM),
    )

    assert all(
        item.role.role_id != specialist.role.role_id
        for item in recomposed.current_assignments
    )
    historical = next(
        item
        for item in recomposed.assignments
        if item.assignment_id == specialist.assignment_id
    )
    assert historical.status is RoleAssignmentStatus.RETIRED
    assert historical.transition_reason == "role retired by deterministic recomposition"
