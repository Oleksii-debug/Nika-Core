from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from nika_core.batch_cursor import (
    AttemptState,
    BatchCursor,
    BatchCursorStateError,
    BatchTargetSpec,
    IntentKind,
)
from nika_core.data.sqlite import SQLiteStore
from nika_core.memory import MemoryScope, MemoryService
from nika_core.runtime.idempotency import IdempotencyLedger, IdempotencyStatus


class BehavioralText(str):
    def strip(self, *_args: object, **_kwargs: object) -> str:
        return "forged-identity"


class BehavioralDateTime(datetime):
    pass


class BehavioralDict(dict[str, object]):
    pass


def _services(tmp_path: Path) -> tuple[MemoryService, IdempotencyLedger, SQLiteStore]:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    return MemoryService(store), IdempotencyLedger(store), store


def _targets(count: int = 3) -> list[BatchTargetSpec]:
    return [
        BatchTargetSpec(target_id=f"target-{index}", payload={"index": index})
        for index in range(count)
    ]


def _state_value(memory: MemoryService, task_id: str) -> dict[str, object]:
    record = memory.get(
        scope=MemoryScope.TASK,
        owner_id=task_id,
        namespace="v01.batch_cursor",
        key="cursor",
    )
    assert record is not None
    assert isinstance(record.value, dict)
    return record.value


def _replace_state(
    memory: MemoryService,
    task_id: str,
    value: dict[str, object],
) -> None:
    memory.put(
        scope=MemoryScope.TASK,
        owner_id=task_id,
        namespace="v01.batch_cursor",
        key="cursor",
        value=value,
    )


@pytest.mark.parametrize("field", ("task_id", "cursor_id"))
def test_create_rejects_behavioral_identity_carriers_before_persistence(
    tmp_path: Path,
    field: str,
) -> None:
    memory, ledger, store = _services(tmp_path)
    identities: dict[str, str] = {"task_id": "task", "cursor_id": "cursor"}
    identities[field] = BehavioralText("")

    with pytest.raises(TypeError, match=rf"{field} must be an exact string"):
        BatchCursor.create(
            memory,
            ledger,
            targets=_targets(1),
            batch_size=1,
            **identities,
        )

    with store.connection() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM memory_records WHERE namespace = ?",
            ("v01.batch_cursor",),
        ).fetchone()[0]
    assert count == 0


def test_effect_lookup_rejects_behavioral_target_identity_without_reservation(
    tmp_path: Path,
) -> None:
    memory, ledger, _ = _services(tmp_path)
    cursor = BatchCursor.create(
        memory,
        ledger,
        task_id="task",
        cursor_id="cursor",
        targets=_targets(1),
        batch_size=1,
    )

    with pytest.raises(TypeError, match="target_id must be an exact string"):
        cursor.begin_effect(BehavioralText("target-0"))

    assert cursor.state.targets[0].attempt_state is AttemptState.PENDING
    assert ledger.list_for_task("task") == ()


@pytest.mark.parametrize("batch_size", (True, 1.0))
def test_batch_size_requires_exact_positive_integer_before_state_access(
    tmp_path: Path,
    batch_size: object,
) -> None:
    memory, ledger, _ = _services(tmp_path)

    with pytest.raises(TypeError, match="batch_size must be an exact integer"):
        BatchCursor.create(
            memory,
            ledger,
            task_id="task",
            cursor_id="cursor",
            targets=_targets(1),
            batch_size=batch_size,  # type: ignore[arg-type]
        )

    canonical = BatchCursor.create(
        memory,
        ledger,
        task_id="task",
        cursor_id="cursor",
        targets=_targets(1),
        batch_size=1,
    )
    assert canonical.state.batch_size == 1

    with pytest.raises(TypeError, match="batch_size must be an exact integer"):
        BatchCursor.restore(
            memory,
            ledger,
            task_id="task",
            cursor_id="cursor",
            targets=_targets(1),
            batch_size=batch_size,  # type: ignore[arg-type]
        )


