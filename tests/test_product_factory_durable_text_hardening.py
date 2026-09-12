from dataclasses import replace

import pytest

from nika_core.product_factory_coordinator import (
    CoordinatorError,
    ProductFactoryCoordinator,
    ReviewDecision,
    WorkState,
)
from tests.test_product_factory_coordinator import _success
from tests.test_product_factory_work_lifecycle import _coordinator, _core_record, _graph


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("reviewer_id", 1),
        ("reviewer_id", " qa-1"),
        ("reviewer_id", "qa-1 "),
        ("reviewer_id", "qa\nforged"),
        ("reviewer_id", "é" * 2049),
        ("reason", None),
        ("reason", " verified"),
        ("reason", "verified "),
        ("reason", "verified\tforged"),
        ("reason", "é" * 2049),
    ),
)
def test_review_identity_and_reason_require_bounded_canonical_text(field: str, value: object) -> None:
    values = {
        "reviewer_id": "qa-1",
        "accepted": True,
        "reason": "verified",
        "evidence_refs": ("ci:1",),
    }
    values[field] = value

    with pytest.raises(CoordinatorError, match="canonical single-line text"):
        ReviewDecision(**values)


def test_review_durable_text_utf8_boundary_round_trips() -> None:
    coordinator = _coordinator()
    request = coordinator.start("core")
    coordinator.record_result(_success(request))
    decision = ReviewDecision("é" * 2048, True, "x" * 4096, ("ci:1",))
    coordinator.review("core", decision)

    snapshot = coordinator.snapshot()
    restored = ProductFactoryCoordinator(_graph())
    restored.restore(snapshot, trusted_plan_fingerprint=coordinator.trusted_plan_fingerprint)
    assert _core_record(restored).review == decision


def test_restore_revalidates_durable_review_text() -> None:
    coordinator = _coordinator()
    request = coordinator.start("core")
    coordinator.record_result(_success(request))
    coordinator.review("core", ReviewDecision("qa-1", True, "verified", ("ci:1",)))
    snapshot = coordinator.snapshot()

    forged = object.__new__(ReviewDecision)
    object.__setattr__(forged, "reviewer_id", " qa-1")
    object.__setattr__(forged, "accepted", True)
    object.__setattr__(forged, "reason", "verified")
    object.__setattr__(forged, "evidence_refs", ("ci:1",))
    records = tuple(
        replace(record, review=forged)
        if record.request.component_id == "core"
        else record
        for record in snapshot.records
    )

    restored = ProductFactoryCoordinator(_graph())
    with pytest.raises(CoordinatorError, match="reviewer id must be canonical single-line text"):
        restored.restore(
            replace(snapshot, records=records),
            trusted_plan_fingerprint=coordinator.trusted_plan_fingerprint,
        )


@pytest.mark.parametrize(
    "reason",
    (1, None, " blocked", "blocked ", "blocked\nforged", "blocked\tforged", "é" * 2049),
)
def test_block_reason_rejects_malformed_or_oversize_text_without_mutation(reason: object) -> None:
    coordinator = _coordinator()
    snapshot = coordinator.snapshot()

    with pytest.raises(CoordinatorError, match="blocker reason must be canonical single-line text"):
        coordinator.block("core", reason)
    assert coordinator.snapshot() == snapshot


def test_block_reason_utf8_boundary_round_trips() -> None:
    coordinator = _coordinator()
    reason = "é" * 2048
    blocked = coordinator.block("core", reason)
    assert blocked.state is WorkState.BLOCKED
    assert blocked.blocker == reason

    snapshot = coordinator.snapshot()
    restored = ProductFactoryCoordinator(_graph())
    restored.restore(snapshot, trusted_plan_fingerprint=coordinator.trusted_plan_fingerprint)
    restored_record = _core_record(restored)
    assert restored_record.state is WorkState.BLOCKED
    assert restored_record.blocker == reason


def test_blocked_restore_rejects_noncanonical_tampered_reason() -> None:
    coordinator = _coordinator()
    coordinator.block("core", "upstream unavailable")
    snapshot = coordinator.snapshot()
    records = tuple(
        replace(record, blocker=" upstream unavailable")
        if record.request.component_id == "core"
        else record
        for record in snapshot.records
    )

    restored = ProductFactoryCoordinator(_graph())
    with pytest.raises(CoordinatorError, match="blocker reason must be canonical single-line text"):
        restored.restore(
            replace(snapshot, records=records),
            trusted_plan_fingerprint=coordinator.trusted_plan_fingerprint,
        )
