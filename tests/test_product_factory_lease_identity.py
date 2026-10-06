from __future__ import annotations

import pytest

from nika_core.product_factory_orchestration import (
    OwnershipLease,
    ProductComponent,
    ProductRepositoryGraph,
    RepositoryGraphError,
    RepositoryRef,
)


def _graph() -> ProductRepositoryGraph:
    return ProductRepositoryGraph(
        project_id="project:lease-identity",
        repositories=(RepositoryRef("repo", "github", "owner/product", "main"),),
        components=(
            ProductComponent("api", "repo", ("src/api",)),
            ProductComponent("desktop", "repo", ("src/desktop",)),
        ),
    )


def test_candidate_cannot_reuse_active_lease_identity_for_another_worker() -> None:
    graph = _graph()
    active = OwnershipLease("lease:shared", "worker:a", ("api",), ("src/api",))
    candidate = OwnershipLease(
        "lease:shared",
        "worker:b",
        ("desktop",),
        ("src/desktop",),
    )

    with pytest.raises(RepositoryGraphError, match="candidate lease id is already active"):
        graph.assess_lease(candidate, (active,))


def test_candidate_cannot_reuse_active_lease_identity_for_same_worker() -> None:
    graph = _graph()
    active = OwnershipLease("lease:shared", "worker:a", ("api",), ("src/api",))
    candidate = OwnershipLease(
        "lease:shared",
        "worker:a",
        ("desktop",),
        ("src/desktop",),
    )

    with pytest.raises(RepositoryGraphError, match="candidate lease id is already active"):
        graph.assess_lease(candidate, (active,))