def test_confirm_rejects_noncanonical_result_object_before_completing_effect(
    tmp_path: Path,
) -> None:
    memory, ledger, _ = _services(tmp_path)
    cursor = BatchCursor.create(
        memory,
        ledger,
        task_id="task",
        cursor_id="cursor",
        targets=_targets(1),
        batch_size=1,
    )
    grant = cursor.begin_effect("target-0")

    with pytest.raises(TypeError, match="result must be an exact JSON object"):
        cursor.confirm("target-0", BehavioralDict(ok=True))  # type: ignore[arg-type]

    durable = ledger.require(grant.operation_key)
    assert durable.status is IdempotencyStatus.PENDING
    assert cursor.state.targets[0].attempt_state is AttemptState.IN_FLIGHT


def test_schedule_wait_rejects_datetime_subclass_without_mutating_deadline(
    tmp_path: Path,
) -> None:
    memory, ledger, _ = _services(tmp_path)
    cursor = BatchCursor.create(
        memory,
        ledger,
        task_id="task",
        cursor_id="cursor",
        targets=_targets(2),
        batch_size=1,
    )
    grant = cursor.begin_effect("target-0")
    assert grant.execute is True
    cursor.confirm("target-0", {"ok": True})
    assert cursor.state.next_scheduled_intent is not None
    assert cursor.state.next_scheduled_intent.kind is IntentKind.INTER_BATCH_WAIT
    assert cursor.state.next_scheduled_intent.not_before is None

    hostile = BehavioralDateTime(2030, 1, 2, 3, 4, 5, tzinfo=UTC)
    with pytest.raises(TypeError, match="datetime must be an exact datetime"):
        cursor.schedule_inter_batch_wait(hostile)

    assert cursor.state.next_scheduled_intent is not None
    assert cursor.state.next_scheduled_intent.not_before is None


def test_restore_rejects_noncanonical_equivalent_persisted_utc_deadline(
    tmp_path: Path,
) -> None:
    memory, ledger, _ = _services(tmp_path)
    due = datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC)
    cursor = BatchCursor.create(
        memory,
        ledger,
        task_id="task",
        cursor_id="cursor",
        targets=_targets(2),
        batch_size=1,
    )
    cursor.begin_effect("target-0")
    cursor.confirm("target-0", {"ok": True}, next_batch_not_before=due)

    state = _state_value(memory, "task")
    intent = state["next_scheduled_intent"]
    assert isinstance(intent, dict)
    intent["not_before"] = "2030-01-02T05:04:05+02:00"
    _replace_state(memory, "task", state)

    with pytest.raises(BatchCursorStateError, match="malformed restored"):
        BatchCursor.restore(
            memory,
            ledger,
            task_id="task",
            cursor_id="cursor",
            targets=_targets(2),
            batch_size=1,
        )


def test_restore_rejects_noncanonical_deadline_in_completed_effect_evidence(
    tmp_path: Path,
) -> None:
    memory, ledger, _ = _services(tmp_path)
    cursor = BatchCursor.create(
        memory,
        ledger,
        task_id="task",
        cursor_id="cursor",
        targets=_targets(2),
        batch_size=1,
    )
    grant = cursor.begin_effect("target-0")
    ledger.complete(
        grant.operation_key,
        {
            "__nika_batch_cursor_completion_v1__": {
                "result": {"ok": True},
                "next_batch_not_before": "2030-01-02T05:04:05+02:00",
            }
        },
    )

    with pytest.raises(BatchCursorStateError, match="wake deadline is malformed"):
        BatchCursor.restore(
            memory,
            ledger,
            task_id="task",
            cursor_id="cursor",
            targets=_targets(2),
            batch_size=1,
        )


