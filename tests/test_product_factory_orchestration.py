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
    TeamCompositionRequest,
)

FULL_CEILING = frozenset(
    {"read_project", "update_project", "read_source", "write_source", "run_tests", "build_release"}
)


def _request(*, scale: ProjectScale, ceiling: frozenset[str] = FULL_CEILING) -> TeamCompositionRequest:
    return TeamCompositionRequest(
        project_id="project:expense-app",
        components=(
            ComponentBrief("desktop", "windows", frozenset({"accessibility"})),
            ComponentBrief("api", "backend", frozenset({"network"})),
        ),
        acceptance_criteria=("Accessible with NVDA", "Package an exact release"),
        permission_ceiling=ceiling,
        scale=scale,
        evidence_refs=("evidence:requirements:v3",),
    )


def _graph(*, case_sensitive: bool = True) -> ProductRepositoryGraph:
    return ProductRepositoryGraph(
        project_id="project:expense-app",
        repositories=(
            RepositoryRef(
                "repo:app",
                "github",
                "owner/app",
                "main",
                credential_ref="credref:github-project",
                case_sensitive_paths=case_sensitive,
            ),
            RepositoryRef("repo:docs", "github", "owner/app-docs", "main"),
        ),
        components=(
            ProductComponent(
                "api",
                "repo:app",
                ("src/api",),
                build_commands=(("python", "-m", "build"),),
                test_commands=(("python", "-m", "pytest", "tests/api"),),
                release_identity="api:v1",
            ),
            ProductComponent(
                "desktop",
                "repo:app",
                ("src/desktop",),
                dependencies=("api",),
                test_commands=(("python", "-m", "pytest", "tests/desktop"),),
                release_identity="desktop:v1",
            ),
            ProductComponent(
                "docs",
                "repo:docs",
                ("docs",),
                dependencies=("desktop",),
                release_identity="docs:v1",
            ),
        ),
    )


def test_small_project_consolidates_builder_but_keeps_independent_review() -> None:
    plan = DynamicTeamComposer().compose(_request(scale=ProjectScale.SMALL))

    assert len(plan.roles) == 2
    builder, reviewer = plan.roles
    assert {"windows", "backend", "accessibility", "security", "release"}.issubset(
        builder.capabilities
    )
    assert reviewer.independent_review is True
    assert reviewer.capabilities == ("qa",)


def test_large_project_fans_out_implementation_by_component() -> None:
    plan = DynamicTeamComposer().compose(_request(scale=ProjectScale.LARGE))

    implementation = [role for role in plan.roles if role.capabilities == ("implementation",)]
    assert {role.component_ids for role in implementation} == {("api",), ("desktop",)}
    assert any(role.capabilities == ("security",) for role in plan.roles)
    assert any(role.capabilities == ("accessibility",) for role in plan.roles)


def test_team_plan_is_deterministic() -> None:
    composer = DynamicTeamComposer()
    first = composer.compose(_request(scale=ProjectScale.MEDIUM))
    second = composer.compose(_request(scale=ProjectScale.MEDIUM))

    assert first == second
    assert first.plan_id == second.plan_id


def test_permissions_are_attenuated_to_project_ceiling() -> None:
    ceiling = frozenset({"read_source", "run_tests"})
    plan = DynamicTeamComposer().compose(_request(scale=ProjectScale.MEDIUM, ceiling=ceiling))

    assert plan.roles
    assert all(role.permissions <= ceiling for role in plan.roles)
    assert all("write_source" not in role.permissions for role in plan.roles)
    assert all("build_release" not in role.permissions for role in plan.roles)


def test_add_specialist_preserves_existing_roles_and_attenuates_permissions() -> None:
    composer = DynamicTeamComposer()
    original = composer.compose(_request(scale=ProjectScale.SMALL))
    updated = composer.add_specialist(
        original,
        specialization="localization",
        component_ids=("desktop",),
        requested_permissions=("read_source", "write_source", "admin_project"),
        reason="Ukrainian release requires localization review",
        evidence_refs=("evidence:locale-risk",),
    )

    assert updated.roles[:-1] == original.roles
    specialist = updated.roles[-1]
    assert specialist.capabilities == ("localization",)
    assert specialist.permissions == frozenset({"read_source", "write_source"})
    assert "admin_project" not in specialist.permissions


def test_repository_graph_orders_dependencies_across_repositories() -> None:
    graph = _graph()

    assert graph.dependency_order() == ("api", "desktop", "docs")


