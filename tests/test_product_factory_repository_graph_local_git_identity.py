from __future__ import annotations

import pytest

from nika_core.product_factory_orchestration import (
    ProductComponent,
    ProductRepositoryGraph,
    RepositoryGraphError,
    RepositoryRef,
)


def _graph_for_locators(
    locator_a: str,
    locator_b: str,
    *,
    windows_path_semantics: bool = False,
) -> ProductRepositoryGraph:
    return ProductRepositoryGraph(
        project_id="project:local-git-alias",
        repositories=(
            RepositoryRef(
                "repo:a",
                "local-git",
                locator_a,
                "main",
                windows_path_semantics=windows_path_semantics,
            ),
            RepositoryRef(
                "repo:b",
                "local-git",
                locator_b,
                "main",
                windows_path_semantics=windows_path_semantics,
            ),
        ),
        components=(
            ProductComponent("component:a", "repo:a", ("src/a",)),
            ProductComponent("component:b", "repo:b", ("src/b",)),
        ),
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
        _graph_for_locators(canonical_locator, aliased_locator)


def test_windows_local_git_drive_and_path_case_aliases_fail_closed() -> None:
    with pytest.raises(RepositoryGraphError, match="physical repository is aliased"):
        _graph_for_locators(
            r"C:\Work\Nika\Repo",
            "c:/work/nika/repo/./",
            windows_path_semantics=True,
        )


def test_windows_local_git_unc_case_and_separator_aliases_fail_closed() -> None:
    with pytest.raises(RepositoryGraphError, match="physical repository is aliased"):
        _graph_for_locators(
            r"\\Server\Share\Nika\Repo",
            "//server/share/nika/repo/./",
            windows_path_semantics=True,
        )


@pytest.mark.parametrize(
    "unsafe_locator",
    (
        r"\\?\C:\Nika\Repo",
        r"\\.\C:\Nika\Repo",
        r"C:relative\repo",
        r"\current-drive\repo",
        r"\\server",
        r"C:\NUL\..\repo",
        r"C:\file:stream\..\repo",
        r"C:\bad.\..\repo",
        "C:/bad /../repo",
    ),
)
def test_windows_local_git_unsafe_raw_locator_identity_fails_closed(
    unsafe_locator: str,
) -> None:
    with pytest.raises(RepositoryGraphError, match="Windows local-git"):
        _graph_for_locators(
            r"C:\Known\Repo",
            unsafe_locator,
            windows_path_semantics=True,
        )


def test_windows_local_git_preserves_non_ascii_edge_whitespace_identity() -> None:
    graph = _graph_for_locators(
        r"C:\Repo",
        "C:/\u3000Repo",
        windows_path_semantics=True,
    )

    assert graph.dependency_order() == ("component:a", "component:b")


def test_non_windows_local_git_preserves_non_ascii_edge_whitespace_identity() -> None:
    graph = _graph_for_locators("checkout", "\u3000checkout")

    assert graph.dependency_order() == ("component:a", "component:b")


def test_local_git_distinct_sibling_locators_remain_independent() -> None:
    graph = _graph_for_locators("checkout-a", "checkout-b")

    assert graph.dependency_order() == ("component:a", "component:b")
