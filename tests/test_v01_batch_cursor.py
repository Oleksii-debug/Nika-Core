from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nika_core.batch_cursor import (
    AttemptState,
    BatchCursor,
    BatchCursorBlockedError,
    BatchCursorStateError,
    BatchTargetSpec,
    IntentKind,
)
from nika_core.data.sqlite import SQLiteStore
from nika_core.memory import MemoryService
from nika_core.runtime.idempotency import IdempotencyLedger


def _services(tmp_path: Path) -> tuple[MemoryService, IdempotencyLedger, SQLiteStore]:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    return MemoryService(store), IdempotencyLedger(store), store


def _targets(count: int = 20) -> list[BatchTargetSpec]:
    return [
        BatchTargetSpec(target_id=f"target-{index}", payload={"index": index})
        for index in range(count)
    ]


def _confirm(cursor: BatchCursor, target_id: str, result: dict[str, object]) -> None:
    grant = cursor.begin_effect(target_id)
    assert grant.execute is True
    cursor.confirm(target_id, result)


def test_restart_after_three_of_five_preserves_exact_next_target(tmp_path: Path) -> None:
    memory, ledger, _ = _services(tmp_path)
    cursor = BatchCursor.create(
        memory,
        ledger,
        task_id="task-3-of-5",
        cursor_id="cursor",
        targets=_targets(),
        batch_size=5,
    )

    for index in range(3):
        _confirm(cursor, f"target-{index}", {"confirmed": index})

    restarted = BatchCursor.restore(
        memory,
        ledger,
        task_id="task-3-of-5",
        cursor_id="cursor",
    )

    assert restarted.state.confirmed_count == 3
    assert restarted.state.pending_count == 17
    assert restarted.state.uncertain_count == 0
    assert restarted.next_target() is not None
    assert restarted.next_target().target_id == "target-3"
    assert restarted.next_target().batch_index == 0
    assert restarted.next_target().batch_position == 3
    assert restarted.state.next_scheduled_intent is not None
    assert restarted.state.next_scheduled_intent.kind is IntentKind.TARGET
    assert restarted.state.next_scheduled_intent.target_id == "target-3"


def test_restart_exactly_between_batches_preserves_durable_next_intent(
    tmp_path: Path,
) -> None:
    memory, ledger, _ = _services(tmp_path)
    cursor = BatchCursor.create(
        memory,
        ledger,
        task_id="task-between",
        cursor_id="cursor",
        targets=_targets(),
        batch_size=5,
    )
    due = datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC)

    for index in range(5):
        grant = cursor.begin_effect(f"target-{index}")
        assert grant.execute is True
        cursor.confirm(
            f"target-{index}",
            {"confirmed": index},
            next_batch_not_before=due if index == 4 else None,
        )

    restarted = BatchCursor.restore(
        memory,
        ledger,
        task_id="task-between",
        cursor_id="cursor",
    )
    intent = restarted.state.next_scheduled_intent
    assert restarted.state.confirmed_count == 5
    assert restarted.next_target() is None
    assert intent is not None
    assert intent.kind is IntentKind.INTER_BATCH_WAIT
    assert intent.batch_index == 1
    assert intent.target_id == "target-5"
    assert intent.not_before == due.isoformat()

    with pytest.raises(BatchCursorBlockedError, match="deadline"):
        restarted.release_inter_batch_wait(now=due - timedelta(microseconds=1))

    restarted.release_inter_batch_wait(now=due)
    assert restarted.next_target() is not None
    assert restarted.next_target().target_id == "target-5"
    assert restarted.state.next_scheduled_intent is not None
    assert restarted.state.next_scheduled_intent.kind is IntentKind.TARGET


def test_completed_target_never_executes_twice_after_restart(tmp_path: Path) -> None:
    memory, ledger, _ = _services(tmp_path)
    cursor = BatchCursor.create(
        memory,
        ledger,
        task_id="task-replay",
        cursor_id="cursor",
        targets=_targets(2),
        batch_size=2,
    )
    _confirm(cursor, "target-0", {"remote_id": "result-0"})

    restarted = BatchCursor.restore(
        memory,
        ledger,
        task_id="task-replay",
        cursor_id="cursor",
    )
    grant = restarted.begin_effect("target-0")

    assert grant.execute is False
    assert grant.reason == "already_confirmed"
    assert restarted.state.targets[0].confirmed_result == {"remote_id": "result-0"}
    assert restarted.next_target() is not None
    assert restarted.next_target().target_id == "target-1"
    records = ledger.list_for_task("task-replay")
    assert len(records) == 1
    assert records[0].operation_key == grant.operation_key