def test_repository_graph_rejects_cycle() -> None:
    with pytest.raises(RepositoryGraphError, match="cycle"):
        ProductRepositoryGraph(
            project_id="p",
            repositories=(RepositoryRef("repo", "github", "o/r", "main"),),
            components=(
                ProductComponent("a", "repo", ("a",), dependencies=("b",)),
                ProductComponent("b", "repo", ("b",), dependencies=("a",)),
            ),
        )


def test_repository_graph_rejects_component_path_overlap() -> None:
    with pytest.raises(RepositoryGraphError, match="overlap"):
        ProductRepositoryGraph(
            project_id="p",
            repositories=(RepositoryRef("repo", "github", "o/r", "main"),),
            components=(
                ProductComponent("a", "repo", ("src",)),
                ProductComponent("b", "repo", ("src/b",)),
            ),
        )


def test_repository_graph_treats_case_collisions_per_repository_policy() -> None:
    with pytest.raises(RepositoryGraphError, match="overlap"):
        ProductRepositoryGraph(
            project_id="p",
            repositories=(
                RepositoryRef(
                    "repo", "github", "o/r", "main", case_sensitive_paths=False
                ),
            ),
            components=(
                ProductComponent("a", "repo", ("Src/API",)),
                ProductComponent("b", "repo", ("src/api/client",)),
            ),
        )


def test_repository_graph_allows_same_path_text_in_different_repositories() -> None:
    graph = ProductRepositoryGraph(
        project_id="p",
        repositories=(
            RepositoryRef("a", "github", "o/a", "main"),
            RepositoryRef("b", "github", "o/b", "main"),
        ),
        components=(
            ProductComponent("api", "a", ("src",)),
            ProductComponent("sdk", "b", ("src",)),
        ),
    )

    assert graph.dependency_order() == ("api", "sdk")


def test_repository_graph_rejects_plaintext_like_credential_field() -> None:
    with pytest.raises(RepositoryGraphError, match="credref"):
        RepositoryRef("repo", "github", "o/r", "main", credential_ref="ghp_secret")


def test_lease_rejects_path_outside_component_boundary() -> None:
    graph = _graph()
    lease = OwnershipLease("lease:1", "worker:1", ("api",), ("src/desktop",))

    with pytest.raises(RepositoryGraphError, match="outside component ownership"):
        graph.assess_lease(lease, ())


def test_parallel_non_overlapping_component_leases_are_grantable() -> None:
    graph = _graph()
    active = OwnershipLease("lease:api", "worker:api", ("api",), ("src/api",))
    candidate = OwnershipLease(
        "lease:desktop", "worker:desktop", ("desktop",), ("src/desktop",)
    )

    assessment = graph.assess_lease(candidate, (active,))

    assert assessment.grantable is True
    assert assessment.conflicts == ()


def test_overlapping_worker_lease_is_not_silently_grantable() -> None:
    graph = _graph()
    active = OwnershipLease("lease:api-a", "worker:a", ("api",), ("src/api",))
    candidate = OwnershipLease("lease:api-b", "worker:b", ("api",), ("src/api/routes",))

    assessment = graph.assess_lease(candidate, (active,))

    assert assessment.grantable is False
    assert assessment.requires_integration is False
    assert assessment.conflicts[0].active_lease_id == "lease:api-a"


def test_explicit_integration_decision_marks_overlap_for_reconciliation_not_grant() -> None:
    graph = _graph()
    active = OwnershipLease("lease:api-a", "worker:a", ("api",), ("src/api",))
    candidate = OwnershipLease("lease:api-b", "worker:b", ("api",), ("src/api/routes",))
    decision = IntegrationDecision(
        "decision:1",
        IntegrationDecisionKind.RECONCILE,
        ("lease:api-a", "lease:api-b"),
        "stale candidate must be reconciled after active owner finishes",
        ("evidence:compare:abc",),
    )

    assessment = graph.assess_lease(candidate, (active,), decision=decision)

    assert assessment.grantable is False
    assert assessment.requires_integration is True
    assert assessment.decision == decision


def test_repository_paths_fail_closed_on_absolute_or_traversal() -> None:
    repository = RepositoryRef("repo", "github", "o/r", "main")

    for path in ("../outside", "/absolute", "C:/absolute"):
        with pytest.raises(RepositoryGraphError):
            ProductRepositoryGraph(
                project_id="p",
                repositories=(repository,),
                components=(ProductComponent("a", "repo", (path,)),),
            )
