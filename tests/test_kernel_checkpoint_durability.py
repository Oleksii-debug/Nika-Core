from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel import checkpoint as checkpoint_module
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
    payload_json: str | bytes,
) -> None:
    payload_bytes = (
        payload_json.encode("utf-8") if isinstance(payload_json, str) else payload_json
    )
    checksum = hashlib.sha256(payload_bytes).hexdigest()
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


def test_latest_rejects_non_finite_durable_json_with_matching_checksum(tmp_path: Path) -> None:
    store, task_id, checkpoints = _build_service(tmp_path)
    _insert_raw_checkpoint(
        store,
        task_id=task_id,
        checkpoint_id="non-finite",
        payload_json='{"value":NaN}',
    )

    with pytest.raises(ValueError, match="invalid JSON"):
        checkpoints.latest(task_id)


def test_latest_rejects_malformed_durable_json_with_matching_checksum(tmp_path: Path) -> None:
    store, task_id, checkpoints = _build_service(tmp_path)
    _insert_raw_checkpoint(
        store,
        task_id=task_id,
        checkpoint_id="malformed",
        payload_json='{"value":',
    )

    with pytest.raises(ValueError, match="invalid JSON"):
        checkpoints.latest(task_id)


def test_latest_rejects_non_object_payload_even_with_matching_checksum(tmp_path: Path) -> None:
    store, task_id, checkpoints = _build_service(tmp_path)
    _insert_raw_checkpoint(
        store,
        task_id=task_id,
        checkpoint_id="non-object",
        payload_json="[]",
    )

    with pytest.raises(TypeError, match="JSON object"):
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


def test_latest_rejects_checksum_tamper(tmp_path: Path) -> None:
    store, task_id, checkpoints = _build_service(tmp_path)
    saved = checkpoints.save(task_id=task_id, stage="valid", payload={"revision": 1})

    with store.connection() as conn:
        conn.execute(
            "UPDATE checkpoints SET payload_json = ? WHERE checkpoint_id = ?",
            ('{"revision":2}', saved.checkpoint_id),
        )

    with pytest.raises(ValueError, match="checksum mismatch"):
        checkpoints.latest(task_id)


def test_corrupt_newest_checkpoint_does_not_fall_back_to_stale_state(tmp_path: Path) -> None:
    store, task_id, checkpoints = _build_service(tmp_path)
    checkpoints.save(task_id=task_id, stage="valid", payload={"revision": 1})
    _insert_raw_checkpoint(
        store,
        task_id=task_id,
        checkpoint_id="newest-corrupt",
        payload_json='{"revision":',
    )

    with pytest.raises(ValueError, match="invalid JSON"):
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


def test_save_return_matches_durable_canonical_payload_and_detaches_caller(
    tmp_path: Path,
) -> None:
    store, task_id, checkpoints = _build_service(tmp_path)
    nested = {"items": [1, 2]}
    payload: dict[str, object] = {
        "nested": nested,
        "sequence": (1, 2),
    }

    saved = checkpoints.save(task_id=task_id, stage="snapshot", payload=payload)
    nested["items"].append(3)

    assert saved.payload == {
        "nested": {"items": [1, 2]},
        "sequence": [1, 2],
    }
    loaded = CheckpointService(store).latest(task_id)
    assert loaded is not None
    assert loaded.payload == saved.payload
    assert loaded.checksum_sha256 == saved.checksum_sha256


def test_latest_rejects_blob_payload_storage_class(tmp_path: Path) -> None:
    store, task_id, checkpoints = _build_service(tmp_path)
    _insert_raw_checkpoint(
        store,
        task_id=task_id,
        checkpoint_id="blob-payload",
        payload_json=b'{"revision":1}',
    )

    with pytest.raises(TypeError, match="payload storage must be SQLite TEXT"):
        checkpoints.latest(task_id)


def test_latest_rejects_blob_checksum_storage_class(tmp_path: Path) -> None:
    store, task_id, checkpoints = _build_service(tmp_path)
    saved = checkpoints.save(task_id=task_id, stage="valid", payload={"revision": 1})

    with store.connection() as conn:
        conn.execute(
            "UPDATE checkpoints SET checksum_sha256 = ? WHERE checkpoint_id = ?",
            (saved.checksum_sha256.encode("ascii"), saved.checkpoint_id),
        )

    with pytest.raises(TypeError, match="checksum storage must be SQLite TEXT"):
        checkpoints.latest(task_id)