def test_duplicate_target_input_is_deterministic_and_conflicts_fail_closed(
    tmp_path: Path,
) -> None:
    memory, ledger, _ = _services(tmp_path)
    cursor = BatchCursor.create(
        memory,
        ledger,
        task_id="task-duplicates",
        cursor_id="cursor",
        targets=[
            BatchTargetSpec(target_id="same", payload={"value": "один"}),
            BatchTargetSpec(target_id="other", payload={"value": "два"}),
            BatchTargetSpec(target_id="same", payload={"value": "один"}),
        ],
        batch_size=2,
    )

    assert cursor.state.input_count == 3
    assert [target.target_id for target in cursor.state.targets] == ["same", "other"]
    assert cursor.state.targets[0].input_positions == [0, 2]
    assert cursor.state.targets[0].position == 0
    assert cursor.state.targets[0].batch_index == 0
    assert cursor.state.targets[0].batch_position == 0

    with pytest.raises(BatchCursorStateError, match="conflicting payload"):
        BatchCursor.create(
            memory,
            ledger,
            task_id="task-conflicting-duplicates",
            cursor_id="cursor",
            targets=[
                BatchTargetSpec(target_id="same", payload={"value": 1}),
                BatchTargetSpec(target_id="same", payload={"value": 2}),
            ],
            batch_size=5,
        )


def test_malformed_restored_state_fails_closed(tmp_path: Path) -> None:
    memory, ledger, store = _services(tmp_path)
    BatchCursor.create(
        memory,
        ledger,
        task_id="task-malformed",
        cursor_id="cursor",
        targets=_targets(2),
        batch_size=1,
    )

    with store.connection() as conn:
        row = conn.execute(
            """
            SELECT value_json
            FROM memory_records
            WHERE scope = 'task' AND owner_id = ?
              AND namespace = 'v01.batch_cursor' AND memory_key = ?
            """,
            ("task-malformed", "cursor"),
        ).fetchone()
        assert row is not None
        value = json.loads(row["value_json"])
        value["plan_fingerprint"] = "tampered"
        conn.execute(
            """
            UPDATE memory_records
            SET value_json = ?
            WHERE scope = 'task' AND owner_id = ?
              AND namespace = 'v01.batch_cursor' AND memory_key = ?
            """,
            (
                json.dumps(value, sort_keys=True, separators=(",", ":")),
                "task-malformed",
                "cursor",
            ),
        )

    with pytest.raises(BatchCursorStateError, match="malformed restored"):
        BatchCursor.restore(
            memory,
            ledger,
            task_id="task-malformed",
            cursor_id="cursor",
        )


def test_restart_with_unresolved_in_flight_effect_becomes_uncertain(
    tmp_path: Path,
) -> None:
    memory, ledger, _ = _services(tmp_path)
    cursor = BatchCursor.create(
        memory,
        ledger,
        task_id="task-uncertain",
        cursor_id="cursor",
        targets=_targets(2),
        batch_size=2,
    )
    grant = cursor.begin_effect("target-0")
    assert grant.execute is True

    restarted = BatchCursor.restore(
        memory,
        ledger,
        task_id="task-uncertain",
        cursor_id="cursor",
    )

    target = restarted.state.targets[0]
    assert target.attempt_state is AttemptState.UNCERTAIN
    assert target.uncertain_result == {
        "reason": "restart_with_unresolved_pending_effect"
    }
    assert restarted.state.next_scheduled_intent is not None
    assert restarted.state.next_scheduled_intent.kind is IntentKind.RECONCILE
    with pytest.raises(BatchCursorBlockedError, match="uncertain"):
        restarted.begin_effect("target-0")


def test_restart_heals_cursor_from_durable_completed_effect(tmp_path: Path) -> None:
    memory, ledger, _ = _services(tmp_path)
    cursor = BatchCursor.create(
        memory,
        ledger,
        task_id="task-heal",
        cursor_id="cursor",
        targets=_targets(2),
        batch_size=2,
    )
    grant = cursor.begin_effect("target-0")
    assert grant.execute is True

    ledger.complete(grant.operation_key, {"verified": True})

    restarted = BatchCursor.restore(
        memory,
        ledger,
        task_id="task-heal",
        cursor_id="cursor",
    )

    target = restarted.state.targets[0]
    assert target.attempt_state is AttemptState.CONFIRMED
    assert target.confirmed_result == {"verified": True}
    assert restarted.next_target() is not None
    assert restarted.next_target().target_id == "target-1"
