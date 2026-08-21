from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest

from nika_core.product_factory_coordinator import (
    ComponentWorkRequest,
    CoordinatorError,
    ProductFactoryCoordinator,
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
        project_id="pf2-trusted-plan-authority",
        repositories=(RepositoryRef("repo-main", "github", "owner/product", "main"),),
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
                test_commands=(("python", "-m", "pytest", "tests/ui"),),
            ),
        ),
    )


def _recomputed_work_id(request: ComponentWorkRequest) -> str:
    """Independent reproduction of the candidate-controlled production framing."""
    parts = (
        request.project_id,
        request.component_id,
        request.repository_id,
        request.goal,
        request.base_sha,
        request.allowed_paths,
        tuple(sorted(request.permission_ceiling)),
        request.acceptance_commands,
        request.attempt,
    )
    payload = json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str)
    return f"work-{hashlib.sha256(payload.encode()).hexdigest()[:24]}"


def _planned():
    graph = _graph()
    coordinator = ProductFactoryCoordinator(graph)
    snapshot = coordinator.plan(
        base_shas={"repo-main": SHA_A},
        goals={"core": "Implement core", "ui": "Implement ui"},
        permission_ceiling=PERMISSIONS,
    )
    assert all(
        _recomputed_work_id(record.request) == record.request.work_id
        for record in snapshot.records
    ), "attacker framing must exactly reproduce legitimate production work IDs"
    return graph, snapshot


def _forge_request(request: ComponentWorkRequest, **changes: object) -> ComponentWorkRequest:
    forged = replace(request, **changes)
    return replace(forged, work_id=_recomputed_work_id(forged))


def _permission_expansion(request: ComponentWorkRequest) -> ComponentWorkRequest:
    return _forge_request(
        request,
        permission_ceiling=request.permission_ceiling | {"admin_project"},
    )


def _initial_base_rebind(request: ComponentWorkRequest) -> ComponentWorkRequest:
    return _forge_request(request, base_sha=SHA_C)


def _goal_rewrite(request: ComponentWorkRequest) -> ComponentWorkRequest:
    if request.component_id != "core":
        return request
    return _forge_request(request, goal="Ship an unrelated hidden objective")


def _sibling_base_split(request: ComponentWorkRequest) -> ComponentWorkRequest:
    if request.component_id != "core":
        return request
    return _forge_request(request, base_sha=SHA_C)


@pytest.mark.parametrize(
    "attack",
    (
        pytest.param(_permission_expansion, id="permission-expansion"),
        pytest.param(_initial_base_rebind, id="initial-base-rebind"),
        pytest.param(_goal_rewrite, id="goal-rewrite"),
        pytest.param(_sibling_base_split, id="sibling-base-split"),
    ),
)
def test_fresh_restore_rejects_plan_forgery_after_attacker_recomputes_work_ids(
    attack,
) -> None:
    graph, snapshot = _planned()
    forged = replace(
        snapshot,
        revision=snapshot.revision + 1,
        records=tuple(
            replace(record, request=attack(record.request))
            for record in snapshot.records
        ),
    )

    with pytest.raises(CoordinatorError):
        ProductFactoryCoordinator(graph).restore(forged)
