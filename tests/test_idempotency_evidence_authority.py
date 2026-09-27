from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.runtime.idempotency import (
    IdempotencyConflictError,
    IdempotencyLedger,
    IdempotencyStatus,
)


class _BehavioralText(str):
    def strip(self, *args, **kwargs):
        del args, kwargs
        return "trusted"


def _store_with_task(tmp_path):
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    task = TaskQueue(store).create(workspace_id="workspace", agent_id="agent")
    return store, task.task_id


def _reserve(ledger: IdempotencyLedger, task_id: str, key: str = "effect:1"):
    return ledger.reserve(
        operation_key=key,
        task_id=task_id,
        operation_type="fixture.effect",
        input_fingerprint="sha256:fixture",
    )


def _raw_record(store: SQLiteStore, key: str):
    with store.connection() as conn:
        return conn.execute(
            "SELECT * FROM idempotency_records WHERE operation_key = ?",
            (key,),
        ).fetchone()


def test_completed_result_is_immutable_and_identical_replay_is_noop(tmp_path) -> None:
    store, task_id = _store_with_task(tmp_path)
    ledger = IdempotencyLedger(store)
    _reserve(ledger, task_id)

    completed = ledger.complete("effect:1", {"message_id": "m-1"})
    replay = ledger.complete("effect:1", {"message_id": "m-1"})

    assert replay == completed
    with pytest.raises(IdempotencyConflictError, match="result is immutable"):
        ledger.complete("effect:1", {"message_id": "m-2"})

    persisted = ledger.require("effect:1")
    assert persisted == completed
    assert persisted.result == {"message_id": "m-1"}


def test_concurrent_conflicting_completions_have_one_durable_winner(tmp_path) -> None:
    store, task_id = _store_with_task(tmp_path)
    ledger = IdempotencyLedger(store)
    _reserve(ledger, task_id)
    gate = Barrier(2)

    def complete(candidate: str):
        gate.wait()
        try:
            record = ledger.complete("effect:1", {"winner": candidate})
        except IdempotencyConflictError:
            return "conflict", candidate
        return "completed", record.result["winner"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(complete, ("a", "b")))

    assert sorted(kind for kind, _ in outcomes) == ["completed", "conflict"]
    winner = next(value for kind, value in outcomes if kind == "completed")
    assert ledger.require("effect:1").result == {"winner": winner}


def test_concurrent_identical_reservations_report_one_created_winner(tmp_path) -> None:
    store, task_id = _store_with_task(tmp_path)
    ledger = IdempotencyLedger(store)
    gate = Barrier(2)

    def reserve(_candidate: str):
        gate.wait()
        record, created = ledger.reserve_once(
            operation_key="effect:shared",
            task_id=task_id,
            operation_type="fixture.effect",
            input_fingerprint="sha256:shared",
        )
        return record, created

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(reserve, ("a", "b")))

    assert sorted(created for _, created in outcomes) == [False, True]
    assert outcomes[0][0] == outcomes[1][0]
    assert ledger.require("effect:shared").status is IdempotencyStatus.PENDING


@pytest.mark.parametrize(
    ("field_name", "value_factory"),
    [
        ("operation_key", lambda task_id: _BehavioralText("")),
        ("task_id", lambda task_id: _BehavioralText(task_id)),
        ("operation_type", lambda task_id: _BehavioralText("")),
        ("input_fingerprint", lambda task_id: _BehavioralText("")),
    ],
)
def test_reserve_rejects_behavioral_identity_carriers_without_write(
    tmp_path,
    field_name,
    value_factory,
) -> None:
    store, task_id = _store_with_task(tmp_path)
    ledger = IdempotencyLedger(store)
    values = {
        "operation_key": "effect:identity",
        "task_id": task_id,
        "operation_type": "fixture.effect",
        "input_fingerprint": "sha256:fixture",
    }
    values[field_name] = value_factory(task_id)

    with pytest.raises(TypeError, match=field_name):
        ledger.reserve(**values)

    with store.connection() as conn:
        count = conn.execute("SELECT COUNT(*) FROM idempotency_records").fetchone()[0]
    assert count == 0


