from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from nika_core.batch_cursor import AttemptState, BatchCursor, BatchTargetSpec, IntentKind
from nika_core.data.sqlite import SQLiteStore
from nika_core.memory import MemoryService
from nika_core.runtime.idempotency import IdempotencyLedger, IdempotencyStatus


class SimulatedHardCrash(BaseException):
    """Stop execution immediately after the durable effect completion commit."""


class CrashAfterCompleteLedger(IdempotencyLedger):
    def complete(self, operation_key: str, result: object):  # type: ignore[override]
        super().complete(operation_key, result)
        raise SimulatedHardCrash


def _store(db_path: Path) -> SQLiteStore:
    store = SQLiteStore(db_path)
    store.initialize()
    return store


def test_final_effect_completion_and_next_wake_are_one_recoverable_transition(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "ніка worker18 crash ordering" / "nika.db"
    first_store = _store(db_path)
    memory = MemoryService(first_store)
    crashing_ledger = CrashAfterCompleteLedger(first_store)
    cursor = BatchCursor.create(
        memory,
        crashing_ledger,
        task_id="worker18-task",
        cursor_id="cursor",
        targets=[
            BatchTargetSpec(target_id="target-0", payload={"batch": 0}),
            BatchTargetSpec(target_id="target-1", payload={"batch": 1}),
        ],
        batch_size=1,
    )
    due = datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC)

    grant = cursor.begin_effect("target-0")
    assert grant.execute is True

    # Exact production boundary under attack:
    # BatchCursor.confirm() currently commits IdempotencyLedger first, then advances
    # and persists INTER_BATCH_WAIT.not_before. Simulate process loss after the first
    # durable commit and before any later cursor persistence can occur.
    with pytest.raises(SimulatedHardCrash):
        cursor.confirm(
            "target-0",
            {"remote_id": "confirmed-effect"},
            next_batch_not_before=due,
        )

    restarted_store = _store(db_path)
    restarted_ledger = IdempotencyLedger(restarted_store)
    restarted = BatchCursor.restore(
        MemoryService(restarted_store),
        restarted_ledger,
        task_id="worker18-task",
        cursor_id="cursor",
    )

    completed = restarted_ledger.require(grant.operation_key)
    assert completed.status is IdempotencyStatus.COMPLETED
    assert restarted.state.targets[0].attempt_state is AttemptState.CONFIRMED
    replay = restarted.begin_effect("target-0")
    assert replay.execute is False
    assert replay.reason == "already_confirmed"

    intent = restarted.state.next_scheduled_intent
    assert intent is not None
    assert intent.kind is IntentKind.INTER_BATCH_WAIT
    assert intent.batch_index == 1
    assert intent.target_id == "target-1"
    assert intent.not_before == due.isoformat()
