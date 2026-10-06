from __future__ import annotations

import pytest

from nika_core.product_factory_orchestration import (
    ComponentBrief,
    DynamicTeamComposer,
    IntegrationDecision,
    IntegrationDecisionKind,
    OwnershipLease,
    ProductComponent,
    ProductRepositoryGraph,
    ProjectScale,
    RepositoryGraphError,
    RepositoryRef,
    TeamCompositionError,
    TeamCompositionRequest,
)

PERMISSIONS = frozenset({"read_source", "write_source", "run_tests"})


def _graph() -> ProductRepositoryGraph:
    return ProductRepositoryGraph(
        project_id="project-1",
        repositories=(RepositoryRef("repo", "github", "org/repo", "main"),),
        components=(
            ProductComponent("api", "repo", ("src/api",)),
            ProductComponent("worker", "repo", ("src/worker",), dependencies=("api",)),
        ),
    )


def _team_plan():
    return DynamicTeamComposer().compose(
        TeamCompositionRequest(
            project_id="project-1",
            components=(ComponentBrief("api", "backend"), ComponentBrief("ui", "web")),
            acceptance_criteria=("deterministic tests", "independent review"),
            permission_ceiling=PERMISSIONS,
            scale=ProjectScale.MEDIUM,
        )
    )


def test_same_physical_repository_cannot_hide_behind_multiple_logical_ids() -> None:
    for second_locator in ("org/repo", "org/repo/"):
        with pytest.raises(RepositoryGraphError, match="aliased by multiple repository ids"):
            ProductRepositoryGraph(
                project_id="project-1",
                repositories=(
                    RepositoryRef("repo-a", "github", "org/repo", "main"),
                    RepositoryRef("repo-b", "github", second_locator, "main"),
                ),
                components=(
                    ProductComponent("api", "repo-a", ("src/api",)),
                    ProductComponent("worker", "repo-b", ("src/worker",)),
                ),
            )


def test_repository_and_project_identity_fail_closed() -> None:
    for values in (
        ("", "github", "org/repo", "main"),
        ("repo", "", "org/repo", "main"),
        ("repo", "github", "", "main"),
        ("repo", "github", "org/repo", ""),
    ):
        with pytest.raises(RepositoryGraphError, match="identity fields"):
            RepositoryRef(*values)

    with pytest.raises(RepositoryGraphError, match="project_id"):
        ProductRepositoryGraph(project_id="", repositories=(), components=())


def test_repository_credential_ref_requires_nonempty_opaque_identity() -> None:
    for credential_ref in ("credref:", " credref:secret", "credref:   ", "secret"):
        with pytest.raises(RepositoryGraphError):
            RepositoryRef(
                "repo",
                "github",
                "org/repo",
                "main",
                credential_ref=credential_ref,
            )

    repository = RepositoryRef(
        "repo",
        "github",
        "org/repo",
        "main",
        credential_ref="credref:github-project-1",
    )
    assert repository.credential_ref == "credref:github-project-1"


def test_component_identity_and_duplicate_dependency_fail_closed() -> None:
    with pytest.raises(RepositoryGraphError, match="component identity"):
        ProductComponent("", "repo", ("src/api",))
    with pytest.raises(RepositoryGraphError, match="component identity"):
        ProductComponent("api", "", ("src/api",))

    with pytest.raises(RepositoryGraphError, match="repeats a dependency"):
        ProductRepositoryGraph(
            project_id="project-1",
            repositories=(RepositoryRef("repo", "github", "org/repo", "main"),),
            components=(
                ProductComponent("api", "repo", ("src/api",)),
                ProductComponent(
                    "worker",
                    "repo",
                    ("src/worker",),
                    dependencies=("api", "api"),
                ),
            ),
        )


def test_added_specialist_requires_reason_and_known_component() -> None:
    composer = DynamicTeamComposer()
    plan = _team_plan()

    with pytest.raises(TeamCompositionError, match="reason"):
        composer.add_specialist(
            plan,
            specialization="security",
            component_ids=("api",),
            requested_permissions=("read_source",),
            reason="  ",
        )

    with pytest.raises(TeamCompositionError, match="unknown component"):
        composer.add_specialist(
            plan,
            specialization="security",
            component_ids=("phantom",),
            requested_permissions=("read_source",),
            reason="review API security",
        )


def test_added_specialist_permissions_remain_bounded_by_project_ceiling() -> None:
    composer = DynamicTeamComposer()
    plan = _team_plan()

    expanded = composer.add_specialist(
        plan,
        specialization="protocol-specialist",
        component_ids=("api",),
        requested_permissions=("read_source", "write_source", "deploy_production"),
        reason="protocol compatibility review",
        evidence_refs=("decision:protocol",),
    )

    role = expanded.roles[-1]
    assert role.component_ids == ("api",)
    assert role.permissions == frozenset({"read_source", "write_source"})
    assert role.evidence_refs == ("decision:protocol",)


def test_integration_decision_requires_exact_pair_identity_and_evidence() -> None:
    invalid = (
        ("", ("candidate", "active"), "reason", ("evidence",)),
        ("decision", ("candidate", "candidate"), "reason", ("evidence",)),
        ("decision", ("candidate", "active"), "", ("evidence",)),
        ("decision", ("candidate", "active"), "reason", ()),
    )
    for decision_id, lease_ids, reason, evidence_refs in invalid:
        with pytest.raises(RepositoryGraphError):
            IntegrationDecision(
                decision_id,
                IntegrationDecisionKind.RECONCILE,
                lease_ids,
                reason,
                evidence_refs,
            )


def test_integration_decision_must_bind_actual_conflict_pair() -> None:
    graph = _graph()
    active = OwnershipLease("active", "worker-a", ("api",), ("src/api",))
    candidate = OwnershipLease("candidate", "worker-b", ("api",), ("src/api/routes",))
    unrelated = IntegrationDecision(
        "decision-1",
        IntegrationDecisionKind.RECONCILE,
        ("candidate", "unrelated"),
        "reconcile shared contract",
        ("compare:evidence",),
    )

    with pytest.raises(RepositoryGraphError, match="every conflicting active lease"):
        graph.assess_lease(candidate, (active,), decision=unrelated)

    exact = IntegrationDecision(
        "decision-2",
        IntegrationDecisionKind.RECONCILE,
        ("candidate", "active"),
        "reconcile shared contract",
        ("compare:evidence",),
    )
    assessment = graph.assess_lease(candidate, (active,), decision=exact)
    assert not assessment.grantable
    assert assessment.requires_integration
    assert assessment.decision == exact


def test_duplicate_active_lease_identity_fails_closed() -> None:
    graph = _graph()
    candidate = OwnershipLease("candidate", "worker", ("api",), ("src/api/routes",))
    active_a = OwnershipLease("active", "worker-a", ("api",), ("src/api/a",))
    active_b = OwnershipLease("active", "worker-b", ("api",), ("src/api/b",))

    with pytest.raises(RepositoryGraphError, match="active lease ids must be unique"):
        graph.assess_lease(candidate, (active_a, active_b))


def test_duplicate_lease_component_identity_fails_closed() -> None:
    graph = _graph()
    duplicate = OwnershipLease(
        "candidate",
        "worker",
        ("api", "api"),
        ("src/api",),
    )

    with pytest.raises(RepositoryGraphError, match="lease component ids must be unique"):
        graph.assess_lease(duplicate, ())
