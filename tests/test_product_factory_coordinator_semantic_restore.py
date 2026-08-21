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
from nika_core.toolsmith.contracts import (
    CodingResult,
    TestEvidence,
    WorkerFailure,
    WorkerFailureKind,
)

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


def _failed_envelope(request) -> WorkerResultEnvelope:
    return WorkerResultEnvelope(
        work_id=request.work_id,
        component_id=request.component_id,
        repository_id=request.repository_id,
        base_sha=request.base_sha,
        result_sha=SHA_B,
        diff_digest=DIGEST,
        coding_result=CodingResult(
            job_id=request.work_id,
            failure=WorkerFailure(
                WorkerFailureKind.PROCESS_FAILED,
                "acceptance failed",
                retryable=True,
            ),
        ),
    )


def _records(snapshot: CoordinatorSnapshot) -> dict[str, WorkRecord]:
    return {record.request.component_id: record for record in snapshot.records}


def test_restore_rejects_forged_accepted_without_result_or_review() -> None:
    coordinator = _planned()
    snapshot = coordinator.snapshot()
    records = _records(snapshot)
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
    records = _records(snapshot)
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
    records = _records(snapshot)
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
    records = _records(snapshot)
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
    records = _records(snapshot)
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


def test_restore_revalidates_acceptance_matrix_inside_accepted_record() -> None:
    coordinator = _planned()
    request = coordinator.start("core")
    incomplete_result = _successful_envelope(
        request,
        (request.acceptance_commands[0],),
    )
    snapshot = coordinator.snapshot()
    records = _records(snapshot)
    forged_core = WorkRecord(
        request,
        WorkState.ACCEPTED,
        incomplete_result,
        ReviewDecision("qa", True, "forged acceptance", ("evidence:forged",)),
    )
    forged = CoordinatorSnapshot(
        snapshot.project_id,
        snapshot.revision + 1,
        (forged_core, records["ui"]),
    )

    with pytest.raises(CoordinatorError, match="every declared acceptance command"):
        ProductFactoryCoordinator(_graph()).restore(forged)


def test_restore_rejects_failed_worker_result_hidden_under_accepted_review() -> None:
    coordinator = _planned()
    request = coordinator.start("core")
    snapshot = coordinator.snapshot()
    records = _records(snapshot)
    forged_core = WorkRecord(
        request,
        WorkState.ACCEPTED,
        _failed_envelope(request),
        ReviewDecision("qa", True, "forged acceptance", ("evidence:forged",)),
    )
    forged = CoordinatorSnapshot(
        snapshot.project_id,
        snapshot.revision + 1,
        (forged_core, records["ui"]),
    )

    with pytest.raises(CoordinatorError, match="successful result"):
        ProductFactoryCoordinator(_graph()).restore(forged)


def test_restore_rejects_work_id_that_does_not_match_component_attempt() -> None:
    coordinator = _planned()
    snapshot = coordinator.snapshot()
    records = _records(snapshot)
    core = records["core"]
    forged = CoordinatorSnapshot(
        snapshot.project_id,
        snapshot.revision + 1,
        (
            WorkRecord(
                replace(core.request, work_id="work-" + "f" * 24),
                core.state,
            ),
            records["ui"],
        ),
    )

    with pytest.raises(CoordinatorError, match="work id"):
        ProductFactoryCoordinator(_graph()).restore(forged)


def test_restore_rejects_ready_dependency_before_parent_acceptance() -> None:
    coordinator = _planned()
    snapshot = coordinator.snapshot()
    records = _records(snapshot)
    forged = CoordinatorSnapshot(
        snapshot.project_id,
        snapshot.revision + 1,
        (
            records["core"],
            WorkRecord(records["ui"].request, WorkState.READY),
        ),
    )

    with pytest.raises(CoordinatorError, match="dependency acceptance"):
        ProductFactoryCoordinator(_graph()).restore(forged)


def test_single_declared_pytest_accepts_equivalent_full_suite_evidence() -> None:
    graph = ProductRepositoryGraph(
        project_id="single-product",
        repositories=(RepositoryRef("repo", "github", "owner/single", "main"),),
        components=(
            ProductComponent(
                "core",
                "repo",
                ("src/core",),
                test_commands=(("python", "-m", "pytest", "tests/core"),),
            ),
        ),
    )
    coordinator = ProductFactoryCoordinator(graph)
    coordinator.plan(
        base_shas={"repo": SHA_A},
        goals={"core": "Implement core"},
        permission_ceiling=PERMISSIONS,
    )
    request = coordinator.start("core")
    envelope = WorkerResultEnvelope(
        request.work_id,
        request.component_id,
        request.repository_id,
        request.base_sha,
        SHA_B,
        DIGEST,
        CodingResult(
            job_id=request.work_id,
            test_evidence=(TestEvidence(("pytest",), 0, "full-suite"),),
        ),
    )

    record = coordinator.record_result(envelope)

    assert record.state is WorkState.REVIEW_REQUIRED


def test_one_full_suite_evidence_cannot_cover_two_declared_commands() -> None:
    coordinator = _planned()
    request = coordinator.start("core")

    with pytest.raises(CoordinatorError, match="every declared acceptance command"):
        coordinator.record_result(
            _successful_envelope(request, (("pytest",),))
        )
