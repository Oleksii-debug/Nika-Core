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


class _BehavioralDict(dict):
    def items(self):
        raise AssertionError("behavioral dict.items() must not execute")

    def keys(self):
        raise AssertionError("behavioral dict.keys() must not execute")

    def __iter__(self):
        raise AssertionError("behavioral dict.__iter__() must not execute")

    def __getitem__(self, key):
        del key
        raise AssertionError("behavioral dict.__getitem__() must not execute")


class _BehavioralList(list):
    def __iter__(self):
        raise AssertionError("behavioral list.__iter__() must not execute")


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



@pytest.mark.parametrize(
    "result",
    (
        {"value": "\ud800"},
        {"key-\ud800": "value"},
        {"nested": ["ok", {"value": "\ud800"}]},
    ),
)
def test_complete_rejects_non_utf8_result_without_status_mutation(
    tmp_path,
    result: dict[str, object],
) -> None:
    store, task_id = _store_with_task(tmp_path)
    ledger = IdempotencyLedger(store)
    _reserve(ledger, task_id)

    with pytest.raises(ValueError, match="valid UTF-8"):
        ledger.complete("effect:1", result)

    persisted = ledger.require("effect:1")
    assert persisted.status is IdempotencyStatus.PENDING
    assert persisted.result is None


@pytest.mark.parametrize(
    "result",
    (
        {1: "value"},
        {None: "value"},
        {_BehavioralText("key"): "value"},
        {"nested": [{False: "value"}]},
    ),
)
def test_complete_rejects_non_text_object_keys_without_status_mutation(
    tmp_path,
    result: dict[object, object],
) -> None:
    store, task_id = _store_with_task(tmp_path)
    ledger = IdempotencyLedger(store)
    _reserve(ledger, task_id)

    with pytest.raises(ValueError, match="object keys must be exact text"):
        ledger.complete("effect:1", result)

    persisted = ledger.require("effect:1")
    assert persisted.status is IdempotencyStatus.PENDING
    assert persisted.result is None


@pytest.mark.parametrize(
    "result",
    (
        {"nested": _BehavioralDict({"safe": "value"})},
        {"nested": _BehavioralList(["safe"])},
    ),
)
def test_complete_rejects_behavioral_json_containers_without_executing_them(
    tmp_path,
    result: dict[str, object],
) -> None:
    store, task_id = _store_with_task(tmp_path)
    ledger = IdempotencyLedger(store)
    _reserve(ledger, task_id)

    with pytest.raises(ValueError, match="containers must use exact built-in types"):
        ledger.complete("effect:1", result)

    persisted = ledger.require("effect:1")
    assert persisted.status is IdempotencyStatus.PENDING
    assert persisted.result is None


def test_complete_rejects_behavioral_root_mapping_without_executing_it(tmp_path) -> None:
    store, task_id = _store_with_task(tmp_path)
    ledger = IdempotencyLedger(store)
    _reserve(ledger, task_id)
    hostile = _BehavioralDict({"safe": "value"})

    with pytest.raises(TypeError, match="exact built-in dict"):
        ledger.complete("effect:1", hostile)

    persisted = ledger.require("effect:1")
    assert persisted.status is IdempotencyStatus.PENDING
    assert persisted.result is None


def test_complete_rejects_circular_result_without_status_mutation(tmp_path) -> None:
    store, task_id = _store_with_task(tmp_path)
    ledger = IdempotencyLedger(store)
    _reserve(ledger, task_id)
    circular: list[object] = []
    circular.append(circular)

    with pytest.raises(ValueError, match="must not contain circular containers"):
        ledger.complete("effect:1", {"circular": circular})

    persisted = ledger.require("effect:1")
    assert persisted.status is IdempotencyStatus.PENDING
    assert persisted.result is None


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



