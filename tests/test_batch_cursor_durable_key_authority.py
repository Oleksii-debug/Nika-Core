from __future__ import annotations

import pytest

from nika_core.batch_cursor import BatchCursorStateError, _decode_completion_result


class ArmedDurableText(str):
    armed = False

    def __hash__(self) -> int:
        if self.armed:
            raise AssertionError("durable key hash must not execute")
        return super().__hash__()

    def __eq__(self, other: object) -> bool:
        if self.armed:
            raise AssertionError("durable key equality must not execute")
        return super().__eq__(other)


def test_durable_completion_decoder_rejects_behavioral_outer_key_before_hash() -> None:
    key = ArmedDurableText("__nika_batch_cursor_completion_v1__")
    payload = {
        key: {
            "result": {"ok": True},
            "next_batch_not_before": None,
        }
    }
    key.armed = True

    with pytest.raises(BatchCursorStateError, match="completed effect result is malformed"):
        _decode_completion_result(payload)


def test_durable_completion_decoder_rejects_behavioral_envelope_key_before_hash() -> None:
    key = ArmedDurableText("result")
    envelope = {
        key: {"ok": True},
        "next_batch_not_before": None,
    }
    payload = {"__nika_batch_cursor_completion_v1__": envelope}
    key.armed = True

    with pytest.raises(BatchCursorStateError, match="completed effect envelope is malformed"):
        _decode_completion_result(payload)


class HostilePublicDict(dict[str, object]):
    def items(self):
        raise AssertionError("nested public mapping behavior must not execute")


class HostilePublicList(list[object]):
    def __iter__(self):
        raise AssertionError("nested public sequence behavior must not execute")


def _public_services(tmp_path):
    from datetime import UTC, datetime

    from nika_core.data.sqlite import SQLiteStore
    from nika_core.memory import MemoryService
    from nika_core.runtime.idempotency import IdempotencyLedger

    store = SQLiteStore(tmp_path / "public-json.db")
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
            ("task", "batch-cursor-json", "batch-cursor-json", "created", "{}", now, now),
        )
    return MemoryService(store), IdempotencyLedger(store)


def test_confirm_rejects_nested_behavioral_json_before_ledger_completion(tmp_path) -> None:
    from nika_core.batch_cursor import AttemptState, BatchCursor, BatchTargetSpec
    from nika_core.runtime.idempotency import IdempotencyStatus

    memory, ledger = _public_services(tmp_path)
    cursor = BatchCursor.create(
        memory,
        ledger,
        task_id="task",
        cursor_id="cursor",
        targets=[BatchTargetSpec(target_id="target", payload={"input": 1})],
        batch_size=1,
    )
    grant = cursor.begin_effect("target")

    with pytest.raises(BatchCursorStateError, match="JSON-serializable"):
        cursor.confirm("target", {"nested": HostilePublicDict({"ok": True})})

    assert ledger.require(grant.operation_key).status is IdempotencyStatus.PENDING
    assert cursor.state.targets[0].attempt_state is AttemptState.IN_FLIGHT


def test_mark_uncertain_rejects_nested_behavioral_json_before_ledger_mutation(
    tmp_path,
) -> None:
    from nika_core.batch_cursor import AttemptState, BatchCursor, BatchTargetSpec
    from nika_core.runtime.idempotency import IdempotencyStatus

    memory, ledger = _public_services(tmp_path)
    cursor = BatchCursor.create(
        memory,
        ledger,
        task_id="task",
        cursor_id="cursor",
        targets=[BatchTargetSpec(target_id="target", payload={"input": 1})],
        batch_size=1,
    )
    grant = cursor.begin_effect("target")

    with pytest.raises(BatchCursorStateError, match="JSON-serializable"):
        cursor.mark_uncertain("target", {"nested": HostilePublicList(["evidence"])})

    assert ledger.require(grant.operation_key).status is IdempotencyStatus.PENDING
    assert cursor.state.targets[0].attempt_state is AttemptState.IN_FLIGHT