def test_save_normalizes_encoder_recursion_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, task_id, checkpoints = _build_service(tmp_path)

    def recurse(*_args: object, **_kwargs: object) -> str:
        raise RecursionError("synthetic encoder recursion")

    monkeypatch.setattr(checkpoint_module.json, "dumps", recurse)
    with pytest.raises(ValueError, match="JSON object with finite values"):
        checkpoints.save(task_id=task_id, stage="too-deep", payload={"value": 1})

    with store.connection() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM checkpoints WHERE task_id = ?",
            (task_id,),
        ).fetchone()[0]
    assert count == 0


def test_latest_normalizes_decoder_recursion_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, task_id, checkpoints = _build_service(tmp_path)
    _insert_raw_checkpoint(
        store,
        task_id=task_id,
        checkpoint_id="too-deep",
        payload_json='{"value":1}',
    )

    def recurse(*_args: object, **_kwargs: object) -> object:
        raise RecursionError("synthetic decoder recursion")

    monkeypatch.setattr(checkpoint_module.json, "loads", recurse)
    with pytest.raises(ValueError, match="invalid JSON"):
        checkpoints.latest(task_id)


def _nested_payload(depth: int) -> dict[str, object]:
    value: object = 0
    for _ in range(depth - 1):
        value = {"value": value}
    assert isinstance(value, dict)
    return value


def test_save_rejects_excessive_nodes_before_json_encoding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, task_id, checkpoints = _build_service(tmp_path)
    payload: dict[str, object] = {"items": [0] * 9_999}

    def unexpected_encoder(*_args: object, **_kwargs: object) -> str:
        pytest.fail("oversized node graph reached json.dumps")

    monkeypatch.setattr(checkpoint_module.json, "dumps", unexpected_encoder)
    with pytest.raises(ValueError, match="node limit"):
        checkpoints.save(task_id=task_id, stage="too-wide", payload=payload)

    with store.connection() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM checkpoints WHERE task_id = ?",
            (task_id,),
        ).fetchone()[0]
    assert count == 0


def test_save_rejects_aggregate_integer_bytes_before_json_encoding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, task_id, checkpoints = _build_service(tmp_path)
    large_integer = (1 << 4096) - 1
    payload: dict[str, object] = {"items": [large_integer] * 900}

    def unexpected_encoder(*_args: object, **_kwargs: object) -> str:
        pytest.fail("oversized canonical JSON reached json.dumps")

    monkeypatch.setattr(checkpoint_module.json, "dumps", unexpected_encoder)
    with pytest.raises(ValueError, match="UTF-8 byte limit"):
        checkpoints.save(task_id=task_id, stage="integer-bytes", payload=payload)

    with store.connection() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM checkpoints WHERE task_id = ?",
            (task_id,),
        ).fetchone()[0]
    assert count == 0


def test_save_accepts_exact_node_and_depth_boundaries(tmp_path: Path) -> None:
    _store, task_id, checkpoints = _build_service(tmp_path)
    exact_nodes: dict[str, object] = {"items": [0] * 9_998}

    wide = checkpoints.save(task_id=task_id, stage="wide-boundary", payload=exact_nodes)
    deep = checkpoints.save(
        task_id=task_id,
        stage="depth-boundary",
        payload=_nested_payload(64),
    )

    assert len(wide.payload["items"]) == 9_998
    assert deep.payload == _nested_payload(64)


def test_save_rejects_depth_above_boundary(tmp_path: Path) -> None:
    store, task_id, checkpoints = _build_service(tmp_path)

    with pytest.raises(ValueError, match="depth limit"):
        checkpoints.save(
            task_id=task_id,
            stage="too-deep",
            payload=_nested_payload(65),
        )

    with store.connection() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM checkpoints WHERE task_id = ?",
            (task_id,),
        ).fetchone()[0]
    assert count == 0


def test_save_rejects_integer_above_bit_boundary(tmp_path: Path) -> None:
    store, task_id, checkpoints = _build_service(tmp_path)

    accepted = checkpoints.save(
        task_id=task_id,
        stage="integer-boundary",
        payload={"value": 1 << 4095},
    )
    assert accepted.payload["value"] == 1 << 4095

    with pytest.raises(ValueError, match="integer exceeds"):
        checkpoints.save(
            task_id=task_id,
            stage="integer-overflow",
            payload={"value": 1 << 4096},
        )

    with store.connection() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM checkpoints WHERE task_id = ?",
            (task_id,),
        ).fetchone()[0]
    assert count == 1


