from dataclasses import replace

import pytest

from nika_core.product_factory_coordinator import (
    CoordinatorError,
    ProductFactoryCoordinator,
    WorkerResultEnvelope,
    WorkState,
)
from nika_core.toolsmith.contracts import CodingResult, WorkerFailure, WorkerFailureKind
from tests.test_product_factory_coordinator import DIGEST, SHA_B
from tests.test_product_factory_work_lifecycle import _coordinator, _core_record, _graph


class _HostileText(str):
    pass


def _failed_result(request, message: str) -> WorkerResultEnvelope:
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
                message,
                retryable=True,
            ),
        ),
    )


@pytest.mark.parametrize(
    "message",
    (
        " failed",
        "failed ",
        "failed\nforged",
        "failed\tforged",
        "é" * 2049,
        _HostileText("failed"),
    ),
)
def test_worker_failure_message_rejects_noncanonical_durable_text_without_mutation(
    message: str,
) -> None:
    coordinator = _coordinator()
    request = coordinator.start("core")
    before = coordinator.snapshot()

    with pytest.raises(
        CoordinatorError,
        match="worker failure message must be canonical single-line text",
    ):
        coordinator.record_result(_failed_result(request, message))

    assert coordinator.snapshot() == before


def test_worker_failure_message_utf8_boundary_round_trips_and_binds_blocker() -> None:
    coordinator = _coordinator()
    request = coordinator.start("core")
    message = "é" * 2048

    record = coordinator.record_result(_failed_result(request, message))
    assert record.state is WorkState.REPAIR_REQUIRED
    assert record.blocker == message
    assert record.result is not None
    assert record.result.coding_result.failure is not None
    assert record.result.coding_result.failure.message == message

    snapshot = coordinator.snapshot()
    restored = ProductFactoryCoordinator(_graph())
    restored.restore(
        snapshot,
        trusted_plan_fingerprint=coordinator.trusted_plan_fingerprint,
    )
    restored_record = _core_record(restored)
    assert restored_record.state is WorkState.REPAIR_REQUIRED
    assert restored_record.blocker == message
    assert restored_record.result is not None
    assert restored_record.result.coding_result.failure is not None
    assert restored_record.result.coding_result.failure.message == message


def test_restore_rejects_worker_failure_blocker_that_does_not_match_failure_evidence() -> None:
    coordinator = _coordinator()
    request = coordinator.start("core")
    coordinator.record_result(_failed_result(request, "tests failed"))
    snapshot = coordinator.snapshot()
    records = tuple(
        replace(record, blocker="different failure")
        if record.request.component_id == "core"
        else record
        for record in snapshot.records
    )

    restored = ProductFactoryCoordinator(_graph())
    with pytest.raises(
        CoordinatorError,
        match="worker-failed repair blocker does not match failure evidence",
    ):
        restored.restore(
            replace(snapshot, records=records),
            trusted_plan_fingerprint=coordinator.trusted_plan_fingerprint,
        )


def test_restore_revalidates_worker_failure_message_before_accepting_durable_state() -> None:
    coordinator = _coordinator()
    request = coordinator.start("core")
    coordinator.record_result(_failed_result(request, "tests failed"))
    snapshot = coordinator.snapshot()
    core = _core_record(coordinator)
    assert core.result is not None
    assert core.result.coding_result.failure is not None

    forged_failure = replace(
        core.result.coding_result.failure,
        message=" forged failure",
    )
    forged_result = replace(
        core.result,
        coding_result=replace(core.result.coding_result, failure=forged_failure),
    )
    records = tuple(
        replace(record, result=forged_result, blocker=" forged failure")
        if record.request.component_id == "core"
        else record
        for record in snapshot.records
    )

    restored = ProductFactoryCoordinator(_graph())
    with pytest.raises(
        CoordinatorError,
        match="worker failure message must be canonical single-line text",
    ):
        restored.restore(
            replace(snapshot, records=records),
            trusted_plan_fingerprint=coordinator.trusted_plan_fingerprint,
        )


def test_cancelled_worker_failure_round_trips_with_canonical_failure_evidence() -> None:
    coordinator = _coordinator()
    request = coordinator.start("core")
    coordinator.record_result(_failed_result(request, "tests failed"))

    cancelled = coordinator.cancel("core", reason="scope removed")
    assert cancelled.state is WorkState.CANCELLED
    assert cancelled.blocker == "scope removed"
    assert cancelled.result is not None
    assert cancelled.result.coding_result.failure is not None
    assert cancelled.result.coding_result.failure.message == "tests failed"

    snapshot = coordinator.snapshot()
    restored = ProductFactoryCoordinator(_graph())
    restored.restore(
        snapshot,
        trusted_plan_fingerprint=coordinator.trusted_plan_fingerprint,
    )

    restored_record = _core_record(restored)
    assert restored_record.state is WorkState.CANCELLED
    assert restored_record.blocker == "scope removed"
    assert restored_record.result is not None
    assert restored_record.result.coding_result.failure is not None
    assert restored_record.result.coding_result.failure.message == "tests failed"


def test_restore_revalidates_failed_result_retained_by_cancelled_state() -> None:
    coordinator = _coordinator()
    request = coordinator.start("core")
    coordinator.record_result(_failed_result(request, "tests failed"))
    coordinator.cancel("core", reason="scope removed")
    snapshot = coordinator.snapshot()

    core = _core_record(coordinator)
    assert core.result is not None
    assert core.result.coding_result.failure is not None
    forged_failure = replace(
        core.result.coding_result.failure,
        message=" forged\nmessage",
    )
    forged_result = replace(
        core.result,
        coding_result=replace(core.result.coding_result, failure=forged_failure),
    )
    records = tuple(
        replace(record, result=forged_result)
        if record.request.component_id == "core"
        else record
        for record in snapshot.records
    )

    restored = ProductFactoryCoordinator(_graph())
    with pytest.raises(
        CoordinatorError,
        match="worker failure message must be canonical single-line text",
    ):
        restored.restore(
            replace(snapshot, records=records),
            trusted_plan_fingerprint=coordinator.trusted_plan_fingerprint,
        )