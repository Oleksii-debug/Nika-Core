from dataclasses import replace

import pytest

from nika_core.product_factory_coordinator import (
    CoordinatorError,
    ProductFactoryCoordinator,
    WorkState,
)
from tests.test_product_factory_work_lifecycle import _coordinator, _core_record, _graph


@pytest.mark.parametrize(
    "reason",
    (None, 1, "", " ", " leading", "trailing ", "line\nbreak", "tab\tbreak", "é" * 2049),
)
def test_cancel_reason_rejects_noncanonical_or_oversized_text(reason: object) -> None:
    coordinator = _coordinator()
    snapshot = coordinator.snapshot()

    with pytest.raises(CoordinatorError, match="cancellation reason must be canonical single-line text"):
        coordinator.cancel("core", reason=reason)  # type: ignore[arg-type]

    assert coordinator.snapshot() == snapshot


def test_cancel_reason_boundary_survives_restore_and_tampering_fails_closed() -> None:
    coordinator = _coordinator()
    reason = "x" * 4096
    cancelled = coordinator.cancel("core", reason=reason)
    assert cancelled.state is WorkState.CANCELLED
    assert cancelled.blocker == reason

    snapshot = coordinator.snapshot()
    restored = ProductFactoryCoordinator(_graph())
    restored.restore(snapshot, trusted_plan_fingerprint=coordinator.trusted_plan_fingerprint)
    assert _core_record(restored).blocker == reason

    tampered_records = tuple(
        replace(record, blocker="é" * 2049)
        if record.request.component_id == "core"
        else record
        for record in snapshot.records
    )
    tampered = replace(snapshot, records=tampered_records)
    rejected = ProductFactoryCoordinator(_graph())
    with pytest.raises(CoordinatorError, match="cancellation reason must be canonical single-line text"):
        rejected.restore(tampered, trusted_plan_fingerprint=coordinator.trusted_plan_fingerprint)


def test_blocked_work_cannot_be_cancelled_without_erasing_blocker_provenance() -> None:
    coordinator = _coordinator()
    blocked = coordinator.block("core", "upstream unavailable")
    assert blocked.state is WorkState.BLOCKED
    snapshot = coordinator.snapshot()

    with pytest.raises(CoordinatorError, match="blocked component cannot be cancelled"):
        coordinator.cancel("core", reason="scope changed")

    assert coordinator.snapshot() == snapshot
    restored = ProductFactoryCoordinator(_graph())
    restored.restore(snapshot, trusted_plan_fingerprint=coordinator.trusted_plan_fingerprint)
    restored_record = _core_record(restored)
    assert restored_record.state is WorkState.BLOCKED
    assert restored_record.blocker == "upstream unavailable"