@pytest.mark.parametrize(
    "field_name",
    ("operation_key", "task_id", "operation_type", "input_fingerprint"),
)
def test_reserve_rejects_non_utf8_identity_without_write(
    tmp_path,
    field_name: str,
) -> None:
    store, task_id = _store_with_task(tmp_path)
    ledger = IdempotencyLedger(store)
    values = {
        "operation_key": "effect:utf8",
        "task_id": task_id,
        "operation_type": "fixture.effect",
        "input_fingerprint": "sha256:utf8",
    }
    values[field_name] = "\ud800"

    with pytest.raises(ValueError, match=rf"{field_name} must be valid UTF-8 text"):
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



def test_blob_operation_key_alias_cannot_hide_existing_effect_or_allow_duplicate(tmp_path) -> None:
    store, task_id = _store_with_task(tmp_path)
    ledger = IdempotencyLedger(store)
    _reserve(ledger, task_id, key="effect:alias")

    with store.connection() as conn:
        conn.execute(
            """
            UPDATE idempotency_records
            SET operation_key = CAST(? AS BLOB)
            WHERE operation_key = ?
            """,
            ("effect:alias", "effect:alias"),
        )

    with pytest.raises(RuntimeError, match=r"operation_key.*SQLite storage class"):
        ledger.get("effect:alias")
    with pytest.raises(RuntimeError, match=r"operation_key.*SQLite storage class"):
        _reserve(ledger, task_id, key="effect:alias")

    with store.connection() as conn:
        rows = conn.execute(
            "SELECT operation_key FROM idempotency_records"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["operation_key"] == b"effect:alias"


@pytest.mark.parametrize(
    "mutation",
    ["complete", "mark_uncertain", "release_pending"],
)
def test_blob_operation_key_alias_cannot_disappear_from_mutation_boundary(
    tmp_path,
    mutation: str,
) -> None:
    store, task_id = _store_with_task(tmp_path)
    ledger = IdempotencyLedger(store)
    _reserve(ledger, task_id, key="effect:alias-mutation")

    with store.connection() as conn:
        conn.execute(
            """
            UPDATE idempotency_records
            SET operation_key = CAST(? AS BLOB)
            WHERE operation_key = ?
            """,
            ("effect:alias-mutation", "effect:alias-mutation"),
        )

    with pytest.raises(RuntimeError, match=r"operation_key.*SQLite storage class"):
        if mutation == "complete":
            ledger.complete("effect:alias-mutation", {"ok": True})
        elif mutation == "mark_uncertain":
            ledger.mark_uncertain("effect:alias-mutation")
        else:
            ledger.release_pending("effect:alias-mutation")

    with store.connection() as conn:
        raw = conn.execute(
            "SELECT operation_key, status, result_json FROM idempotency_records"
        ).fetchone()
    assert raw is not None
    assert raw["operation_key"] == b"effect:alias-mutation"
    assert raw["status"] == IdempotencyStatus.PENDING.value
    assert raw["result_json"] is None


def test_blob_operation_key_alias_cannot_disappear_from_reconciliation_boundary(
    tmp_path,
) -> None:
    store, task_id = _store_with_task(tmp_path)
    ledger = IdempotencyLedger(store)
    _reserve(ledger, task_id, key="effect:alias-reconcile")
    ledger.mark_uncertain("effect:alias-reconcile")

    with store.connection() as conn:
        conn.execute(
            """
            UPDATE idempotency_records
            SET operation_key = CAST(? AS BLOB)
            WHERE operation_key = ?
            """,
            ("effect:alias-reconcile", "effect:alias-reconcile"),
        )

    with pytest.raises(RuntimeError, match=r"operation_key.*SQLite storage class"):
        ledger.reconcile_completed(
            "effect:alias-reconcile",
            {"remote_id": "r-1"},
        )

    with store.connection() as conn:
        raw = conn.execute(
            "SELECT operation_key, status, result_json FROM idempotency_records"
        ).fetchone()
    assert raw is not None
    assert raw["operation_key"] == b"effect:alias-reconcile"
    assert raw["status"] == IdempotencyStatus.UNCERTAIN.value
    assert raw["result_json"] is None


def test_blob_task_id_alias_cannot_disappear_from_task_inventory(tmp_path) -> None:
    store, task_id = _store_with_task(tmp_path)
    ledger = IdempotencyLedger(store)
    _reserve(ledger, task_id)

    with store.connection() as conn:
        # Simulate out-of-band/tampered durable storage. Normal SQLiteStore
        # connections enable foreign_keys, so the corruption injection must
        # explicitly bypass the relational guard before testing ledger recovery.
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute(
            """
            UPDATE idempotency_records
            SET task_id = CAST(? AS BLOB)
            WHERE operation_key = ?
            """,
            (task_id, "effect:1"),
        )

    with pytest.raises(RuntimeError, match=r"task_id.*SQLite storage class"):
        ledger.list_for_task(task_id)
    with pytest.raises(RuntimeError, match=r"task_id.*SQLite storage class"):
        ledger.promote_pending_to_uncertain(task_id)

    raw = _raw_record(store, "effect:1")
    assert raw is not None
    assert raw["task_id"] == task_id.encode()
    assert raw["status"] == IdempotencyStatus.PENDING.value


def test_duplicate_storage_aliases_fail_closed_before_effect_reservation(tmp_path) -> None:
    store, task_id = _store_with_task(tmp_path)
    ledger = IdempotencyLedger(store)
    _reserve(ledger, task_id, key="effect:alias")

    with store.connection() as conn:
        original = conn.execute(
            "SELECT * FROM idempotency_records WHERE operation_key = ?",
            ("effect:alias",),
        ).fetchone()
        conn.execute(
            """
            INSERT INTO idempotency_records(
                operation_key, task_id, operation_type, input_fingerprint,
                status, result_json, created_at, updated_at
            ) VALUES (CAST(? AS BLOB), ?, ?, ?, ?, NULL, ?, ?)
            """,
            (
                "effect:alias",
                task_id,
                original["operation_type"],
                original["input_fingerprint"],
                IdempotencyStatus.PENDING.value,
                original["created_at"],
                original["updated_at"],
            ),
        )

    with pytest.raises(RuntimeError, match="multiple persisted.*storage aliases"):
        ledger.get("effect:alias")
    with pytest.raises(RuntimeError, match="multiple persisted.*storage aliases"):
        _reserve(ledger, task_id, key="effect:alias")

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


def test_pending_canonical_metadata_remains_readable_for_recovery_claims(tmp_path) -> None:
    store, task_id = _store_with_task(tmp_path)
    ledger = IdempotencyLedger(store)
    _reserve(ledger, task_id)
    metadata = '{"claim_id": "c-1", "schema": "recovery-v1"}'

    with store.connection() as conn:
        conn.execute(
            "UPDATE idempotency_records SET result_json = ? WHERE operation_key = ?",
            (metadata, "effect:1"),
        )

    record = ledger.require("effect:1")
    assert record.status is IdempotencyStatus.PENDING
    assert record.result == {"claim_id": "c-1", "schema": "recovery-v1"}



@pytest.mark.parametrize("unsafe_number", [float("nan"), float("inf"), float("-inf")])
def test_non_json_numeric_completion_evidence_is_rejected_atomically(
    tmp_path,
    unsafe_number,
) -> None:
    store, task_id = _store_with_task(tmp_path)
    ledger = IdempotencyLedger(store)
    _reserve(ledger, task_id)

    before = ledger.require("effect:1")
    with pytest.raises(ValueError, match="JSON serializable"):
        ledger.complete("effect:1", {"unsafe": unsafe_number})

    after = ledger.require("effect:1")
    assert after == before
    assert after.status is IdempotencyStatus.PENDING
    assert after.result is None


def test_persisted_non_json_numeric_evidence_is_classified_as_corruption(tmp_path) -> None:
    store, task_id = _store_with_task(tmp_path)
    ledger = IdempotencyLedger(store)
    _reserve(ledger, task_id)
    forged = '{"unsafe": NaN}'

    with store.connection() as conn:
        conn.execute(
            "UPDATE idempotency_records SET result_json = ? WHERE operation_key = ?",
            (forged, "effect:1"),
        )

    with pytest.raises(RuntimeError, match="result_json is invalid"):
        ledger.require("effect:1")

    raw = _raw_record(store, "effect:1")
    assert raw is not None
    assert raw["result_json"] == forged

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



def test_recovery_promotion_rejects_corrupt_pending_evidence_without_rewrite(tmp_path) -> None:
    store, task_id = _store_with_task(tmp_path)
    ledger = IdempotencyLedger(store)
    _reserve(ledger, task_id)
    forged = '{"schema":"recovery-v1","claim_id":"c-1"}'

    with store.connection() as conn:
        conn.execute(
            """
            UPDATE idempotency_records
            SET result_json = ?
            WHERE operation_key = ?
            """,
            (forged, "effect:1"),
        )

    with pytest.raises(RuntimeError, match="result_json is not canonical"):
        ledger.promote_pending_to_uncertain(task_id)

    raw = _raw_record(store, "effect:1")
    assert raw is not None
    assert raw["status"] == IdempotencyStatus.PENDING.value
    assert raw["result_json"] == forged


def test_recovery_promotion_rejects_corrupt_timestamp_without_rewrite(tmp_path) -> None:
    store, task_id = _store_with_task(tmp_path)
    ledger = IdempotencyLedger(store)
    _reserve(ledger, task_id)
    corrupted = "2026-09-27T12:00:00"

    with store.connection() as conn:
        conn.execute(
            """
            UPDATE idempotency_records
            SET updated_at = ?
            WHERE operation_key = ?
            """,
            (corrupted, "effect:1"),
        )

    with pytest.raises(RuntimeError, match="updated_at must be timezone-aware"):
        ledger.promote_pending_to_uncertain(task_id)

    raw = _raw_record(store, "effect:1")
    assert raw is not None
    assert raw["status"] == IdempotencyStatus.PENDING.value
    assert raw["updated_at"] == corrupted



def test_recovery_promotion_rejects_corrupt_task_status_without_skipping_record(tmp_path) -> None:
    store, task_id = _store_with_task(tmp_path)
    ledger = IdempotencyLedger(store)
    _reserve(ledger, task_id, key="effect:corrupt-status")
    _reserve(ledger, task_id, key="effect:pending")

    with store.connection() as conn:
        conn.execute(
            """
            UPDATE idempotency_records
            SET status = ?
            WHERE operation_key = ?
            """,
            ("pending-corrupt", "effect:corrupt-status"),
        )

    with pytest.raises(RuntimeError, match="status is unsupported"):
        ledger.promote_pending_to_uncertain(task_id)

    corrupt = _raw_record(store, "effect:corrupt-status")
    pending = _raw_record(store, "effect:pending")
    assert corrupt is not None and pending is not None
    assert corrupt["status"] == "pending-corrupt"
    assert pending["status"] == IdempotencyStatus.PENDING.value

def test_recovery_promotion_preserves_atomic_key_set(tmp_path) -> None:
    store, task_id = _store_with_task(tmp_path)
    ledger = IdempotencyLedger(store)
    _reserve(ledger, task_id, key="effect:a")
    _reserve(ledger, task_id, key="effect:b")

    promoted = ledger.promote_pending_to_uncertain(task_id)

    assert promoted == ("effect:a", "effect:b")
    assert ledger.require("effect:a").status is IdempotencyStatus.UNCERTAIN
    assert ledger.require("effect:b").status is IdempotencyStatus.UNCERTAIN

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
