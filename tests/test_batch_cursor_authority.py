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
    now = datetime.now(UTC).isoformat()
    with store.connection() as conn:
        conn.execute(
            """
            INSERT INTO tasks(
                task_id, workspace_id, agent_id, state, payload_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("task", "batch-cursor-fixture", "batch-cursor-fixture", "created", "{}", now, now),
        )
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


def test_restore_rejects_nonfinite_uncertain_evidence_before_mutation(
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
    cursor.mark_uncertain("target-0", {"reason": "network"})
    state = _state_value(memory, "task")
    targets = state["targets"]
    assert isinstance(targets, list)
    first = targets[0]
    assert isinstance(first, dict)
    uncertain = first["uncertain_result"]
    assert isinstance(uncertain, dict)
    uncertain["score"] = float("nan")
    _replace_state(memory, "task", state)

    before = ledger.require(grant.operation_key)
    assert before.status is IdempotencyStatus.UNCERTAIN

    with pytest.raises(BatchCursorStateError, match="malformed restored batch cursor state"):
        BatchCursor.restore(
            memory,
            ledger,
            task_id="task",
            cursor_id="cursor",
            targets=_targets(1),
            batch_size=1,
        )

    after = ledger.require(grant.operation_key)
    assert after.status is IdempotencyStatus.UNCERTAIN
    persisted = _state_value(memory, "task")
    persisted_targets = persisted["targets"]
    assert isinstance(persisted_targets, list)
    persisted_first = persisted_targets[0]
    assert isinstance(persisted_first, dict)
    persisted_uncertain = persisted_first["uncertain_result"]
    assert isinstance(persisted_uncertain, dict)
    score = persisted_uncertain["score"]
    assert isinstance(score, float)
    assert score != score


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

def test_restore_preflights_future_effect_evidence_without_mutating_ledger(
    tmp_path: Path,
) -> None:
    memory, ledger, _ = _services(tmp_path)
    cursor = BatchCursor.create(
        memory,
        ledger,
        task_id="task",
        cursor_id="cursor",
        targets=_targets(2),
        batch_size=2,
    )
    future = cursor.state.targets[1]
    record, created = ledger.reserve_once(
        operation_key=future.operation_key,
        task_id="task",
        operation_type="v01.batch_target_effect",
        input_fingerprint=future.input_fingerprint,
    )
    assert created is True
    assert record.status is IdempotencyStatus.PENDING

    with pytest.raises(BatchCursorStateError, match="beyond cursor execution frontier"):
        BatchCursor.restore(
            memory,
            ledger,
            task_id="task",
            cursor_id="cursor",
            targets=_targets(2),
            batch_size=2,
        )

    durable = ledger.require(future.operation_key)
    assert durable.status is IdempotencyStatus.PENDING
    assert cursor.state.targets[0].attempt_state is AttemptState.PENDING

def test_failed_batch_release_restores_last_durable_cursor_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
    assert cursor.state.ready_batch_index == 0
    assert cursor.state.next_scheduled_intent is not None
    assert cursor.state.next_scheduled_intent.kind is IntentKind.INTER_BATCH_WAIT

    def fail_put(**_kwargs: object) -> None:
        raise RuntimeError("synthetic memory write failure")

    monkeypatch.setattr(memory, "put", fail_put)
    with pytest.raises(RuntimeError, match="synthetic memory write failure"):
        cursor.release_inter_batch_wait()

    assert cursor.state.ready_batch_index == 0
    assert cursor.state.next_scheduled_intent is not None
    assert cursor.state.next_scheduled_intent.kind is IntentKind.INTER_BATCH_WAIT
    with pytest.raises(BatchCursorBlockedError, match="waiting for scheduled release"):
        cursor.begin_effect("target-1")


def test_failed_second_begin_checkpoint_rolls_back_to_last_durable_prepared_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
    original_put = memory.put
    calls = 0

    def fail_second_put(**kwargs: object):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("synthetic second checkpoint failure")
        return original_put(**kwargs)

    monkeypatch.setattr(memory, "put", fail_second_put)
    with pytest.raises(RuntimeError, match="synthetic second checkpoint failure"):
        cursor.begin_effect("target-0")

    assert cursor.state.targets[0].attempt_state is AttemptState.PREPARED
    durable = ledger.require(cursor.state.targets[0].operation_key)
    assert durable.status is IdempotencyStatus.PENDING

    replay = cursor.begin_effect("target-0")
    assert replay.execute is False
    assert replay.reason == "effect_already_reserved"

def test_post_commit_put_exception_reconciles_authoritative_future_wait(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory, ledger, store = _services(tmp_path)
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
    due = datetime.now(UTC) + timedelta(hours=1)
    original_put = memory.put

    def commit_then_raise(**kwargs: object):
        record = original_put(**kwargs)
        assert record is not None
        raise RuntimeError("synthetic lost acknowledgement")

    monkeypatch.setattr(memory, "put", commit_then_raise)
    with pytest.raises(RuntimeError, match="synthetic lost acknowledgement"):
        cursor.schedule_inter_batch_wait(due)

    intent = cursor.state.next_scheduled_intent
    assert intent is not None
    assert intent.kind is IntentKind.INTER_BATCH_WAIT
    assert intent.not_before == due.isoformat()

    with pytest.raises(BatchCursorBlockedError, match="deadline has not been reached"):
        cursor.release_inter_batch_wait(now=due - timedelta(minutes=1))

    durable = MemoryService(store).get(
        scope=MemoryScope.TASK,
        owner_id="task",
        namespace="v01.batch_cursor",
        key="cursor",
    )
    assert durable is not None
    durable_intent = durable.value["next_scheduled_intent"]
    assert isinstance(durable_intent, dict)
    assert durable_intent["not_before"] == due.isoformat()


def test_post_commit_json_alias_conflict_fail_stops_live_cursor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory, ledger, store = _services(tmp_path)
    cursor = BatchCursor.create(
        memory,
        ledger,
        task_id="task",
        cursor_id="cursor",
        targets=_targets(2),
        batch_size=1,
    )
    cursor.begin_effect("target-0")
    cursor.confirm("target-0", {"value": True})
    due = datetime.now(UTC) + timedelta(hours=1)
    original_put = memory.put

    def commit_alias_then_raise(**kwargs: object):
        committed = original_put(**kwargs)
        assert committed is not None
        tampered = json.loads(json.dumps(committed.value))
        targets = tampered["targets"]
        assert isinstance(targets, list)
        first = targets[0]
        assert isinstance(first, dict)
        result = first["confirmed_result"]
        assert isinstance(result, dict)
        result["value"] = 1
        tampered_kwargs = dict(kwargs)
        tampered_kwargs["value"] = tampered
        original_put(**tampered_kwargs)
        raise RuntimeError("synthetic canonical-json conflict")

    monkeypatch.setattr(memory, "put", commit_alias_then_raise)
    with pytest.raises(
        BatchCursorStateError,
        match="persistence outcome conflicts with durable state",
    ):
        cursor.schedule_inter_batch_wait(due)

    with pytest.raises(BatchCursorBlockedError, match="restore is required"):
        cursor.release_inter_batch_wait(now=due + timedelta(hours=1))
    with pytest.raises(BatchCursorBlockedError, match="restore is required"):
        cursor.begin_effect("target-1")

    durable = MemoryService(store).get(
        scope=MemoryScope.TASK,
        owner_id="task",
        namespace="v01.batch_cursor",
        key="cursor",
    )
    assert durable is not None
    durable_targets = durable.value["targets"]
    assert isinstance(durable_targets, list)
    durable_first = durable_targets[0]
    assert isinstance(durable_first, dict)
    durable_result = durable_first["confirmed_result"]
    assert isinstance(durable_result, dict)
    assert type(durable_result["value"]) is int
    assert durable_result["value"] == 1


def test_post_commit_nonfinite_conflict_fail_stops_live_cursor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory, ledger, store = _services(tmp_path)
    cursor = BatchCursor.create(
        memory,
        ledger,
        task_id="task",
        cursor_id="cursor",
        targets=_targets(2),
        batch_size=1,
    )
    cursor.begin_effect("target-0")
    cursor.confirm("target-0", {"value": 0})
    due = datetime.now(UTC) + timedelta(hours=1)
    original_put = memory.put

    def commit_nonfinite_then_raise(**kwargs: object):
        committed = original_put(**kwargs)
        assert committed is not None
        tampered = json.loads(json.dumps(committed.value))
        targets = tampered["targets"]
        assert isinstance(targets, list)
        first = targets[0]
        assert isinstance(first, dict)
        result = first["confirmed_result"]
        assert isinstance(result, dict)
        result["value"] = float("nan")
        tampered_kwargs = dict(kwargs)
        tampered_kwargs["value"] = tampered
        original_put(**tampered_kwargs)
        raise RuntimeError("synthetic non-finite durable conflict")

    monkeypatch.setattr(memory, "put", commit_nonfinite_then_raise)
    with pytest.raises(
        BatchCursorStateError,
        match="persistence outcome conflicts with durable state",
    ):
        cursor.schedule_inter_batch_wait(due)

    with pytest.raises(BatchCursorBlockedError, match="restore is required"):
        cursor.next_target()
    with pytest.raises(BatchCursorBlockedError, match="restore is required"):
        cursor.begin_effect("target-1")

    durable = MemoryService(store).get(
        scope=MemoryScope.TASK,
        owner_id="task",
        namespace="v01.batch_cursor",
        key="cursor",
    )
    assert durable is not None
    durable_targets = durable.value["targets"]
    assert isinstance(durable_targets, list)
    durable_first = durable_targets[0]
    assert isinstance(durable_first, dict)
    durable_result = durable_first["confirmed_result"]
    assert isinstance(durable_result, dict)
    durable_value = durable_result["value"]
    assert isinstance(durable_value, float)
    assert durable_value != durable_value


def test_unreadable_post_commit_outcome_fail_stops_live_cursor_until_restore(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory, ledger, store = _services(tmp_path)
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
    due = datetime.now(UTC) + timedelta(hours=1)

    def fail_read(**_kwargs: object):
        raise RuntimeError("synthetic post-commit read outage")

    monkeypatch.setattr(memory, "get", fail_read)
    with pytest.raises(BatchCursorStateError, match="persistence outcome is unknown"):
        cursor.schedule_inter_batch_wait(due)

    with pytest.raises(BatchCursorBlockedError, match="restore is required"):
        cursor.release_inter_batch_wait(now=due + timedelta(hours=1))
    with pytest.raises(BatchCursorBlockedError, match="restore is required"):
        cursor.next_target()
    with pytest.raises(BatchCursorBlockedError, match="restore is required"):
        cursor.begin_effect("target-1")

    durable = MemoryService(store).get(
        scope=MemoryScope.TASK,
        owner_id="task",
        namespace="v01.batch_cursor",
        key="cursor",
    )
    assert durable is not None
    durable_intent = durable.value["next_scheduled_intent"]
    assert isinstance(durable_intent, dict)
    assert durable_intent["not_before"] == due.isoformat()


@pytest.mark.parametrize("restart_after", tuple(range(1, 21)))
def test_twenty_target_plan_restores_exactly_after_every_target(
    tmp_path: Path,
    restart_after: int,
) -> None:
    memory, ledger, _ = _services(tmp_path)
    targets = _targets(20)
    cursor = BatchCursor.create(
        memory,
        ledger,
        task_id="task",
        cursor_id="cursor",
        targets=targets,
        batch_size=5,
    )

    for index in range(restart_after):
        if cursor.next_target() is None:
            intent = cursor.state.next_scheduled_intent
            assert intent is not None
            assert intent.kind is IntentKind.INTER_BATCH_WAIT
            cursor.release_inter_batch_wait()
        grant = cursor.begin_effect(f"target-{index}")
        assert grant.execute is True
        cursor.confirm(f"target-{index}", {"confirmed": index})

    restarted = BatchCursor.restore(
        memory,
        ledger,
        task_id="task",
        cursor_id="cursor",
        targets=targets,
        batch_size=5,
    )
    assert restarted.state.confirmed_count == restart_after
    assert len(ledger.list_for_task("task")) == restart_after

    if restart_after == 20:
        assert restarted.next_target() is None
        assert restarted.state.next_scheduled_intent is None
        return

    if restart_after % 5 == 0:
        assert restarted.next_target() is None
        intent = restarted.state.next_scheduled_intent
        assert intent is not None
        assert intent.kind is IntentKind.INTER_BATCH_WAIT
        assert intent.target_id == f"target-{restart_after}"
        assert intent.batch_index == restart_after // 5
    else:
        next_target = restarted.next_target()
        assert next_target is not None
        assert next_target.target_id == f"target-{restart_after}"
        assert next_target.position == restart_after
        assert next_target.batch_index == restart_after // 5
        assert next_target.batch_position == restart_after % 5

    replay = restarted.begin_effect("target-0")
    assert replay.execute is False
    assert replay.reason == "already_confirmed"
    assert len(ledger.list_for_task("task")) == restart_after

def test_restore_rejects_released_ready_state_that_still_has_future_wait(
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
    assert cursor.state.ready_batch_index == 0
    assert cursor.state.next_scheduled_intent is not None
    assert cursor.state.next_scheduled_intent.kind is IntentKind.INTER_BATCH_WAIT
    assert cursor.state.next_scheduled_intent.not_before == due.isoformat()

    state = _state_value(memory, "task")
    state["ready_batch_index"] = 1
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

    durable = ledger.list_for_task("task")
    assert len(durable) == 1
    assert durable[0].status is IdempotencyStatus.COMPLETED

@pytest.mark.parametrize(
    "ledger_status",
    [IdempotencyStatus.PENDING, IdempotencyStatus.UNCERTAIN],
)
def test_restore_rejects_confirmed_cursor_with_noncompleted_ledger_without_mutation(
    tmp_path: Path,
    ledger_status: IdempotencyStatus,
) -> None:
    memory, ledger, store = _services(tmp_path)
    cursor = BatchCursor.create(
        memory,
        ledger,
        task_id="task",
        cursor_id="cursor",
        targets=_targets(1),
        batch_size=1,
    )
    grant = cursor.begin_effect("target-0")
    cursor.confirm("target-0", {"ok": True})
    confirmed_state = _state_value(memory, "task")

    with store.connection() as conn:
        conn.execute(
            """
            UPDATE idempotency_records
            SET status = ?, result_json = NULL
            WHERE operation_key = ?
            """,
            (ledger_status.value, grant.operation_key),
        )

    contradictory = ledger.require(grant.operation_key)
    assert contradictory.status is ledger_status
    assert contradictory.result is None

    with pytest.raises(
        BatchCursorStateError,
        match="confirmed cursor target contradicts idempotency evidence",
    ):
        BatchCursor.restore(
            memory,
            ledger,
            task_id="task",
            cursor_id="cursor",
            targets=_targets(1),
            batch_size=1,
        )

    unchanged = ledger.require(grant.operation_key)
    assert unchanged.status is ledger_status
    assert unchanged.result is None
    assert _state_value(memory, "task") == confirmed_state

def test_restore_rejects_confirmed_cursor_with_conflicting_completed_result_without_mutation(
    tmp_path: Path,
) -> None:
    memory, ledger, store = _services(tmp_path)
    cursor = BatchCursor.create(
        memory,
        ledger,
        task_id="task",
        cursor_id="cursor",
        targets=_targets(1),
        batch_size=1,
    )
    grant = cursor.begin_effect("target-0")
    cursor.confirm("target-0", {"ok": True})
    confirmed_state = _state_value(memory, "task")
    tampered_result = {
        "__nika_batch_cursor_completion_v1__": {
            "result": {"ok": False},
            "next_batch_not_before": None,
        }
    }

    with store.connection() as conn:
        conn.execute(
            """
            UPDATE idempotency_records
            SET result_json = ?
            WHERE operation_key = ?
            """,
            (json.dumps(tampered_result, sort_keys=True), grant.operation_key),
        )

    with pytest.raises(
        BatchCursorStateError,
        match="confirmed cursor result contradicts idempotency evidence",
    ):
        BatchCursor.restore(
            memory,
            ledger,
            task_id="task",
            cursor_id="cursor",
            targets=_targets(1),
            batch_size=1,
        )

    assert ledger.require(grant.operation_key).result == tampered_result
    assert _state_value(memory, "task") == confirmed_state


@pytest.mark.parametrize(
    ("confirmed_value", "tampered_value"),
    [(True, 1), (1, 1.0)],
)
def test_restore_rejects_python_equal_json_numeric_aliases(
    tmp_path: Path,
    confirmed_value: object,
    tampered_value: object,
) -> None:
    memory, ledger, store = _services(tmp_path)
    cursor = BatchCursor.create(
        memory,
        ledger,
        task_id="task",
        cursor_id="cursor",
        targets=_targets(1),
        batch_size=1,
    )
    grant = cursor.begin_effect("target-0")
    cursor.confirm("target-0", {"value": confirmed_value})
    confirmed_state = _state_value(memory, "task")
    confirmed_state_json = json.dumps(
        confirmed_state,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    tampered_result = {
        "__nika_batch_cursor_completion_v1__": {
            "result": {"value": tampered_value},
            "next_batch_not_before": None,
        }
    }

    assert {"value": confirmed_value} == {"value": tampered_value}
    assert json.dumps(
        {"value": confirmed_value},
        sort_keys=True,
        separators=(",", ":"),
    ) != json.dumps(
        {"value": tampered_value},
        sort_keys=True,
        separators=(",", ":"),
    )

    with store.connection() as conn:
        conn.execute(
            """
            UPDATE idempotency_records
            SET result_json = ?
            WHERE operation_key = ?
            """,
            (json.dumps(tampered_result, sort_keys=True), grant.operation_key),
        )

    with pytest.raises(
        BatchCursorStateError,
        match="confirmed cursor result contradicts idempotency evidence",
    ):
        BatchCursor.restore(
            memory,
            ledger,
            task_id="task",
            cursor_id="cursor",
            targets=_targets(1),
            batch_size=1,
        )

    durable_result = ledger.require(grant.operation_key).result
    assert durable_result is not None
    durable_value = durable_result["__nika_batch_cursor_completion_v1__"]["result"]["value"]
    assert type(durable_value) is type(tampered_value)
    assert json.dumps(
        _state_value(memory, "task"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ) == confirmed_state_json


@pytest.mark.parametrize(
    "tampered_due",
    [
        None,
        datetime(2031, 1, 2, 3, 4, 5, tzinfo=UTC).isoformat(),
    ],
)
def test_restore_rejects_confirmed_cursor_with_conflicting_completed_deadline_without_mutation(
    tmp_path: Path,
    tampered_due: str | None,
) -> None:
    memory, ledger, store = _services(tmp_path)
    due = datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC)
    cursor = BatchCursor.create(
        memory,
        ledger,
        task_id="task",
        cursor_id="cursor",
        targets=_targets(2),
        batch_size=1,
    )
    grant = cursor.begin_effect("target-0")
    cursor.confirm("target-0", {"ok": True}, next_batch_not_before=due)
    confirmed_state = _state_value(memory, "task")
    intent = cursor.state.next_scheduled_intent
    assert intent is not None
    assert intent.kind is IntentKind.INTER_BATCH_WAIT
    assert intent.deadline_source == "completion"
    assert intent.not_before == due.isoformat()

    tampered_result = {
        "__nika_batch_cursor_completion_v1__": {
            "result": {"ok": True},
            "next_batch_not_before": tampered_due,
        }
    }
    with store.connection() as conn:
        conn.execute(
            """
            UPDATE idempotency_records
            SET result_json = ?
            WHERE operation_key = ?
            """,
            (json.dumps(tampered_result, sort_keys=True), grant.operation_key),
        )

    with pytest.raises(
        BatchCursorStateError,
        match="confirmed cursor deadline contradicts idempotency evidence",
    ):
        BatchCursor.restore(
            memory,
            ledger,
            task_id="task",
            cursor_id="cursor",
            targets=_targets(2),
            batch_size=1,
        )

    assert ledger.require(grant.operation_key).result == tampered_result
    assert _state_value(memory, "task") == confirmed_state


def test_explicit_inter_batch_schedule_survives_restore_as_scheduler_authority(
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
    cursor.confirm("target-0", {"ok": True})
    cursor.schedule_inter_batch_wait(due)

    intent = cursor.state.next_scheduled_intent
    assert intent is not None
    assert intent.kind is IntentKind.INTER_BATCH_WAIT
    assert intent.deadline_source == "scheduler"
    assert intent.not_before == due.isoformat()

    restored = BatchCursor.restore(
        memory,
        ledger,
        task_id="task",
        cursor_id="cursor",
        targets=_targets(2),
        batch_size=1,
    )
    restored_intent = restored.state.next_scheduled_intent
    assert restored_intent is not None
    assert restored_intent.kind is IntentKind.INTER_BATCH_WAIT
    assert restored_intent.deadline_source == "scheduler"
    assert restored_intent.not_before == due.isoformat()

