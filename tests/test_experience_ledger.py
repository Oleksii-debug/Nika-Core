from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier

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
    kwargs = {
        "event_key": "task-2:recovery:1",
        "task_id": "task-2",
        "kind": ContinuityKind.RECOVERY,
        "outcome": ContinuityOutcome.RESUMED,
        "reason_code": "checkpoint_verified",
        "occurred_at": when,
    }

    assert ledger.record(**kwargs) == ledger.record(**kwargs)
    assert len(ledger.list_for_task("task-2")) == 1


def test_generated_occurrence_is_idempotent_for_concurrent_same_event_replay(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    ledger = ExperienceLedger(store)
    barrier = Barrier(4)

    def record_same_event() -> object:
        barrier.wait(timeout=5)
        return ledger.record(
            event_key="task-2:restart:generated",
            task_id="task-2",
            kind=ContinuityKind.APP_RESTART,
            outcome=ContinuityOutcome.PRESERVED,
            reason_code="state_restored",
            attempt=1,
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        events = tuple(pool.map(lambda _: record_same_event(), range(4)))

    assert events == (events[0],) * 4
    assert ledger.list_for_task("task-2") == (events[0],)


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


def test_generated_occurrence_still_conflicts_on_materially_different_evidence(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    ledger = ExperienceLedger(store)
    ledger.record(
        event_key="task-3:generated:1",
        task_id="task-3",
        kind=ContinuityKind.RECOVERY,
        outcome=ContinuityOutcome.BLOCKED,
        reason_code="checkpoint_unavailable",
    )

    with pytest.raises(ExperienceConflictError, match="conflicts"):
        ledger.record(
            event_key="task-3:generated:1",
            task_id="task-3",
            kind=ContinuityKind.RECOVERY,
            outcome=ContinuityOutcome.RESUMED,
            reason_code="checkpoint_verified",
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


def test_numeric_evidence_rejects_non_typed_runtime_values(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    ledger = ExperienceLedger(store)

    with pytest.raises(TypeError, match="attempt must be an integer"):
        ledger.record(
            event_key="task-5:attempt:float",
            task_id="task-5",
            kind=ContinuityKind.RECOVERY,
            outcome=ContinuityOutcome.WAITING,
            reason_code="retry_pending",
            attempt=1.5,  # type: ignore[arg-type]
        )
    with pytest.raises(TypeError, match="int or float"):
        ledger.record(
            event_key="task-5:delay:bool",
            task_id="task-5",
            kind=ContinuityKind.RECOVERY,
            outcome=ContinuityOutcome.WAITING,
            reason_code="retry_pending",
            delay_seconds=True,
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
    naive_timestamp = datetime(2026, 9, 3, 2, 0, 0, tzinfo=UTC).replace(tzinfo=None)

    with pytest.raises(ValueError, match="timezone-aware"):
        ledger.record(
            event_key="task-6:restart:1",
            task_id="task-6",
            kind=ContinuityKind.APP_RESTART,
            outcome=ContinuityOutcome.PRESERVED,
            reason_code="state_restored",
            occurred_at=naive_timestamp,
        )


def test_tampered_durable_event_fails_closed_on_get_and_list(tmp_path) -> None:
    path = tmp_path / "Користувач Ніка" / "ledger.db"
    store = SQLiteStore(path)
    store.initialize()
    ledger = ExperienceLedger(store)
    ledger.record(
        event_key="task-7:recovery:1",
        task_id="task-7",
        kind=ContinuityKind.RECOVERY,
        outcome=ContinuityOutcome.WAITING,
        reason_code="checkpoint_pending",
        occurred_at=datetime(2026, 9, 12, 8, 0, tzinfo=UTC),
        attempt=1,
    )

    with store.connection() as conn:
        conn.execute(
            "UPDATE continuity_experience_events SET outcome = ? WHERE event_key = ?",
            (ContinuityOutcome.COMPLETED.value, "task-7:recovery:1"),
        )

    reopened = ExperienceLedger(SQLiteStore(path))
    with pytest.raises(ExperienceConflictError, match="integrity"):
        reopened.get("task-7:recovery:1")
    with pytest.raises(ExperienceConflictError, match="integrity"):
        reopened.list_for_task("task-7")


def test_invalid_durable_event_shape_fails_closed_on_readback(tmp_path) -> None:
    path = tmp_path / "nika.db"
    store = SQLiteStore(path)
    store.initialize()
    ledger = ExperienceLedger(store)
    ledger.record(
        event_key="task-8:recovery:1",
        task_id="task-8",
        kind=ContinuityKind.RECOVERY,
        outcome=ContinuityOutcome.PRESERVED,
        reason_code="checkpoint_verified",
        occurred_at=datetime(2026, 9, 12, 8, 30, tzinfo=UTC),
    )

    with store.connection() as conn:
        conn.execute(
            "UPDATE continuity_experience_events SET kind = ? WHERE event_key = ?",
            ("unknown_kind", "task-8:recovery:1"),
        )

    reopened = ExperienceLedger(SQLiteStore(path))
    with pytest.raises(ExperienceConflictError, match="invalid durable evidence"):
        reopened.get("task-8:recovery:1")