def test_lookup_rejects_behavioral_operation_key_carrier(tmp_path) -> None:
    store, task_id = _store_with_task(tmp_path)
    ledger = IdempotencyLedger(store)
    _reserve(ledger, task_id)

    with pytest.raises(TypeError, match="operation_key"):
        ledger.get(_BehavioralText("effect:1"))

    assert ledger.require("effect:1").status is IdempotencyStatus.PENDING


def test_persisted_blob_identity_fails_closed_without_normalization(tmp_path) -> None:
    store, task_id = _store_with_task(tmp_path)
    ledger = IdempotencyLedger(store)
    _reserve(ledger, task_id)

    with store.connection() as conn:
        conn.execute(
            """
            UPDATE idempotency_records
            SET input_fingerprint = CAST(? AS BLOB)
            WHERE operation_key = ?
            """,
            ("sha256:forged", "effect:1"),
        )

    with pytest.raises(RuntimeError, match=r"input_fingerprint.*SQLite storage class"):
        ledger.require("effect:1")

    raw = _raw_record(store, "effect:1")
    assert raw is not None
    assert raw["input_fingerprint"] == b"sha256:forged"


def test_noncompleted_record_cannot_carry_result_evidence(tmp_path) -> None:
    store, task_id = _store_with_task(tmp_path)
    ledger = IdempotencyLedger(store)
    _reserve(ledger, task_id)

    with store.connection() as conn:
        conn.execute(
            "UPDATE idempotency_records SET result_json = ? WHERE operation_key = ?",
            ('{"forged": true}', "effect:1"),
        )

    with pytest.raises(RuntimeError, match="must not carry result evidence"):
        ledger.require("effect:1")


def test_noncanonical_completed_result_json_is_rejected_without_rewrite(tmp_path) -> None:
    store, task_id = _store_with_task(tmp_path)
    ledger = IdempotencyLedger(store)
    _reserve(ledger, task_id)
    ledger.complete("effect:1", {"a": 1, "b": 2})
    forged = '{"b": 2, "a": 1}'

    with store.connection() as conn:
        conn.execute(
            "UPDATE idempotency_records SET result_json = ? WHERE operation_key = ?",
            (forged, "effect:1"),
        )

    with pytest.raises(RuntimeError, match="result_json is not canonical"):
        ledger.require("effect:1")

    raw = _raw_record(store, "effect:1")
    assert raw is not None
    assert raw["result_json"] == forged


def test_naive_durable_timestamp_is_rejected_without_normalization(tmp_path) -> None:
    store, task_id = _store_with_task(tmp_path)
    ledger = IdempotencyLedger(store)
    _reserve(ledger, task_id)
    corrupted = "2026-09-27T12:00:00"

    with store.connection() as conn:
        conn.execute(
            "UPDATE idempotency_records SET updated_at = ? WHERE operation_key = ?",
            (corrupted, "effect:1"),
        )

    with pytest.raises(RuntimeError, match="updated_at must be timezone-aware"):
        ledger.require("effect:1")

    raw = _raw_record(store, "effect:1")
    assert raw is not None
    assert raw["updated_at"] == corrupted


def test_reconciliation_requires_uncertain_state_inside_writer_transaction(tmp_path) -> None:
    store, task_id = _store_with_task(tmp_path)
    ledger = IdempotencyLedger(store)
    _reserve(ledger, task_id)

    with pytest.raises(IdempotencyConflictError, match="only uncertain"):
        ledger.reconcile_completed("effect:1", {"remote_id": "r-1"})

    uncertain = ledger.mark_uncertain("effect:1")
    assert uncertain.status is IdempotencyStatus.UNCERTAIN

    completed = ledger.reconcile_completed("effect:1", {"remote_id": "r-1"})
    assert completed.status is IdempotencyStatus.COMPLETED
    assert completed.result == {"remote_id": "r-1"}

    with pytest.raises(IdempotencyConflictError, match="only uncertain"):
        ledger.reconcile_completed("effect:1", {"remote_id": "r-1"})
