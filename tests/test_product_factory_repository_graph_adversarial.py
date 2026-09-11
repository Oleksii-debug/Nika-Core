from __future__ import annotations

from itertools import permutations

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


def _single_repo_graph(
    *,
    case_sensitive: bool = True,
    windows_path_semantics: bool = False,
) -> ProductRepositoryGraph:
    return ProductRepositoryGraph(
        project_id="project:graph-adversarial",
        repositories=(
            RepositoryRef(
                "repo:app",
                "github",
                "owner/app",
                "main",
                case_sensitive_paths=case_sensitive,
                windows_path_semantics=windows_path_semantics,
            ),
        ),
        components=(ProductComponent("app", "repo:app", ("src",)),),
    )


def _multi_repo_shared_path_graph() -> ProductRepositoryGraph:
    return ProductRepositoryGraph(
        project_id="project:multi-repo",
        repositories=(
            RepositoryRef("repo:a", "github", "owner/a", "main"),
            RepositoryRef("repo:b", "github", "owner/b", "main"),
        ),
        components=(
            ProductComponent("component:a", "repo:a", ("src/shared",)),
            ProductComponent("component:b", "repo:b", ("src/shared",)),
        ),
    )


def test_component_path_boundary_siblings_remain_independently_owned() -> None:
    graph = ProductRepositoryGraph(
        project_id="project:boundary",
        repositories=(RepositoryRef("repo", "github", "owner/repo", "main"),),
        components=(
            ProductComponent("api", "repo", ("src/api",)),
            ProductComponent("api-v2", "repo", ("src/api-v2",)),
        ),
    )

    assert graph.dependency_order() == ("api", "api-v2")


def test_lease_path_boundary_siblings_do_not_conflict() -> None:
    graph = _single_repo_graph()
    active = OwnershipLease("lease:api", "worker:a", ("app",), ("src/api",))
    candidate = OwnershipLease("lease:api-v2", "worker:b", ("app",), ("src/api-v2",))

    assessment = graph.assess_lease(candidate, (active,))

    assert assessment.grantable
    assert assessment.conflicts == ()
    assert not assessment.requires_integration


def test_case_insensitive_repository_detects_case_variant_lease_overlap() -> None:
    graph = _single_repo_graph(case_sensitive=False)
    active = OwnershipLease("lease:upper", "worker:a", ("app",), ("src/API",))
    candidate = OwnershipLease("lease:lower", "worker:b", ("app",), ("SRC/api/routes",))

    assessment = graph.assess_lease(candidate, (active,))

    assert not assessment.grantable
    assert len(assessment.conflicts) == 1
    assert assessment.conflicts[0].active_lease_id == "lease:upper"


def test_case_sensitive_repository_keeps_case_variant_lease_paths_independent() -> None:
    graph = _single_repo_graph(case_sensitive=True)
    active = OwnershipLease("lease:upper", "worker:a", ("app",), ("src/API",))
    candidate = OwnershipLease("lease:lower", "worker:b", ("app",), ("src/api",))

    assessment = graph.assess_lease(candidate, (active,))

    assert assessment.grantable
    assert assessment.conflicts == ()


def test_n_way_reconciliation_is_order_invariant_and_requires_exact_conflict_set() -> None:
    graph = _single_repo_graph()
    candidate = OwnershipLease("lease:candidate", "worker:c", ("app",), ("src/api",))
    active_a = OwnershipLease("lease:a", "worker:a", ("app",), ("src/api/routes",))
    active_b = OwnershipLease("lease:b", "worker:b", ("app",), ("src/api/models",))
    unrelated = OwnershipLease("lease:docs", "worker:d", ("app",), ("src/docs",))
    exact = IntegrationDecision(
        "decision:exact-n-way",
        IntegrationDecisionKind.RECONCILE,
        ("lease:b", "lease:candidate", "lease:a"),
        "both overlapping owners must reconcile the shared API boundary",
        ("evidence:ownership-map",),
    )

    observed = []
    for active_order in permutations((active_a, active_b, unrelated)):
        assessment = graph.assess_lease(candidate, active_order, decision=exact)
        assert not assessment.grantable
        assert assessment.requires_integration
        assert assessment.decision == exact
        observed.append(
            tuple(
                (conflict.active_lease_id, conflict.path_a, conflict.path_b)
                for conflict in assessment.conflicts
            )
        )

    assert len(set(observed)) == 1
    assert {item[0] for item in observed[0]} == {"lease:a", "lease:b"}

    missing = IntegrationDecision(
        "decision:missing",
        IntegrationDecisionKind.RECONCILE,
        ("lease:candidate", "lease:a"),
        "missing one actual conflicting owner must fail closed",
        ("evidence:incomplete",),
    )
    with pytest.raises(RepositoryGraphError, match="every conflicting active lease"):
        graph.assess_lease(candidate, (active_a, active_b, unrelated), decision=missing)

    extra = IntegrationDecision(
        "decision:extra",
        IntegrationDecisionKind.RECONCILE,
        ("lease:candidate", "lease:a", "lease:b", "lease:docs"),
        "non-conflicting owners must not be smuggled into the reconciliation identity",
        ("evidence:overbroad",),
    )
    with pytest.raises(RepositoryGraphError, match="every conflicting active lease"):
        graph.assess_lease(candidate, (active_a, active_b, unrelated), decision=extra)


