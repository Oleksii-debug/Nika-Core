from __future__ import annotations

from dataclasses import replace

import pytest

from nika_core.product_factory_coordinator import (
    CoordinatorError,
    CoordinatorSnapshot,
    ProductFactoryCoordinator,
    WorkRecord,
)
from nika_core.product_factory_orchestration import (
    ProductComponent,
    ProductRepositoryGraph,
    RepositoryRef,
)

SHA_A = "a" * 40
SHA_C = "c" * 40
PERMISSIONS = frozenset({"read_source", "write_source", "run_tests"})


def _graph() -> ProductRepositoryGraph:
    return ProductRepositoryGraph(
        project_id="product-restore-plan",
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
                "ui",
                "repo-main",
                ("src/ui",),
                dependencies=("core",),
                test_commands=(("python", "-m", "pytest", "tests/ui"),),
            ),
        ),
    )


def _planned() -> tuple[ProductRepositoryGraph, CoordinatorSnapshot]:
    graph = _graph()
    coordinator = ProductFactoryCoordinator(graph)
    snapshot = coordinator.plan(
        base_shas={"repo-main": SHA_A},
        goals={"core": "Implement core", "ui": "Implement ui"},
        permission_ceiling=PERMISSIONS,
    )
    return graph, snapshot


def _replace_requests(snapshot: CoordinatorSnapshot, transform) -> CoordinatorSnapshot:
    return CoordinatorSnapshot(
        snapshot.project_id,
        snapshot.revision + 1,
        tuple(
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


def test_pf12_restore_rejects_uniform_permission_ceiling_expansion() -> None:
    """A forged checkpoint may not expand every child permission ceiling in lockstep."""
    graph, snapshot = _planned()
    forged = _replace_requests(
        snapshot,
        lambda request: replace(
            request,
            permission_ceiling=request.permission_ceiling | {"admin_project"},
        ),
    )

    with pytest.raises(CoordinatorError):
        ProductFactoryCoordinator(graph).restore(forged)


def test_pf12_restore_rejects_uniform_initial_base_sha_rebinding() -> None:
    """Restart may not silently retarget an initial plan to a different repository base."""
    graph, snapshot = _planned()
    forged = _replace_requests(
        snapshot,
        lambda request: replace(request, base_sha=SHA_C),
    )

    with pytest.raises(CoordinatorError):
        ProductFactoryCoordinator(graph).restore(forged)


def test_pf12_restore_rejects_initial_component_goal_rewrite() -> None:
    """Attempt-one work goals are plan identity, not caller-editable serialized state."""
    graph, snapshot = _planned()
    records = []
    for record in snapshot.records:
        request = record.request
        if request.component_id == "core":
            request = replace(request, goal="Ship an unrelated hidden objective")
        records.append(
            WorkRecord(
                request,
                record.state,
                record.result,
                record.review,
                record.blocker,
            )
        )
    forged = CoordinatorSnapshot(
        snapshot.project_id,
        snapshot.revision + 1,
        tuple(records),
    )

    with pytest.raises(CoordinatorError):
        ProductFactoryCoordinator(graph).restore(forged)
