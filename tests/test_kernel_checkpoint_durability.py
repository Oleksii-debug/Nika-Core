from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.checkpoint import CheckpointService
from nika_core.kernel.task_queue import TaskQueue


def _build_service(tmp_path: Path) -> tuple[SQLiteStore, str, CheckpointService]:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    task = TaskQueue(store).create(
        workspace_id="checkpoint-tests",
        agent_id="checkpoint-tests",
        payload={"goal": "durable recovery"},
    )
    return store, task.task_id, CheckpointService(store)


def _insert_raw_checkpoint(
    store: SQLiteStore,
    *,
    task_id: str,
    checkpoint_id: str,
    payload_json: str,
) -> None:
    checksum = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
    with store.connection() as conn:
        conn.execute(
            """
            INSERT INTO checkpoints(
                checkpoint_id, task_id, stage, payload_json, checksum_sha256, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                checkpoint_id,
                task_id,
                "raw",
                payload_json,
                checksum,
                "2026-08-26T20:00:00+00:00",
            ),
        )


def test_latest_uses_insertion_order_when_wall_clock_moves_backward(tmp_path: Path) -> None:
    store, task_id, checkpoints = _build_service(tmp_path)
    first = checkpoints.save(task_id=task_id, stage="first", payload={"revision": 1})
    second = checkpoints.save(task_id=task_id, stage="second", payload={"revision": 2})

    with store.connection() as conn:
        conn.execute(
            "UPDATE checkpoints SET created_at = ? WHERE checkpoint_id = ?",
            ("2099-01-01T00:00:00+00:00", first.checkpoint_id),
        )
        conn.execute(
            "UPDATE checkpoints SET created_at = ? WHERE checkpoint_id = ?",
            ("2000-01-01T00:00:00+00:00", second.checkpoint_id),
        )

    latest = CheckpointService(store).latest(task_id)
    assert latest is not None
    assert latest.checkpoint_id == second.checkpoint_id
    assert latest.payload == {"revision": 2}


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_save_rejects_non_finite_json_without_durable_row(
    tmp_path: Path,
    value: float,
) -> None:
    store, task_id, checkpoints = _build_service(tmp_path)

    with pytest.raises(ValueError, match="finite"):
        checkpoints.save(task_id=task_id, stage="unsafe", payload={"value": value})

    with store.connection() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM checkpoints WHERE task_id = ?",
            (task_id,),
        ).fetchone()[0]
    assert count == 0


def test_latest_rejects_non_object_payload_even_with_matching_checksum(tmp_path: Path) -> None:
    store, task_id, checkpoints = _build_service(tmp_path)
    _insert_raw_checkpoint(
        store,
        task_id=task_id,
        checkpoint_id="non-object",
        payload_json="[]",
    )

    with pytest.raises(ValueError, match="JSON object"):
        checkpoints.latest(task_id)


def test_latest_rejects_non_canonical_payload_even_with_matching_checksum(tmp_path: Path) -> None:
    store, task_id, checkpoints = _build_service(tmp_path)
    _insert_raw_checkpoint(
        store,
        task_id=task_id,
        checkpoint_id="non-canonical",
        payload_json='{"b":2,"a":1}',
    )

    with pytest.raises(ValueError, match="canonical JSON"):
        checkpoints.latest(task_id)


def test_unicode_nested_payload_round_trips_after_restart(tmp_path: Path) -> None:
    store, task_id, checkpoints = _build_service(tmp_path)
    payload = {
        "ключ": "значення",
        "nested": {"items": [1, True, None, "тест"]},
    }
    saved = checkpoints.save(task_id=task_id, stage="unicode", payload=payload)

    loaded = CheckpointService(store).latest(task_id)
    assert loaded is not None
    assert loaded.checkpoint_id == saved.checkpoint_id
    assert loaded.payload == payload
