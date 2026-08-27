from __future__ import annotations

import pytest

from nika_core.product_factory_orchestration import (
    ProductComponent,
    ProductRepositoryGraph,
    RepositoryGraphError,
    RepositoryRef,
)


@pytest.mark.parametrize(
    "aliased_locator",
    (
        "checkout/.",
        "checkout/sub/..",
        r"checkout\.",
        "/workspace/checkout/.",
        "/workspace/checkout/sub/..",
    ),
)
def test_local_git_lexical_locator_aliases_fail_closed(aliased_locator: str) -> None:
    canonical_locator = (
        "/workspace/checkout" if aliased_locator.startswith("/workspace/") else "checkout"
    )

    with pytest.raises(RepositoryGraphError, match="physical repository is aliased"):
        ProductRepositoryGraph(
            project_id="project:local-git-alias",
            repositories=(
                RepositoryRef("repo:a", "local-git", canonical_locator, "main"),
                RepositoryRef("repo:b", "local-git", aliased_locator, "main"),
            ),
            components=(
                ProductComponent("component:a", "repo:a", ("src/a",)),
                ProductComponent("component:b", "repo:b", ("src/b",)),
            ),
        )


def test_local_git_distinct_sibling_locators_remain_independent() -> None:
    graph = ProductRepositoryGraph(
        project_id="project:local-git-siblings",
        repositories=(
            RepositoryRef("repo:a", "local-git", "checkout-a", "main"),
            RepositoryRef("repo:b", "local-git", "checkout-b", "main"),
        ),
        components=(
            ProductComponent("component:a", "repo:a", ("src",)),
            ProductComponent("component:b", "repo:b", ("src",)),
        ),
    )

    assert graph.dependency_order() == ("component:a", "component:b")