def test_save_rejects_behavioral_container_subclasses_before_hooks(
    tmp_path: Path,
) -> None:
    store, task_id, checkpoints = _build_service(tmp_path)

    class HostileList(list[object]):
        def __len__(self) -> int:
            raise AssertionError("hostile list len hook executed")

        def __iter__(self):
            raise AssertionError("hostile list iteration hook executed")

    class HostileDict(dict[str, object]):
        def __len__(self) -> int:
            raise AssertionError("hostile dict len hook executed")

        def items(self):
            raise AssertionError("hostile dict items hook executed")

    with pytest.raises(ValueError, match="containers must be built-in"):
        checkpoints.save(
            task_id=task_id,
            stage="hostile-list",
            payload={"items": HostileList([1, 2, 3])},
        )
    with pytest.raises(ValueError, match="containers must be built-in"):
        checkpoints.save(
            task_id=task_id,
            stage="hostile-dict",
            payload=HostileDict({"value": 1}),
        )

    with store.connection() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM checkpoints WHERE task_id = ?",
            (task_id,),
        ).fetchone()[0]
    assert count == 0


def test_save_rejects_scalar_subclasses_before_json_encoding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, task_id, checkpoints = _build_service(tmp_path)

    class TextSubclass(str):
        pass

    class IntegerSubclass(int):
        pass

    def unexpected_encoder(*_args: object, **_kwargs: object) -> str:
        pytest.fail("scalar subclass reached json.dumps")

    monkeypatch.setattr(checkpoint_module.json, "dumps", unexpected_encoder)
    payloads: tuple[dict[object, object], ...] = (
        {"value": TextSubclass("payload")},
        {TextSubclass("key"): 1},
        {"value": IntegerSubclass(7)},
        {IntegerSubclass(7): "value"},
    )
    for index, payload in enumerate(payloads):
        with pytest.raises(ValueError, match="must be built-in types"):
            checkpoints.save(
                task_id=task_id,
                stage=f"hostile-scalar-{index}",
                payload=payload,  # type: ignore[arg-type]
            )

    with store.connection() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM checkpoints WHERE task_id = ?",
            (task_id,),
        ).fetchone()[0]
    assert count == 0


def test_save_rejects_invalid_utf8_text_before_persistence(tmp_path: Path) -> None:
    store, task_id, checkpoints = _build_service(tmp_path)

    with pytest.raises(ValueError, match="valid UTF-8"):
        checkpoints.save(
            task_id=task_id,
            stage="invalid-text",
            payload={"value": "\ud800"},
        )

    with store.connection() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM checkpoints WHERE task_id = ?",
            (task_id,),
        ).fetchone()[0]
    assert count == 0


def test_latest_rejects_oversized_durable_payload(tmp_path: Path) -> None:
    store, task_id, checkpoints = _build_service(tmp_path)
    oversized = '{"value":"' + ("a" * (1024 * 1024)) + '"}'
    _insert_raw_checkpoint(
        store,
        task_id=task_id,
        checkpoint_id="oversized",
        payload_json=oversized,
    )

    with pytest.raises(ValueError, match="UTF-8 byte limit"):
        checkpoints.latest(task_id)


def test_latest_rejects_durable_node_overflow_with_matching_checksum(tmp_path: Path) -> None:
    store, task_id, checkpoints = _build_service(tmp_path)
    payload_json = '{"items":[' + ",".join("0" for _ in range(9_999)) + "]}"

    _insert_raw_checkpoint(
        store,
        task_id=task_id,
        checkpoint_id="too-wide",
        payload_json=payload_json,
    )

    with pytest.raises(ValueError, match="node limit"):
        checkpoints.latest(task_id)


def test_latest_rejects_oversized_checksum_without_unbounded_read(tmp_path: Path) -> None:
    store, task_id, checkpoints = _build_service(tmp_path)
    saved = checkpoints.save(task_id=task_id, stage="valid", payload={"revision": 1})

    with store.connection() as conn:
        conn.execute(
            "UPDATE checkpoints SET checksum_sha256 = ? WHERE checkpoint_id = ?",
            ("a" * (1024 * 1024), saved.checkpoint_id),
        )

    with pytest.raises(ValueError, match="checksum is invalid"):
        checkpoints.latest(task_id)


