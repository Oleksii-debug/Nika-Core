from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nika_core.batch_cursor import (
    AttemptState,
    BatchCursor,
    BatchCursorBlockedError,
    BatchTargetSpec,
    IntentKind,
)
from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.memory import MemoryService
from nika_core.runtime.idempotency import IdempotencyLedger


def _services(tmp_path: Path) -> tuple[MemoryService, IdempotencyLedger, SQLiteStore]:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    return MemoryService(store), IdempotencyLedger(store), store


def _task(store: SQLiteStore, label: str) -> str:
    return TaskQueue(store).create(
        workspace_id=f"terminal-failure-{label}",
        agent_id="scenario-b",
    ).task_id


def _targets(count: int) -> list[BatchTargetSpec]:
    return [
        BatchTargetSpec(target_id=f"target-{index}", payload={"index": index})
        for index in range(count)
    ]


def test_deterministic_failure_is_durable_and_does_not_block_sibling(
    tmp_path: Path,
) -> None:
    memory, ledger, store = _services(tmp_path)
    task_id = _task(store, "sibling")
    targets = _targets(3)
    cursor = BatchCursor.create(
        memory,
        ledger,
        task_id=task_id,
        cursor_id="cursor",
        targets=targets,
        batch_size=2,
    )

    cursor.mark_terminal_failure("target-0")

    state = cursor.state
    assert state.targets[0].attempt_state is AttemptState.FAILED
    assert state.failed_count == 1
    assert state.pending_count == 2
    assert state.next_scheduled_intent is not None
    assert state.next_scheduled_intent.kind is IntentKind.TARGET
    assert state.next_scheduled_intent.target_id == "target-1"
    assert ledger.get(state.targets[0].operation_key) is None

    restarted = BatchCursor.restore(
        memory,
        ledger,
        task_id=task_id,
        cursor_id="cursor",
        targets=targets,
        batch_size=2,
    )
    assert restarted.state.targets[0].attempt_state is AttemptState.FAILED
    assert restarted.next_target() is not None
    assert restarted.next_target().target_id == "target-1"
    assert restarted.begin_effect("target-1").execute is True


def test_failed_last_target_in_batch_preserves_durable_inter_batch_wait(
    tmp_path: Path,
) -> None:
    memory, ledger, store = _services(tmp_path)
    task_id = _task(store, "wait")
    targets = _targets(3)
    cursor = BatchCursor.create(
        memory,
        ledger,
        task_id=task_id,
        cursor_id="cursor",
        targets=targets,
        batch_size=2,
    )
    due = datetime(2031, 4, 5, 6, 7, 8, tzinfo=UTC)

    grant = cursor.begin_effect("target-0")
    assert grant.execute is True
    cursor.confirm("target-0", {"verified": True})
    cursor.mark_terminal_failure("target-1", next_batch_not_before=due)

    intent = cursor.state.next_scheduled_intent
    assert intent is not None
    assert intent.kind is IntentKind.INTER_BATCH_WAIT
    assert intent.target_id == "target-2"
    assert intent.not_before == due.isoformat()

    restarted = BatchCursor.restore(
        memory,
        ledger,
        task_id=task_id,
        cursor_id="cursor",
        targets=targets,
        batch_size=2,
    )
    with pytest.raises(BatchCursorBlockedError, match="deadline"):
        restarted.release_inter_batch_wait(now=due - timedelta(microseconds=1))
    restarted.release_inter_batch_wait(now=due)
    assert restarted.next_target() is not None
    assert restarted.next_target().target_id == "target-2"


def test_terminal_failure_cannot_overwrite_prepared_effect_authority(
    tmp_path: Path,
) -> None:
    memory, ledger, store = _services(tmp_path)
    task_id = _task(store, "prepared")
    cursor = BatchCursor.create(
        memory,
        ledger,
        task_id=task_id,
        cursor_id="cursor",
        targets=_targets(1),
        batch_size=1,
    )

    grant = cursor.prepare_external_effect("target-0")
    assert grant.execute is True
    assert cursor.state.targets[0].attempt_state is AttemptState.PREPARED

    with pytest.raises(BatchCursorBlockedError, match="pre-effect"):
        cursor.mark_terminal_failure("target-0")

    assert cursor.state.targets[0].attempt_state is AttemptState.PREPARED
