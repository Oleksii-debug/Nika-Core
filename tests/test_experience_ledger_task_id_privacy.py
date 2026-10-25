from __future__ import annotations

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.experience_ledger import (
    ContinuityKind,
    ContinuityOutcome,
    ExperienceLedger,
)


class _TaskIdSubclass(str):
    pass


@pytest.mark.parametrize(
    ("task_id", "expected_error"),
    (
        ("Authorization: Bearer canary-secret", ValueError),
        ("task-11\nCookie: session=canary-secret", ValueError),
        ("https://example.test/result?token=canary-secret", ValueError),
        ("task-11\x00secret", ValueError),
        (_TaskIdSubclass("task-11"), TypeError),
    ),
)
def test_task_id_rejects_sensitive_or_noncanonical_text_before_persistence(
    tmp_path, task_id: object, expected_error: type[Exception]
) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    ledger = ExperienceLedger(store)
    event_key = "task-11:recovery:task-id-privacy"

    with pytest.raises(expected_error, match="task_id"):
        ledger.record(
            event_key=event_key,
            task_id=task_id,  # type: ignore[arg-type]
            kind=ContinuityKind.RECOVERY,
            outcome=ContinuityOutcome.PRESERVED,
            reason_code="checkpoint_verified",
        )

    with store.connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS event_count FROM continuity_experience_events"
        ).fetchone()
    assert row is not None
    assert row["event_count"] == 0

    accepted = ledger.record(
        event_key=event_key,
        task_id="task-11",
        kind=ContinuityKind.RECOVERY,
        outcome=ContinuityOutcome.PRESERVED,
        reason_code="checkpoint_verified",
    )
    assert accepted.task_id == "task-11"
    assert ledger.list_for_task("task-11") == (accepted,)