def test_stale_integration_decision_cannot_attach_to_nonconflicting_candidate() -> None:
    graph = _single_repo_graph()
    active = OwnershipLease("lease:api", "worker:a", ("app",), ("src/api",))
    candidate = OwnershipLease("lease:docs", "worker:b", ("app",), ("src/docs",))
    stale = IntegrationDecision(
        "decision:stale",
        IntegrationDecisionKind.RECONCILE,
        ("lease:docs", "lease:api"),
        "stale reconciliation evidence must not authorize unrelated ownership",
        ("evidence:stale",),
    )

    with pytest.raises(RepositoryGraphError, match="every conflicting active lease"):
        graph.assess_lease(candidate, (active,), decision=stale)


def test_cross_repository_same_text_lease_paths_remain_isolated() -> None:
    graph = _multi_repo_shared_path_graph()
    active = OwnershipLease("lease:a", "worker:a", ("component:a",), ("src/shared",))
    candidate = OwnershipLease("lease:b", "worker:b", ("component:b",), ("src/shared",))

    assessment = graph.assess_lease(candidate, (active,))

    assert assessment.grantable
    assert assessment.conflicts == ()


def test_multi_repository_lease_fails_closed_when_path_identity_is_ambiguous() -> None:
    graph = _multi_repo_shared_path_graph()
    ambiguous = OwnershipLease(
        "lease:ambiguous",
        "worker:multi",
        ("component:a", "component:b"),
        ("src/shared",),
    )

    with pytest.raises(RepositoryGraphError, match="ambiguous across repositories"):
        graph.assess_lease(ambiguous, ())


def test_windows_separator_normalization_preserves_overlap_identity() -> None:
    graph = _single_repo_graph()
    active = OwnershipLease("lease:windows", "worker:a", ("app",), (r"src\api",))
    candidate = OwnershipLease(
        "lease:posix",
        "worker:b",
        ("app",),
        ("src/api/routes",),
    )

    assessment = graph.assess_lease(candidate, (active,))

    assert not assessment.grantable
    assert assessment.conflicts[0].path_b == "src/api"


@pytest.mark.parametrize(
    "unsafe_path",
    ("../outside", "/src/api", r"C:\src\api", "src/api/../../../outside"),
)
def test_lease_paths_fail_closed_on_escape_or_absolute_identity(unsafe_path: str) -> None:
    graph = _single_repo_graph()
    candidate = OwnershipLease("lease:unsafe", "worker:a", ("app",), (unsafe_path,))

    with pytest.raises(RepositoryGraphError):
        graph.assess_lease(candidate, ())


@pytest.mark.parametrize(
    "unsafe_path",
    (
        "src/api.",
        "src/api ",
        "src/bad /child",
        "src/NUL.txt",
        "src/COM0.log",
        "src/COM1.log",
        "src/LPT0",
        "src/CONIN$",
        "src/CONOUT$",
        "src/file:stream",
        "src/what?now",
        "src/control\x01",
        "src/control\t",
    ),
)
def test_windows_repository_rejects_nonportable_component_identity(unsafe_path: str) -> None:
    repository = RepositoryRef(
        "repo",
        "github",
        "owner/repo",
        "main",
        windows_path_semantics=True,
    )

    with pytest.raises(RepositoryGraphError, match="Windows repository path"):
        ProductRepositoryGraph(
            project_id="project:windows-paths",
            repositories=(repository,),
            components=(ProductComponent("app", "repo", (unsafe_path,)),),
        )


def test_windows_repository_rejects_ads_lease_under_broad_component_root() -> None:
    graph = _single_repo_graph(windows_path_semantics=True)
    candidate = OwnershipLease(
        "lease:ads",
        "worker:a",
        ("app",),
        ("src/api.py:review",),
    )

    with pytest.raises(RepositoryGraphError, match="Windows repository path"):
        graph.assess_lease(candidate, ())


def test_windows_repository_accepts_normal_dotted_paths() -> None:
    graph = _single_repo_graph(
        case_sensitive=True,
        windows_path_semantics=True,
    )
    candidate = OwnershipLease(
        "lease:normal",
        "worker:a",
        ("app",),
        ("src/api.v2/client.py",),
    )

    assessment = graph.assess_lease(candidate, ())

    assert assessment.grantable
    assert assessment.conflicts == ()


def test_non_windows_repository_preserves_existing_posix_path_semantics() -> None:
    graph = ProductRepositoryGraph(
        project_id="project:posix-paths",
        repositories=(RepositoryRef("repo", "github", "owner/repo", "main"),),
        components=(
            ProductComponent(
                "app",
                "repo",
                ("src/file:stream", "src/trailing."),
            ),
        ),
    )

    assert graph.dependency_order() == ("app",)
