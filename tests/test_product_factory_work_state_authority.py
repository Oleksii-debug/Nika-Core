from dataclasses import replace

import pytest

from nika_core.product_factory_coordinator import (
    CoordinatorError,
    ProductFactoryCoordinator,
    WorkRecord,
)
from tests.test_product_factory_work_lifecycle import _coordinator, _core_record, _graph


@pytest.mark.parametrize("state", ("planned", "ready", "accepted", "done", "cancelled"))
def test_work_record_state_requires_exact_enum(state: str) -> None:
    record = _core_record(_coordinator())

    with pytest.raises(CoordinatorError, match="work state must be an exact WorkState"):
        replace(record, state=state)


def _forge_work_record_state(record: WorkRecord, state: object) -> WorkRecord:
    forged = object.__new__(WorkRecord)
    for field_name in ("request", "state", "result", "review", "blocker"):
        object.__setattr__(
            forged,
            field_name,
            state if field_name == "state" else getattr(record, field_name),
        )
    return forged


@pytest.mark.parametrize("state", ("ready", "accepted", "done"))
def test_restore_rejects_forged_strenum_equal_work_state(state: str) -> None:
    coordinator = _coordinator()
    snapshot = coordinator.snapshot()
    records = tuple(
        _forge_work_record_state(record, state)
        if record.request.component_id == "core"
        else record
        for record in snapshot.records
    )
    tampered = replace(snapshot, records=records)

    restored = ProductFactoryCoordinator(_graph())
    with pytest.raises(
        CoordinatorError,
        match="snapshot work state must be an exact WorkState",
    ):
        restored.restore(
            tampered,
            trusted_plan_fingerprint=coordinator.trusted_plan_fingerprint,
        )
