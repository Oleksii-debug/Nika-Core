from __future__ import annotations

from dataclasses import replace

import pytest

from nika_core.product_factory_coordinator import (
    CoordinatorError,
    CoordinatorSnapshot,
    ProductFactoryCoordinator,
    WorkerResultEnvelope,
    WorkRecord,
)
from nika_core.product_factory_orchestration import (
    IntegrationDecision,
    IntegrationDecisionKind,
    OwnershipLease,
    ProductComponent,
    ProductRepositoryGraph,
    RepositoryGraphError,
    RepositoryRef,
)
from nika_core.toolsmith.contracts import CodingResult, WorkerFailure, WorkerFailureKind

SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40
DIGEST = "d" * 64
PERMISSIONS = frozenset({"read_source", "write_source", "run_tests"})


def _graph() -> ProductRepositoryGraph:
    return ProductRepositoryGraph(
        project_id="product-final-integrity",
        repositories=(RepositoryRef("repo-main", "github", "Owner/Product", "main"),),
        components=(
            ProductComponent(
                "core",
                "repo-main",
                ("src/Core",),
                test_commands=(("python", "-m", "pytest", "tests/core"),),
            ),
            ProductComponent(
                "docs",
                "repo-main",
                ("docs/Product",),
                test_commands=(("python", "-m", "pytest", "tests/docs"),),
            ),
        ),
    )


def _planned() -> tuple[ProductRepositoryGraph, ProductFactoryCoordinator, CoordinatorSnapshot]:
    graph = _graph()
    coordinator = ProductFactoryCoordinator(graph)
    snapshot = coordinator.plan(
        base_shas={"repo-main": SHA_A},
        goals={"core": "Implement core", "docs": "Write product docs"},
        permission_ceiling=PERMISSIONS,
    )
    return graph, coordinator, snapshot


def _replace_requests(snapshot: CoordinatorSnapshot, transform) -> CoordinatorSnapshot:
    return replace(
        snapshot,
        revision=snapshot.revision + 1,
        records=tuple(
            WorkRecord(
                transform(record.request),
                record.state,
                record.result,
                record.review,
                record.blocker,
            )
            for record in snapshot.records
        ),
    )


def test_github_owner_repository_identity_is_case_insensitive_without_path_case_change() -> None:
    with pytest.raises(RepositoryGraphError, match="aliased by multiple repository ids"):
        ProductRepositoryGraph(
            project_id="product-alias",
            repositories=(
                RepositoryRef("repo-a", "GitHub", "Owner/Shared-Repo", "main"),
                RepositoryRef("repo-b", "github", "owner/shared-repo/", "main"),
            ),
            components=(
                ProductComponent("a", "repo-a", ("src/API",)),
                ProductComponent("b", "repo-b", ("src/api",)),
            ),
        )

    graph = ProductRepositoryGraph(
        project_id="product-path-case",
        repositories=(
            RepositoryRef("repo", "github", "Owner/Shared-Repo", "main", case_sensitive_paths=True),
        ),
        components=(
            ProductComponent("upper", "repo", ("src/API",)),
            ProductComponent("lower", "repo", ("src/api",)),
        ),
    )
    assert graph.dependency_order() == ("lower", "upper")


def test_restore_rejects_uniform_permission_base_and_goal_rebinding() -> None:
    graph, coordinator, snapshot = _planned()
    attacks = (
        lambda request: replace(
            request,
            permission_ceiling=request.permission_ceiling | {"admin_project"},
        ),
        lambda request: replace(request, base_sha=SHA_C),
        lambda request: replace(request, goal=f"{request.goal} hidden rewrite"),
    )
    for attack in attacks:
        forged = _replace_requests(snapshot, attack)
        with pytest.raises(CoordinatorError, match="trusted plan"):
            ProductFactoryCoordinator(graph).restore(
                forged,
                trusted_plan_fingerprint=coordinator.trusted_plan_fingerprint,
            )


def test_restore_rejects_attempt_one_sibling_base_split() -> None:
    graph, coordinator, snapshot = _planned()
    forged = replace(
        snapshot,
        revision=snapshot.revision + 1,
        records=tuple(
            replace(record, request=replace(record.request, base_sha=SHA_C))
            if record.request.component_id == "core"
            else record
            for record in snapshot.records
        ),
    )
    with pytest.raises(CoordinatorError, match="trusted plan"):
        ProductFactoryCoordinator(graph).restore(
            forged,
            trusted_plan_fingerprint=coordinator.trusted_plan_fingerprint,
        )


def test_legitimate_attempt_two_repair_can_advance_one_component_base_and_restart() -> None:
    graph, coordinator, _snapshot = _planned()
    request = coordinator.start("core")
    coordinator.record_result(
        WorkerResultEnvelope(
            request.work_id,
            request.component_id,
            request.repository_id,
            request.base_sha,
            SHA_B,
            DIGEST,
            CodingResult(
                job_id=request.work_id,
                failure=WorkerFailure(
                    WorkerFailureKind.PROCESS_FAILED,
                    "tests failed",
                    retryable=True,
                ),
            ),
        )
    )
    repair = coordinator.prepare_repair(
        "core",
        base_sha=SHA_C,
        reason="repair after deterministic failure",
    )
    assert repair.attempt == 2
    assert repair.base_sha == SHA_C

    snapshot = coordinator.snapshot()
    by_component = {record.request.component_id: record.request for record in snapshot.records}
    assert by_component["core"].base_sha == SHA_C
    assert by_component["docs"].base_sha == SHA_A

    restored = ProductFactoryCoordinator(graph)
    restored.restore(
        snapshot,
        trusted_plan_fingerprint=coordinator.trusted_plan_fingerprint,
    )
    assert restored.snapshot() == snapshot


def test_reconciliation_preserves_exact_n_way_conflict_coverage() -> None:
    graph = ProductRepositoryGraph(
        project_id="product-n-way",
        repositories=(RepositoryRef("repo", "github", "owner/repo", "main"),),
        components=(ProductComponent("api", "repo", ("src/api",)),),
    )
    candidate = OwnershipLease("candidate", "worker-c", ("api",), ("src/api",))
    active_a = OwnershipLease("active-a", "worker-a", ("api",), ("src/api/routes",))
    active_b = OwnershipLease("active-b", "worker-b", ("api",), ("src/api/models",))
    decision = IntegrationDecision(
        "decision",
        IntegrationDecisionKind.RECONCILE,
        ("candidate", "active-a", "active-b"),
        "reconcile every actual conflict",
        ("evidence:compare",),
    )
    assessment = graph.assess_lease(candidate, (active_a, active_b), decision=decision)
    assert assessment.decision == decision
    assert {item.active_lease_id for item in assessment.conflicts} == {"active-a", "active-b"}
