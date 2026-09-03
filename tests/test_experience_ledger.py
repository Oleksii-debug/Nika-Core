from __future__ import annotations

from datetime import UTC, datetime

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.experience_ledger import (
    ContinuityKind,
    ContinuityOutcome,
    ExperienceConflictError,
    ExperienceLedger,
)


def test_continuity_event_survives_fresh_store_restart(tmp_path) -> None:
    path = tmp_path / "Користувач Ніка" / "дані з пробілом" / "nika_core.db"
    store = SQLiteStore(path)
    store.initialize()
    ledger = ExperienceLedger(store)
    when = datetime(2026, 9, 3, 1, 2, 3, tzinfo=UTC)

    first = ledger.record(
        event_key="task-1:offline:1",
        task_id="task-1",
        kind=ContinuityKind.INTERNET,
        outcome=ContinuityOutcome.WAITING,
        reason_code="network_unavailable",
        occurred_at=when,
        attempt=2,
        delay_seconds=8,
    )

    reopened = ExperienceLedger(SQLiteStore(path))
    restored = reopened.get("task-1:offline:1")

    assert restored == first
    assert reopened.list_for_task("task-1") == (first,)


def test_same_event_key_and_same_evidence_is_idempotent(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    ledger = ExperienceLedger(store)
    when = datetime(2026, 9, 3, 4, 5, tzinfo=UTC)
    kwargs = dict(
        event_key="task-2:recovery:1",
        task_id="task-2",
        kind=ContinuityKind.RECOVERY,
        outcome=ContinuityOutcome.RESUMED,
        reason_code="checkpoint_verified",
        occurred_at=when,
    )

    assert ledger.record(**kwargs) == ledger.record(**kwargs)
    assert len(ledger.list_for_task("task-2")) == 1


def test_same_event_key_with_different_evidence_fails_closed(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    ledger = ExperienceLedger(store)
    when = datetime(2026, 9, 3, 4, 5, tzinfo=UTC)
    ledger.record(
        event_key="task-3:effect:1",
        task_id="task-3",
        kind=ContinuityKind.UNCERTAIN_EFFECT,
        outcome=ContinuityOutcome.BLOCKED,
        reason_code="verification_unavailable",
        occurred_at=when,
    )

    with pytest.raises(ExperienceConflictError, match="conflicts"):
        ledger.record(
            event_key="task-3:effect:1",
            task_id="task-3",
            kind=ContinuityKind.UNCERTAIN_EFFECT,
            outcome=ContinuityOutcome.RESUMED,
            reason_code="verification_unavailable",
            occurred_at=when,
        )


def test_ledger_schema_has_no_arbitrary_payload_or_secret_columns(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    ExperienceLedger(store)

    with store.connection() as conn:
        rows = conn.execute("PRAGMA table_info(continuity_experience_events)").fetchall()

    columns = {row["name"] for row in rows}
    assert columns == {
        "event_key",
        "task_id",
        "kind",
        "outcome",
        "reason_code",
        "occurred_at",
        "attempt",
        "delay_seconds",
        "clock_jump_seconds",
        "fingerprint",
    }
    forbidden = {"payload", "prompt", "response", "url", "header", "token", "secret", "error"}
    assert forbidden.isdisjoint(columns)


def test_reason_code_rejects_freeform_sensitive_text(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    ledger = ExperienceLedger(store)

    with pytest.raises(ValueError, match="machine-safe"):
        ledger.record(
            event_key="task-4:recovery:1",
            task_id="task-4",
            kind=ContinuityKind.RECOVERY,
            outcome=ContinuityOutcome.FAILED_SAFE,
            reason_code="provider error: Authorization: Bearer canary-secret",
        )


def test_clock_jump_capture_is_bounded_numeric_evidence(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    ledger = ExperienceLedger(store)

    event = ledger.record(
        event_key="task-5:wake:1",
        task_id="task-5",
        kind=ContinuityKind.HIBERNATE,
        outcome=ContinuityOutcome.PRESERVED,
        reason_code="wake_reconciled",
        clock_jump_seconds=28_800,
    )

    assert event.clock_jump_seconds == 28_800

    with pytest.raises(ValueError, match="non-negative"):
        ledger.record(
            event_key="task-5:wake:2",
            task_id="task-5",
            kind=ContinuityKind.HIBERNATE,
            outcome=ContinuityOutcome.PRESERVED,
            reason_code="wake_reconciled",
            clock_jump_seconds=-1,
        )


def test_naive_timestamp_is_rejected(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    ledger = ExperienceLedger(store)

    with pytest.raises(ValueError, match="timezone-aware"):
        ledger.record(
            event_key="task-6:restart:1",
            task_id="task-6",
            kind=ContinuityKind.APP_RESTART,
            outcome=ContinuityOutcome.PRESERVED,
            reason_code="state_restored",
            occurred_at=datetime(2026, 9, 3, 2, 0, 0),
        )