def test_restore_rejects_ready_batch_index_ahead_of_execution_frontier(
    tmp_path: Path,
) -> None:
    memory, ledger, _ = _services(tmp_path)
    cursor = BatchCursor.create(
        memory,
        ledger,
        task_id="task",
        cursor_id="cursor",
        targets=_targets(3),
        batch_size=1,
    )
    cursor.begin_effect("target-0")
    cursor.confirm("target-0", {"ok": True})

    state = _state_value(memory, "task")
    state["ready_batch_index"] = 2
    _replace_state(memory, "task", state)

    with pytest.raises(BatchCursorStateError, match="malformed restored"):
        BatchCursor.restore(
            memory,
            ledger,
            task_id="task",
            cursor_id="cursor",
            targets=_targets(3),
            batch_size=1,
        )


def test_canonical_utc_deadline_and_released_frontier_still_round_trip(
    tmp_path: Path,
) -> None:
    memory, ledger, _ = _services(tmp_path)
    due = datetime(2020, 1, 2, 3, 4, 5, tzinfo=UTC)
    cursor = BatchCursor.create(
        memory,
        ledger,
        task_id=" task ",
        cursor_id=" cursor ",
        targets=_targets(2),
        batch_size=1,
    )
    grant = cursor.begin_effect("target-0")
    assert grant.execute is True
    cursor.confirm("target-0", {"ok": True}, next_batch_not_before=due)
    cursor.release_inter_batch_wait(now=due)

    restarted = BatchCursor.restore(
        memory,
        ledger,
        task_id="task",
        cursor_id="cursor",
        targets=_targets(2),
        batch_size=1,
    )
    assert restarted.state.ready_batch_index == 1
    assert restarted.next_target() is not None
    assert restarted.next_target().target_id == "target-1"


def test_mark_uncertain_validates_evidence_before_ledger_mutation(
    tmp_path: Path,
) -> None:
    memory, ledger, _ = _services(tmp_path)
    cursor = BatchCursor.create(
        memory,
        ledger,
        task_id="task",
        cursor_id="cursor",
        targets=_targets(1),
        batch_size=1,
    )
    grant = cursor.begin_effect("target-0")

    with pytest.raises(BatchCursorStateError, match="JSON-serializable"):
        cursor.mark_uncertain("target-0", {"bad": object()})

    durable = ledger.require(grant.operation_key)
    assert durable.status is IdempotencyStatus.PENDING
    assert cursor.state.targets[0].attempt_state is AttemptState.IN_FLIGHT


def test_confirm_rejects_nonfinite_json_before_completing_effect(
    tmp_path: Path,
) -> None:
    memory, ledger, _ = _services(tmp_path)
    cursor = BatchCursor.create(
        memory,
        ledger,
        task_id="task",
        cursor_id="cursor",
        targets=_targets(1),
        batch_size=1,
    )
    grant = cursor.begin_effect("target-0")

    with pytest.raises(BatchCursorStateError, match="JSON-serializable"):
        cursor.confirm("target-0", {"score": float("nan")})

    durable = ledger.require(grant.operation_key)
    assert durable.status is IdempotencyStatus.PENDING
    assert cursor.state.targets[0].attempt_state is AttemptState.IN_FLIGHT


def test_restore_rejects_active_future_frontier_before_batch_release(
    tmp_path: Path,
) -> None:
    memory, ledger, _ = _services(tmp_path)
    cursor = BatchCursor.create(
        memory,
        ledger,
        task_id="task",
        cursor_id="cursor",
        targets=_targets(2),
        batch_size=1,
    )
    cursor.begin_effect("target-0")
    cursor.confirm("target-0", {"ok": True})

    state = _state_value(memory, "task")
    targets = state["targets"]
    assert isinstance(targets, list)
    second = targets[1]
    assert isinstance(second, dict)
    second["attempt_state"] = "prepared"
    _replace_state(memory, "task", state)

    with pytest.raises(BatchCursorStateError, match="malformed restored"):
        BatchCursor.restore(
            memory,
            ledger,
            task_id="task",
            cursor_id="cursor",
            targets=_targets(2),
            batch_size=1,
        )

