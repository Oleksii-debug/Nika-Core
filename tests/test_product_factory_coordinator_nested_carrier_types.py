from typing import Any

import pytest

from nika_core.product_factory_coordinator import CoordinatorError, WorkerResultEnvelope
from nika_core.toolsmith.contracts import (
    CodingResult,
    TestEvidence,
    WorkerFailure,
    WorkerFailureKind,
)

SHA_A = "a" * 40
SHA_B = "b" * 40
DIFF_DIGEST = "d" * 64


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
