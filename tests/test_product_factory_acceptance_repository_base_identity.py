from __future__ import annotations

from dataclasses import replace

import pytest

from nika_core.product_factory_coordinator import (
    CoordinatorError,
    CoordinatorSnapshot,
    ProductFactoryCoordinator,
    WorkerResultEnvelope,
)
from nika_core.product_factory_orchestration import (
    ProductComponent,
    ProductRepositoryGraph,
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
        project_id="product-repository-base",
        repositories=(
            RepositoryRef("repo-main", "github", "owner/product", "main"),
        ),
        components=(
            ProductComponent(
                "core",
                "repo-main",
                ("src/core",),
                test_commands=(("python", "-m", "pytest", "tests/core"),),
            ),
            ProductComponent(
                "docs",
                "repo-main",
                ("docs/product",),
                test_commands=(("python", "-m", "pytest", "tests/docs"),),
            ),
        ),
    )


def _planned() -> tuple[
    ProductRepositoryGraph,
    ProductFactoryCoordinator,
    CoordinatorSnapshot,
]:
    graph = _graph()
    coordinator = ProductFactoryCoordinator(graph)
    snapshot = coordinator.plan(
        base_shas={"repo-main": SHA_A},
        goals={"core": "Implement core", "docs": "Write product docs"},
        permission_ceiling=PERMISSIONS,
    )
    return graph, coordinator, snapshot


def test_pf12_restore_rejects_split_attempt_one_base_sha_within_repository() -> None:
    """One repository cannot restart from two different initial source identities."""
    graph, _coordinator, snapshot = _planned()
    records = tuple(
        replace(
            record,
            request=replace(record.request, base_sha=SHA_C),
        )
        if record.request.component_id == "core"
        else record
        for record in snapshot.records
    )
    forged = CoordinatorSnapshot(
        snapshot.project_id,
        snapshot.revision + 1,
        records,
    )

    with pytest.raises(CoordinatorError):
        ProductFactoryCoordinator(graph).restore(forged)


def test_pf12_restore_allows_later_repair_attempt_to_use_new_repository_base() -> None:
    """A real component repair may advance its base without rewriting sibling history."""
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
        reason="repair after reviewed failure",
    )
    assert repair.attempt == 2
    assert repair.base_sha == SHA_C

    snapshot = coordinator.snapshot()
    requests = {record.request.component_id: record.request for record in snapshot.records}
    assert requests["core"].base_sha == SHA_C
    assert requests["docs"].base_sha == SHA_A

    restored = ProductFactoryCoordinator(graph)
    restored.restore(snapshot)
    assert restored.snapshot() == snapshot
