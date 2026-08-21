from __future__ import annotations

import pytest

from nika_core.product_factory_coordinator import (
    CoordinatorError,
    ProductFactoryCoordinator,
    WorkerResultEnvelope,
    WorkState,
)
from nika_core.product_factory_orchestration import (
    ProductComponent,
    ProductRepositoryGraph,
    RepositoryRef,
)
from nika_core.toolsmith.contracts import CodingResult, TestEvidence

SHA_A = "a" * 40
SHA_B = "b" * 40
DIGEST = "1" * 64
PERMISSIONS = frozenset({"read_source", "write_source", "run_tests"})
DECLARED = ("python", "-m", "pytest", "tests/component_000")


def _coordinator() -> ProductFactoryCoordinator:
    graph = ProductRepositoryGraph(
        project_id="product-command-identity",
        repositories=(RepositoryRef("repo-main", "github", "owner/product", "main"),),
        components=(
            ProductComponent(
                "component-000",
                "repo-main",
                ("src/component_000",),
                test_commands=(DECLARED,),
            ),
        ),
    )
    coordinator = ProductFactoryCoordinator(graph)
    coordinator.plan(
        base_shas={"repo-main": SHA_A},
        goals={"component-000": "Implement component"},
        permission_ceiling=PERMISSIONS,
    )
    return coordinator


def _envelope(request, command: tuple[str, ...]) -> WorkerResultEnvelope:
    return WorkerResultEnvelope(
        request.work_id,
        request.component_id,
        request.repository_id,
        request.base_sha,
        SHA_B,
        DIGEST,
        CodingResult(
            job_id=request.work_id,
            test_evidence=(TestEvidence(command, 0, "test-evidence"),),
        ),
    )


def test_pf4_component_identity_cannot_alias_a_different_pytest_target() -> None:
    """Component naming is not evidence that the declared filesystem target executed."""
    coordinator = _coordinator()
    request = coordinator.start("component-000")

    with pytest.raises(CoordinatorError):
        coordinator.record_result(
            _envelope(
                request,
                ("python", "-m", "pytest", "component-000"),
            )
        )


def test_pf4_safe_pytest_path_spelling_normalization_remains_accepted() -> None:
    """Pure path spelling normalization may not be confused with target substitution."""
    coordinator = _coordinator()
    request = coordinator.start("component-000")

    record = coordinator.record_result(
        _envelope(
            request,
            ("python", "-m", "pytest", "./tests/component_000"),
        )
    )

    assert record.state is WorkState.REVIEW_REQUIRED
