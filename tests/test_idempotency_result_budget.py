from __future__ import annotations

import json

import pytest

import nika_core.runtime.idempotency as idempotency_module
from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.runtime.idempotency import IdempotencyLedger, IdempotencyStatus


def _ledger(tmp_path):
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    task = TaskQueue(store).create(workspace_id="workspace", agent_id="agent")
    ledger = IdempotencyLedger(store)
    ledger.reserve(
        operation_key="effect:budget",
        task_id=task.task_id,
        operation_type="fixture.effect",
        input_fingerprint="sha256:fixture",
    )
    return store, ledger


def _raw(store):
    with store.connection() as conn:
        return conn.execute(
            "SELECT status, result_json FROM idempotency_records "
            "WHERE operation_key = ?",
            ("effect:budget",),
        ).fetchone()


def test_oversized_completion_is_atomic_and_a_small_retry_succeeds(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(idempotency_module, "_MAX_RESULT_BYTES", 256)
    store, ledger = _ledger(tmp_path)
    before = ledger.require("effect:budget")

    with pytest.raises(ValueError, match="JSON serializable") as failure:
        ledger.complete("effect:budget", {"output": "x" * 256})
    assert failure.value.__cause__ is not None
    assert "byte length" in str(failure.value.__cause__)
    assert ledger.require("effect:budget") == before
    assert _raw(store)["result_json"] is None

    # The UTF-8 limit is inclusive, not one byte smaller than advertised.
    payload = "x" * (256 - len(json.dumps({"output": ""})))
    expected = {"output": payload}
    assert len(json.dumps(expected).encode("utf-8")) == 256
    assert ledger.complete("effect:budget", expected).result == expected
    assert IdempotencyLedger(store).require("effect:budget").result == expected


def test_wide_completion_fails_before_durable_status_change(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(idempotency_module, "_MAX_RESULT_NODES", 64)
    store, ledger = _ledger(tmp_path)
    before = ledger.require("effect:budget")

    with pytest.raises(ValueError, match="JSON serializable") as failure:
        ledger.complete("effect:budget", {"output": [0] * 63})
    assert failure.value.__cause__ is not None
    assert "node count" in str(failure.value.__cause__)
    assert ledger.require("effect:budget") == before
    assert _raw(store)["status"] == IdempotencyStatus.PENDING.value

    expected = {"output": [0] * 62}
    assert ledger.complete("effect:budget", expected).result == expected
    assert IdempotencyLedger(store).require("effect:budget").result == expected


def test_oversized_persisted_result_fails_before_decode_without_rewrite(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(idempotency_module, "_MAX_RESULT_BYTES", 256)
    store, ledger = _ledger(tmp_path)
    forged = '{"output":"' + ("x" * 256) + '"}'
    with store.connection() as conn:
        conn.execute(
            "UPDATE idempotency_records SET result_json = ? "
            "WHERE operation_key = ?",
            (forged, "effect:budget"),
        )

    with pytest.raises(RuntimeError, match="exceeds maximum byte length"):
        ledger.require("effect:budget")
    assert _raw(store)["result_json"] == forged
    assert _raw(store)["status"] == IdempotencyStatus.PENDING.value


def test_wide_persisted_result_fails_closed_without_rewrite(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(idempotency_module, "_MAX_RESULT_NODES", 64)
    store, ledger = _ledger(tmp_path)
    forged = json.dumps({"output": [0] * 63}, sort_keys=True)
    with store.connection() as conn:
        conn.execute(
            "UPDATE idempotency_records SET result_json = ? "
            "WHERE operation_key = ?",
            (forged, "effect:budget"),
        )

    with pytest.raises(RuntimeError, match="result_json is invalid"):
        ledger.require("effect:budget")
    assert _raw(store)["result_json"] == forged


def test_multibyte_utf8_result_cannot_bypass_character_precheck(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(idempotency_module, "_MAX_RESULT_BYTES", 256)
    store, ledger = _ledger(tmp_path)
    forged = json.dumps({"output": "ї" * 140}, ensure_ascii=False, sort_keys=True)
    assert len(forged) < 256
    assert len(forged.encode("utf-8")) > 256
    with store.connection() as conn:
        conn.execute(
            "UPDATE idempotency_records SET result_json = ? "
            "WHERE operation_key = ?",
            (forged, "effect:budget"),
        )

    with pytest.raises(RuntimeError, match="result_json is invalid"):
        ledger.require("effect:budget")
    assert _raw(store)["result_json"] == forged


def test_tuple_and_sibling_nodes_share_one_budget(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(idempotency_module, "_MAX_RESULT_NODES", 9)
    store, ledger = _ledger(tmp_path)
    rejected = {"first": [0] * 4, "second": (0,) * 4}

    with pytest.raises(ValueError, match="JSON serializable") as failure:
        ledger.complete("effect:budget", rejected)
    assert failure.value.__cause__ is not None
    assert "node count" in str(failure.value.__cause__)
    assert _raw(store)["result_json"] is None

    accepted = {"first": [0] * 3, "second": (0,) * 3}
    decoded = {"first": [0] * 3, "second": [0] * 3}
    assert ledger.complete("effect:budget", accepted).result == decoded
    assert IdempotencyLedger(store).require("effect:budget").result == decoded


def test_giant_string_is_rejected_before_json_serialization(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(idempotency_module, "_MAX_RESULT_BYTES", 256)
    store, ledger = _ledger(tmp_path)

    with pytest.raises(ValueError, match="JSON serializable") as failure:
        ledger.complete("effect:budget", {"output": "x" * 257})
    assert failure.value.__cause__ is not None
    assert "string exceeds maximum" in str(failure.value.__cause__)
    assert _raw(store)["status"] == IdempotencyStatus.PENDING.value
    assert _raw(store)["result_json"] is None


def test_wide_root_mapping_rejected_before_initial_copy(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(idempotency_module, "_MAX_RESULT_NODES", 64)
    store, ledger = _ledger(tmp_path)
    too_wide = {f"key-{index}": index for index in range(64)}

    with pytest.raises(ValueError, match="JSON serializable") as failure:
        ledger.complete("effect:budget", too_wide)
    assert failure.value.__cause__ is not None
    assert "node count" in str(failure.value.__cause__)
    assert _raw(store)["status"] == IdempotencyStatus.PENDING.value
    assert _raw(store)["result_json"] is None