def test_save_rejects_non_string_stage_before_persistence(tmp_path: Path) -> None:
    store, task_id, checkpoints = _build_service(tmp_path)

    with pytest.raises(TypeError, match="stage must be str"):
        checkpoints.save(
            task_id=task_id,
            stage=1,  # type: ignore[arg-type]
            payload={"revision": 1},
        )

    with store.connection() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM checkpoints WHERE task_id = ?",
            (task_id,),
        ).fetchone()[0]
    assert count == 0


def test_save_rejects_oversized_stage_before_persistence(tmp_path: Path) -> None:
    store, task_id, checkpoints = _build_service(tmp_path)

    with pytest.raises(ValueError, match="stage exceeds"):
        checkpoints.save(
            task_id=task_id,
            stage="с" * 4097,
            payload={"revision": 1},
        )

    with store.connection() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM checkpoints WHERE task_id = ?",
            (task_id,),
        ).fetchone()[0]
    assert count == 0


def test_unicode_stage_round_trips_with_same_type_and_value(tmp_path: Path) -> None:
    _store, task_id, checkpoints = _build_service(tmp_path)

    saved = checkpoints.save(
        task_id=task_id,
        stage="етап-відновлення",
        payload={"revision": 1},
    )
    loaded = checkpoints.latest(task_id)

    assert type(saved.stage) is str
    assert loaded is not None
    assert loaded.stage == saved.stage


def test_latest_rejects_blob_stage_storage_class(tmp_path: Path) -> None:
    store, task_id, checkpoints = _build_service(tmp_path)
    saved = checkpoints.save(task_id=task_id, stage="valid", payload={"revision": 1})

    with store.connection() as conn:
        conn.execute(
            "UPDATE checkpoints SET stage = ? WHERE checkpoint_id = ?",
            (b"raw-stage", saved.checkpoint_id),
        )

    with pytest.raises(TypeError, match="stage storage must be SQLite TEXT"):
        checkpoints.latest(task_id)


def test_latest_rejects_oversized_checkpoint_identity(tmp_path: Path) -> None:
    store, task_id, checkpoints = _build_service(tmp_path)
    saved = checkpoints.save(task_id=task_id, stage="valid", payload={"revision": 1})

    with store.connection() as conn:
        conn.execute(
            "UPDATE checkpoints SET checkpoint_id = ? WHERE checkpoint_id = ?",
            ("x" * 4097, saved.checkpoint_id),
        )

    with pytest.raises(ValueError, match="checkpoint_id exceeds"):
        checkpoints.latest(task_id)


def test_latest_rejects_blob_task_alias_instead_of_falling_back(
    tmp_path: Path,
) -> None:
    store, task_id, checkpoints = _build_service(tmp_path)
    older = checkpoints.save(task_id=task_id, stage="older", payload={"revision": 1})
    newer = checkpoints.save(task_id=task_id, stage="newer", payload={"revision": 2})

    with store.connection() as conn:
        conn.execute(
            "UPDATE checkpoints SET task_id = ? WHERE checkpoint_id = ?",
            (task_id.encode("utf-8"), newer.checkpoint_id),
        )

    with pytest.raises(TypeError, match="task_id storage must be SQLite TEXT"):
        checkpoints.latest(task_id)

    with store.connection() as conn:
        assert conn.execute(
            "SELECT checkpoint_id FROM checkpoints WHERE checkpoint_id = ?",
            (older.checkpoint_id,),
        ).fetchone() is not None


def test_latest_rejects_single_blob_task_alias_instead_of_not_found(
    tmp_path: Path,
) -> None:
    store, task_id, checkpoints = _build_service(tmp_path)
    saved = checkpoints.save(task_id=task_id, stage="only", payload={"revision": 1})

    with store.connection() as conn:
        conn.execute(
            "UPDATE checkpoints SET task_id = ? WHERE checkpoint_id = ?",
            (task_id.encode("utf-8"), saved.checkpoint_id),
        )

    with pytest.raises(TypeError, match="task_id storage must be SQLite TEXT"):
        checkpoints.latest(task_id)


def test_latest_rejects_invalid_task_id_utf8_before_sql(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _task_id, checkpoints = _build_service(tmp_path)

    def unexpected_connection() -> object:
        pytest.fail("invalid task_id reached SQLite")

    monkeypatch.setattr(store, "connection", unexpected_connection)
    with pytest.raises(ValueError, match="task_id must be valid UTF-8"):
        checkpoints.latest("\ud800")
