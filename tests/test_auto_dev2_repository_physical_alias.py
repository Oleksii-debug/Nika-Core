from __future__ import annotations

import os
from pathlib import Path

import pytest

from nika_core.product_factory_orchestration import (
    ProductComponent,
    ProductRepositoryGraph,
    RepositoryGraphError,
    RepositoryRef,
)


def test_local_git_lexical_alias_cannot_masquerade_as_second_logical_repository(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    canonical = str(checkout.resolve())
    lexical_alias = canonical + os.sep + "."

    assert Path(lexical_alias).resolve() == checkout.resolve()

    repositories = (
        RepositoryRef("repo:a", "local-git", canonical, "main"),
        RepositoryRef("repo:b", "local-git", lexical_alias, "main"),
    )
    components = (
        ProductComponent("component:a", "repo:a", ("src/a",)),
        ProductComponent("component:b", "repo:b", ("src/b",)),
    )

    with pytest.raises(RepositoryGraphError, match="physical repository is aliased"):
        ProductRepositoryGraph(
            project_id="project:physical-alias",
            repositories=repositories,
            components=components,
        )
