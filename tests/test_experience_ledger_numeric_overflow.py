from __future__ import annotations

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.experience_ledger import ContinuityKind, ContinuityOutcome, ExperienceLedger


def test_pathological_numeric_evidence_fails_closed_before_persistence(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "Користувач Ніка" / "numeric overflow.db")
    store.initialize()
    ledger = ExperienceLedger(store)
    huge_integer = 10**10000

    with pytest.raises(ValueError, match="finite and non-negative"):
        ledger.record(
            event_key="task-overflow:delay",
            task_id="task-overflow",
            kind=ContinuityKind.RECOVERY,
            outcome=ContinuityOutcome.WAITING,
            reason_code="retry_pending",
            delay_seconds=huge_integer,
        )

    with pytest.raises(ValueError, match="finite and non-negative"):
        ledger.record(
            event_key="task-overflow:clock",
            task_id="task-overflow",
            kind=ContinuityKind.HIBERNATE,
            outcome=ContinuityOutcome.PRESERVED,
            reason_code="wake_reconciled",
            clock_jump_seconds=huge_integer,
        )

    assert ledger.get("task-overflow:delay") is None
    assert ledger.get("task-overflow:clock") is None

@pytest.mark.parametrize("field", ("delay_seconds", "clock_jump_seconds"))
def test_non_lossless_integer_evidence_fails_closed_before_persistence(
    tmp_path,
    field: str,
) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    ledger = ExperienceLedger(store)
    kwargs: dict[str, object] = {
        "event_key": f"task-overflow:lossy:{field}",
        "task_id": "task-overflow",
        "kind": ContinuityKind.RECOVERY,
        "outcome": ContinuityOutcome.WAITING,
        "reason_code": "retry_pending",
    }
    kwargs[field] = (2**53) + 1

    with pytest.raises(ValueError, match="exactly representable"):
        ledger.record(**kwargs)  # type: ignore[arg-type]

    with store.connection() as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS event_count FROM continuity_experience_events"
        ).fetchone()["event_count"]
    assert count == 0


class _BehavioralInt(int):
    def __float__(self) -> float:
        raise AssertionError("numeric behavior must not run before exact-type rejection")


class _BehavioralFloat(float):
    def __float__(self) -> float:
        raise AssertionError("numeric behavior must not run before exact-type rejection")


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("delay_seconds", _BehavioralInt(3)),
        ("clock_jump_seconds", _BehavioralFloat(3.0)),
    ),
)
def test_behavioral_numeric_subclasses_fail_closed_before_persistence(
    tmp_path,
    field: str,
    value: object,
) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    ledger = ExperienceLedger(store)
    kwargs: dict[str, object] = {
        "event_key": "task-overflow:behavioral",
        "task_id": "task-overflow",
        "kind": ContinuityKind.RECOVERY,
        "outcome": ContinuityOutcome.WAITING,
        "reason_code": "retry_pending",
    }
    kwargs[field] = value

    with pytest.raises(TypeError, match="int or float"):
        ledger.record(**kwargs)  # type: ignore[arg-type]

    with store.connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS event_count FROM continuity_experience_events"
        ).fetchone()
    assert row is not None
    assert row["event_count"] == 0

