from __future__ import annotations

from dataclasses import replace

import pytest

from nika_core.product_factory_coordinator import (
    CoordinatorError,
    CoordinatorSnapshot,
    ProductFactoryCoordinator,
    ReviewDecision,
    WorkerResultEnvelope,
    WorkRecord,
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


def _graph() -> ProductRepositoryGraph:
    return ProductRepositoryGraph(
        project_id="product-1",
        repositories=(RepositoryRef("repo-main", "github", "owner/product", "main"),),
        components=(
            ProductComponent(
                "core",
                "repo-main",
                ("src/core",),
                test_commands=(
                    ("python", "-m", "pytest", "tests/core"),
                    ("python", "-m", "pytest", "tests/integration"),
                ),
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


def _planned() -> ProductFactoryCoordinator:
    graph = _graph()
    coordinator = ProductFactoryCoordinator(graph)
    coordinator.plan(
        base_shas={"repo-main": SHA_A},
        goals={"core": "Implement core", "ui": "Implement ui"},
        permission_ceiling=PERMISSIONS,
    )
    return coordinator


def _successful_envelope(request, commands) -> WorkerResultEnvelope:
    return WorkerResultEnvelope(
        work_id=request.work_id,
        component_id=request.component_id,
        repository_id=request.repository_id,
        base_sha=request.base_sha,
        result_sha=SHA_B,
        diff_digest=DIGEST,
        coding_result=CodingResult(
            job_id=request.work_id,
            test_evidence=tuple(
                TestEvidence(command, 0, f"evidence-{index}")
                for index, command in enumerate(commands, start=1)
            ),
        ),
    )


def test_restore_rejects_forged_accepted_without_result_or_review() -> None:
    coordinator = _planned()
    snapshot = coordinator.snapshot()
    records = {record.request.component_id: record for record in snapshot.records}
    forged_core = WorkRecord(records["core"].request, WorkState.ACCEPTED)
    forged = CoordinatorSnapshot(
        snapshot.project_id,
        snapshot.revision + 1,
        (forged_core, records["ui"]),
    )

    with pytest.raises(CoordinatorError):
        ProductFactoryCoordinator(_graph()).restore(forged)


def test_restore_rejects_request_repository_identity_drift() -> None:
    coordinator = _planned()
    snapshot = coordinator.snapshot()
    records = {record.request.component_id: record for record in snapshot.records}
    core = records["core"]
    forged = CoordinatorSnapshot(
        snapshot.project_id,
        snapshot.revision + 1,
        (
            WorkRecord(replace(core.request, repository_id="repo-other"), core.state),
            records["ui"],
        ),
    )

    with pytest.raises(CoordinatorError):
        ProductFactoryCoordinator(_graph()).restore(forged)


def test_restore_rejects_request_project_identity_drift() -> None:
    coordinator = _planned()
    snapshot = coordinator.snapshot()
    records = {record.request.component_id: record for record in snapshot.records}
    core = records["core"]
    forged = CoordinatorSnapshot(
        snapshot.project_id,
        snapshot.revision + 1,
        (
            WorkRecord(replace(core.request, project_id="product-other"), core.state),
            records["ui"],
        ),
    )

    with pytest.raises(CoordinatorError):
        ProductFactoryCoordinator(_graph()).restore(forged)


def test_restore_rejects_path_scope_expansion() -> None:
    coordinator = _planned()
    snapshot = coordinator.snapshot()
    records = {record.request.component_id: record for record in snapshot.records}
    core = records["core"]
    forged = CoordinatorSnapshot(
        snapshot.project_id,
        snapshot.revision + 1,
        (
            WorkRecord(replace(core.request, allowed_paths=("src",)), core.state),
            records["ui"],
        ),
    )

    with pytest.raises(CoordinatorError):
        ProductFactoryCoordinator(_graph()).restore(forged)


def test_restore_rejects_permission_ceiling_expansion() -> None:
    coordinator = _planned()
    snapshot = coordinator.snapshot()
    records = {record.request.component_id: record for record in snapshot.records}
    core = records["core"]
    forged = CoordinatorSnapshot(
        snapshot.project_id,
        snapshot.revision + 1,
        (
            WorkRecord(
                replace(
                    core.request,
                    permission_ceiling=core.request.permission_ceiling | {"admin_project"},
                ),
                core.state,
            ),
            records["ui"],
        ),
    )

    with pytest.raises(CoordinatorError):
        ProductFactoryCoordinator(_graph()).restore(forged)


def test_success_requires_every_declared_acceptance_command() -> None:
    coordinator = _planned()
    request = coordinator.start("core")

    with pytest.raises(CoordinatorError):
        coordinator.record_result(
            _successful_envelope(request, (request.acceptance_commands[0],))
        )


def test_unrelated_passing_command_cannot_substitute_for_acceptance() -> None:
    coordinator = _planned()
    request = coordinator.start("core")

    with pytest.raises(CoordinatorError):
        coordinator.record_result(
            _successful_envelope(request, (("python", "-c", "print('green')"),))
        )


def test_extra_passing_evidence_is_allowed_when_declared_matrix_is_complete() -> None:
    coordinator = _planned()
    request = coordinator.start("core")
    evidence = request.acceptance_commands + (("python", "-m", "pytest", "tests/extra"),)

    record = coordinator.record_result(_successful_envelope(request, evidence))

    assert record.state is WorkState.REVIEW_REQUIRED
    accepted = coordinator.review(
        "core",
        ReviewDecision("qa", True, "all checks passed", ("evidence:qa",)),
    )
    assert accepted.state is WorkState.ACCEPTED
