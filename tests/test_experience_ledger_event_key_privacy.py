from __future__ import annotations

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.experience_ledger import (
    ContinuityKind,
    ContinuityOutcome,
    ExperienceLedger,
)


class _EventKeySubclass(str):
    pass


@pytest.mark.parametrize(
    ("event_key", "expected_error"),
    (
        ("https://example.test/private/token-secret", ValueError),
        ("Authorization:Bearer-token-secret", ValueError),
        ("Cookie:session-token-secret", ValueError),
        ("task-12\nCookie:session-token-secret", ValueError),
        ("task-12:recovery:\tsecret", ValueError),
        ("task-12::recovery:1", ValueError),
        (_EventKeySubclass("task-12:recovery:1"), TypeError),
    ),
)
def test_event_key_rejects_sensitive_or_noncanonical_text_before_persistence(
    tmp_path, event_key: object, expected_error: type[Exception]
) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    ledger = ExperienceLedger(store)
    canonical_key = "task-12:recovery:1"

    with pytest.raises(expected_error, match="event_key"):
        ledger.record(
            event_key=event_key,  # type: ignore[arg-type]
            task_id="task-12",
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
        event_key=canonical_key,
        task_id="task-12",
        kind=ContinuityKind.RECOVERY,
        outcome=ContinuityOutcome.PRESERVED,
        reason_code="checkpoint_verified",
    )
    assert accepted.event_key == canonical_key
    assert ledger.get(canonical_key) == accepted
