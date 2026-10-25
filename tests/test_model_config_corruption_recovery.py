from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.v01_model_settings import ModelSetupError, V01ModelSettings


def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "profile" / "nika.db")
    store.initialize()
    V01ModelSettings(store)
    return store


def _selection_json(**changes: Any) -> str:
    selection: dict[str, object] = {
        "schema_version": 1,
        "route_kind": "openai_compatible",
        "provider_id": "configured-api",
        "model": "fixture-model",
        "base_url": "https://api.example.test/v1",
        "credential_ref": "env:NIKA_CONFIG_CORRUPTION_TEST",
        "private_data_allowed": False,
        "timeout_seconds": 30,
    }
    selection.update(changes)
    return json.dumps(selection, ensure_ascii=False, sort_keys=True)


def _write_raw_current(store: SQLiteStore, selection_json: str) -> None:
    with store.connection() as conn:
        conn.execute(
            "INSERT INTO v01_model_settings(singleton, revision, selection_json) "
            "VALUES (1, 7, ?) ON CONFLICT(singleton) DO UPDATE SET "
            "revision=excluded.revision, selection_json=excluded.selection_json",
            (selection_json,),
        )


def _raw_current(store: SQLiteStore) -> tuple[object, object]:
    with store.connection() as conn:
        row = conn.execute(
            "SELECT revision, selection_json FROM v01_model_settings WHERE singleton = 1"
        ).fetchone()
    assert row is not None
    return row["revision"], row["selection_json"]


def _row_count(store: SQLiteStore, table: str) -> int:
    if table not in {"tasks", "v01_model_selections"}:
        raise AssertionError("test helper table is not allowlisted")
    with store.connection() as conn:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


@pytest.mark.parametrize(
    ("case", "selection_json"),
    (
        (
            "unknown-provider-kind",
            _selection_json(route_kind="future_provider"),
        ),
        ("empty-model", _selection_json(model="")),
        (
            "malformed-endpoint",
            _selection_json(base_url="http://api.example.test/v1"),
        ),
        ("unsupported-selection-schema", _selection_json(schema_version=2)),
        ("wrong-field-type", _selection_json(private_data_allowed="false")),
        (
            "partial-write",
            '{"schema_version":1,"route_kind":"openai_compatible","provider_id":',
        ),
    ),
)
def test_corrupt_durable_model_config_is_invalid_preserved_and_not_task_authority(
    tmp_path: Path,
    case: str,
    selection_json: str,
) -> None:
    store = _store(tmp_path)
    _write_raw_current(store, selection_json)
    before = _raw_current(store)

    restarted_store = SQLiteStore(store.path)
    restarted = V01ModelSettings(restarted_store)

    assert restarted.snapshot() == {"status": "invalid"}, case
    with pytest.raises(ModelSetupError):
        restarted.prepare_task_payload({"command": "must not reach a provider"})

    assert _raw_current(restarted_store) == before, case
    assert _row_count(restarted_store, "v01_model_selections") == 0, case
    assert _row_count(restarted_store, "tasks") == 0, case


def test_fresh_store_is_explicitly_unconfigured_and_cannot_freeze_a_default_route(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    settings = V01ModelSettings(store)

    assert settings.snapshot() == {"status": "missing", "revision": 0}
    with pytest.raises(ModelSetupError, match="Спочатку виберіть постачальника та модель"):
        settings.prepare_task_payload({"command": "must remain unconfigured"})

    assert _row_count(store, "v01_model_selections") == 0
    assert _row_count(store, "tasks") == 0


def test_newer_durable_model_settings_schema_fails_startup_without_rewriting_config(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    settings = V01ModelSettings(store)
    configured = settings.configure(
        {
            "schema_version": 1,
            "route_kind": "openai_compatible",
            "provider_id": "configured-api",
            "model": "recoverable-model",
            "base_url": "https://api.example.test/v1",
            "credential_ref": "env:NIKA_CONFIG_CORRUPTION_TEST",
            "private_data_allowed": False,
            "timeout_seconds": 30,
            "revision": 0,
        }
    )
    assert configured.status == "completed"
    before = _raw_current(store)

    with store.connection() as conn:
        conn.execute(
            "INSERT INTO v01_model_settings_schema(version, applied_at) VALUES (?, ?)",
            (2, "2026-09-10T00:00:00+00:00"),
        )

    with pytest.raises(ModelSetupError, match="новіша за цю програму"):
        V01ModelSettings(SQLiteStore(store.path))

    reopened = SQLiteStore(store.path)
    assert _raw_current(reopened) == before
    with reopened.connection() as conn:
        versions = [
            row["version"]
            for row in conn.execute(
                "SELECT version FROM v01_model_settings_schema ORDER BY version"
            )
        ]
    assert versions == [1, 2]
