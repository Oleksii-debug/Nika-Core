from __future__ import annotations

import pytest

from nika_core.product_factory_orchestration import (
    OwnershipLease,
    ProductComponent,
    ProductRepositoryGraph,
    RepositoryGraphError,
    RepositoryRef,
)


def _windows_repository(*, repository_id: str = "repo") -> RepositoryRef:
    locator_name = repository_id.replace(":", "-")
    return RepositoryRef(
        repository_id,
        "github",
        f"owner/{locator_name}",
        "main",
        windows_path_semantics=True,
    )


@pytest.mark.parametrize(
    "unsafe_path",
    (
        "src/NUL/../safe",
        "src/COM1/../safe",
        "src/file:stream/../safe",
        "src/bad./../safe",
        "src/../safe",
        "src/ child",
    ),
)
def test_windows_component_rejects_unsafe_raw_identity_before_normpath(
    unsafe_path: str,
) -> None:
    with pytest.raises(RepositoryGraphError, match="Windows repository path"):
        ProductRepositoryGraph(
            project_id="project:raw-windows-component",
            repositories=(_windows_repository(),),
            components=(ProductComponent("app", "repo", (unsafe_path,)),),
        )


@pytest.mark.parametrize(
    "unsafe_path",
    (
        "src/NUL/../api",
        "src/file:stream/../api",
        "src/bad./../api",
        "src/../api",
    ),
)
def test_windows_lease_rejects_unsafe_raw_identity_before_normpath(
    unsafe_path: str,
) -> None:
    graph = ProductRepositoryGraph(
        project_id="project:raw-windows-lease",
        repositories=(_windows_repository(),),
        components=(ProductComponent("app", "repo", ("src",)),),
    )
    lease = OwnershipLease("lease:unsafe", "worker", ("app",), (unsafe_path,))

    with pytest.raises(RepositoryGraphError, match="Windows repository path"):
        graph.assess_lease(lease, ())


def test_windows_lease_does_not_strip_unicode_component_root() -> None:
    root = "\u3000src"
    graph = ProductRepositoryGraph(
        project_id="project:unicode-boundary",
        repositories=(_windows_repository(),),
        components=(ProductComponent("app", "repo", (root,)),),
    )
    candidate = OwnershipLease(
        "lease:wrong-root",
        "worker",
        ("app",),
        ("src/api",),
    )

    with pytest.raises(RepositoryGraphError, match="outside component ownership"):
        graph.assess_lease(candidate, ())


def test_windows_preserves_non_ascii_leading_whitespace_identity() -> None:
    root = "\u3000src"
    graph = ProductRepositoryGraph(
        project_id="project:unicode-whitespace",
        repositories=(_windows_repository(),),
        components=(ProductComponent("app", "repo", (root,)),),
    )
    active = OwnershipLease("lease:root", "worker:a", ("app",), (root,))
    candidate = OwnershipLease(
        "lease:child",
        "worker:b",
        ("app",),
        (f"{root}/api",),
    )

    assessment = graph.assess_lease(candidate, (active,))

    assert len(assessment.conflicts) == 1
    assert assessment.conflicts[0].path_a == f"{root}/api"
    assert assessment.conflicts[0].path_b == root


def test_mixed_repo_policy_resolves_only_matching_repository_identity() -> None:
    graph = ProductRepositoryGraph(
        project_id="project:mixed-path-policy",
        repositories=(
            _windows_repository(repository_id="repo:windows"),
            RepositoryRef("repo:posix", "git", "owner/posix", "main"),
        ),
        components=(
            ProductComponent("windows", "repo:windows", ("\u3000src",)),
            ProductComponent("posix", "repo:posix", ("src",)),
        ),
    )
    active = OwnershipLease("lease:posix", "worker:a", ("posix",), ("src",))
    candidate = OwnershipLease(
        "lease:mixed",
        "worker:b",
        ("windows", "posix"),
        ("src/api",),
    )

    assessment = graph.assess_lease(candidate, (active,))

    assert len(assessment.conflicts) == 1
    assert assessment.conflicts[0].repository_id == "repo:posix"


def test_windows_invalid_ads_does_not_poison_valid_posix_repository_path() -> None:
    graph = ProductRepositoryGraph(
        project_id="project:mixed-ads-policy",
        repositories=(
            _windows_repository(repository_id="repo:windows"),
            RepositoryRef("repo:posix", "git", "owner/posix", "main"),
        ),
        components=(
            ProductComponent("windows", "repo:windows", ("src",)),
            ProductComponent("posix", "repo:posix", ("src",)),
        ),
    )
    active = OwnershipLease("lease:posix", "worker:a", ("posix",), ("src",))
    candidate = OwnershipLease(
        "lease:mixed",
        "worker:b",
        ("windows", "posix"),
        ("src/file:stream",),
    )

    assessment = graph.assess_lease(candidate, (active,))

    assert len(assessment.conflicts) == 1
    assert assessment.conflicts[0].repository_id == "repo:posix"
    assert assessment.conflicts[0].path_a == "src/file:stream"


def test_non_windows_internal_components_keep_existing_normalization() -> None:
    graph = ProductRepositoryGraph(
        project_id="project:posix-normalization",
        repositories=(RepositoryRef("repo", "github", "owner/repo", "main"),),
        components=(ProductComponent("app", "repo", ("src/NUL/../safe",)),),
    )

    assert graph.dependency_order() == ("app",)
