from __future__ import annotations

import pytest

from nika_core.product_factory_orchestration import (
    IntegrationDecision,
    IntegrationDecisionKind,
    OwnershipLease,
    ProductComponent,
    ProductRepositoryGraph,
    RepositoryGraphError,
    RepositoryRef,
)


def test_pf3_github_repository_case_alias_cannot_create_second_logical_identity() -> None:
    """GitHub owner/repository names are one physical identity across case variants."""
    with pytest.raises(RepositoryGraphError):
        ProductRepositoryGraph(
            project_id="product-1",
            repositories=(
                RepositoryRef("repo-a", "github", "Owner/Shared-Repo", "main"),
                RepositoryRef("repo-b", "github", "owner/shared-repo", "main"),
            ),
            components=(
                ProductComponent("api", "repo-a", ("src/api",)),
                ProductComponent("worker", "repo-b", ("src/worker",)),
            ),
        )


def test_pf3_valid_reconciliation_can_cover_multiple_conflicting_active_leases() -> None:
    """Exact conflict coverage must support N active owners, not only one pair."""
    graph = ProductRepositoryGraph(
        project_id="product-1",
        repositories=(RepositoryRef("repo", "github", "owner/repo", "main"),),
        components=(ProductComponent("api", "repo", ("src/api",)),),
    )
    active_a = OwnershipLease(
        "lease-active-a",
        "worker-a",
        ("api",),
        ("src/api/routes",),
    )
    active_b = OwnershipLease(
        "lease-active-b",
        "worker-b",
        ("api",),
        ("src/api/models",),
    )
    candidate = OwnershipLease(
        "lease-candidate",
        "worker-candidate",
        ("api",),
        ("src/api",),
    )
    decision = IntegrationDecision(
        "decision-all-conflicts",
        IntegrationDecisionKind.RECONCILE,
        ("lease-candidate", "lease-active-a", "lease-active-b"),
        "reconcile candidate with every conflicting active owner",
        ("evidence:compare",),
    )

    assessment = graph.assess_lease(
        candidate,
        (active_a, active_b),
        decision=decision,
    )

    assert {conflict.active_lease_id for conflict in assessment.conflicts} == {
        "lease-active-a",
        "lease-active-b",
    }
    assert assessment.decision == decision
