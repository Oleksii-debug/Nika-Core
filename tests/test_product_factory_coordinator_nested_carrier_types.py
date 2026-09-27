from typing import Any

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
from nika_core.toolsmith.contracts import (
    CodingResult,
    TestEvidence,
    WorkerFailure,
    WorkerFailureKind,
)

SHA_A = "a" * 40
SHA_B = "b" * 40
DIFF_DIGEST = "d" * 64
PERMISSIONS = frozenset({"read_source", "write_source", "run_tests"})


class _HostileHex(str):
    def __eq__(self, _other: object) -> bool:
        return True


class _SuccessForgingCodingResult(CodingResult):
    @property
    def succeeded(self) -> bool:
        return True


def _envelope(**overrides: Any) -> WorkerResultEnvelope:
    values: dict[str, Any] = {
        "work_id": "work-1",
        "component_id": "core",
        "repository_id": "repo-1",
        "base_sha": SHA_A,
        "result_sha": SHA_B,
        "diff_digest": DIFF_DIGEST,
        "coding_result": CodingResult(job_id="work-1"),
        "producer_actor_id": "worker:builder",
    }
    values.update(overrides)
    return WorkerResultEnvelope(**values)


def test_worker_result_rejects_hostile_stale_base_sha_subclass() -> None:
    hostile = _HostileHex("f" * 40)
    assert hostile == SHA_A

    with pytest.raises(CoordinatorError, match="base_sha must be a 40-character hexadecimal SHA"):
        _envelope(base_sha=hostile)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        (
            "result_sha",
            _HostileHex("b" * 40),
            "result_sha must be a 40-character hexadecimal SHA",
        ),
        (
            "diff_digest",
            _HostileHex("d" * 64),
            "diff_digest must be a 64-character hexadecimal digest",
        ),
    ),
)
def test_worker_result_rejects_string_subclass_result_authority_carriers(
    field: str,
    value: str,
    message: str,
) -> None:
    with pytest.raises(CoordinatorError, match=message):
        _envelope(**{field: value})


def test_worker_result_rejects_coding_result_subclass_success_override() -> None:
    forged = _SuccessForgingCodingResult(
        job_id="work-1",
        test_evidence=(TestEvidence(("pytest",), 0, "passing-evidence"),),
        failure=WorkerFailure(WorkerFailureKind.INTERNAL_ERROR, "real worker failure"),
    )
    assert forged.failure is not None
    assert forged.succeeded is True

    with pytest.raises(CoordinatorError, match="coding_result must be exact CodingResult"):
        _envelope(coding_result=forged)

def _running_coordinator():
    graph = ProductRepositoryGraph(
        project_id="project-1",
        repositories=(RepositoryRef("repo-1", "github", "org/repo", "main"),),
        components=(
            ProductComponent(
                component_id="core",
                repository_id="repo-1",
                paths=("src/core",),
                test_commands=(("pytest",),),
            ),
        ),
    )
    coordinator = ProductFactoryCoordinator(graph)
    coordinator.plan(
        base_shas={"repo-1": SHA_A},
        goals={"core": "build core"},
        permission_ceiling=PERMISSIONS,
    )
    request = coordinator.start("core")
    return coordinator, request


def _request_envelope(request, coding_result: CodingResult) -> WorkerResultEnvelope:
    return WorkerResultEnvelope(
        work_id=request.work_id,
        component_id=request.component_id,
        repository_id=request.repository_id,
        base_sha=request.base_sha,
        result_sha=SHA_B,
        diff_digest=DIFF_DIGEST,
        coding_result=coding_result,
        producer_actor_id="worker:builder",
    )


def test_record_result_rejects_forged_nested_failure_before_state_mutation() -> None:
    coordinator, request = _running_coordinator()
    failure = WorkerFailure(WorkerFailureKind.INTERNAL_ERROR, "worker failed")
    object.__setattr__(failure, "message", "")
    envelope = _request_envelope(
        request,
        CodingResult(job_id=request.work_id, failure=failure),
    )

    with pytest.raises(CoordinatorError, match="invalid failure evidence"):
        coordinator.record_result(envelope)

    record = coordinator.snapshot().records[0]
    assert record.state is WorkState.RUNNING
    assert record.result is None
    assert record.blocker is None


def test_record_result_rejects_forged_nested_test_evidence_before_state_mutation() -> None:
    coordinator, request = _running_coordinator()
    evidence = TestEvidence(("pytest",), 0, "tests-ok")
    object.__setattr__(evidence, "output_digest", "")
    envelope = _request_envelope(
        request,
        CodingResult(job_id=request.work_id, test_evidence=(evidence,)),
    )

    with pytest.raises(CoordinatorError, match="invalid test evidence"):
        coordinator.record_result(envelope)

    record = coordinator.snapshot().records[0]
    assert record.state is WorkState.RUNNING
    assert record.result is None


def test_record_result_snapshots_worker_evidence_before_storing_state() -> None:
    coordinator, request = _running_coordinator()
    evidence = TestEvidence(("pytest",), 0, "tests-ok")
    result = CodingResult(job_id=request.work_id, test_evidence=(evidence,))
    envelope = _request_envelope(request, result)

    recorded = coordinator.record_result(envelope)

    assert recorded.state is WorkState.REVIEW_REQUIRED
    assert recorded.result is not None
    assert recorded.result is not envelope
    assert recorded.result.coding_result is not result
    assert recorded.result.coding_result.test_evidence[0] is not evidence

    object.__setattr__(evidence, "output_digest", "")
    object.__setattr__(envelope, "diff_digest", "forged")

    persisted = coordinator.snapshot().records[0]
    assert persisted.result is not None
    assert persisted.result.diff_digest == DIFF_DIGEST
    assert persisted.result.coding_result.test_evidence[0].output_digest == "tests-ok"

