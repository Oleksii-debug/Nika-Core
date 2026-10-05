from __future__ import annotations

from pathlib import Path

from nika_core.config import AppConfig
from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.v01_cloud_model_permission import CloudModelGrantRequest
from nika_core.v01_model_settings import V01ModelSettings
from nika_core.v01_source_settings import V01SourceSettings
from scripts import nika_windows


def _configured_product(tmp_path: Path) -> AppConfig:
    database = (tmp_path / "Дані Nika" / "ніка.db").resolve()
    config = AppConfig(database_path=database)
    store = SQLiteStore(database)
    store.initialize()

    root = tmp_path / "Джерела команди"
    root.mkdir()
    source_a = root / "перше.txt"
    source_b = root / "друге.txt"
    source_a.write_text("alpha evidence", encoding="utf-8")
    source_b.write_text("beta evidence", encoding="utf-8")

    sources = V01SourceSettings(store, config)
    assert sources.configure(
        {
            "schema_version": 1,
            "root": str(root.resolve()),
            "source_a": str(source_a.resolve()),
            "source_b": str(source_b.resolve()),
            "revision": 0,
        }
    ).status == "completed"

    models = V01ModelSettings(store)
    assert models.configure(
        {
            "schema_version": 1,
            "route_kind": "openai_compatible",
            "provider_id": "configured-api",
            "model": "api-model",
            "base_url": "https://api.example.test/v1",
            "credential_ref": "env:PRIVATE_CREDENTIAL_REFERENCE_CANARY",
            "private_data_allowed": True,
            "timeout_seconds": 30,
            "revision": 0,
        }
    ).status == "completed"
    return config


def _assert_no_cloud_admission(
    config: AppConfig,
    prompts: list[CloudModelGrantRequest],
) -> None:
    store = SQLiteStore(config.database_path)
    assert TaskQueue(store).list_recent(limit=10) == []
    assert prompts == []
    with store.connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM standing_permissions"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM v01_cloud_model_permission_bindings"
        ).fetchone()[0] == 0


def test_corrupt_persisted_model_rejects_before_cloud_prompt_or_grant(
    tmp_path: Path,
) -> None:
    config = _configured_product(tmp_path)
    store = SQLiteStore(config.database_path)
    corrupt = (
        '{"schema_version":1,"route_kind":"openai_compatible",'
        '"route_kind":"deterministic"}'
    )
    with store.connection() as conn:
        conn.execute(
            "UPDATE v01_model_settings SET selection_json = ? WHERE singleton = 1",
            (corrupt,),
        )

    prompts: list[CloudModelGrantRequest] = []
    bridge, _products = nika_windows.build_windows_bridge(
        config,
        cloud_permission_confirm=lambda request: prompts.append(request) or True,
    )

    result = bridge.dispatch(
        {
            "request_id": "corrupt-model",
            "action_id": "task.create",
            "payload": {"command": "Порівняй ці два джерела."},
        }
    )

    assert result["status"] == "rejected"
    assert result["focus_id"] == "model-route-kind"
    assert "пошкоджені" in result["message"]
    _assert_no_cloud_admission(config, prompts)


def test_corrupt_persisted_sources_reject_before_cloud_prompt_or_grant(
    tmp_path: Path,
) -> None:
    config = _configured_product(tmp_path)
    store = SQLiteStore(config.database_path)
    corrupt = (
        '{"schema_version":1,"root":"C:/one","root":"C:/two",'
        '"source_a":"C:/one/a.txt","source_b":"C:/one/b.txt"}'
    )
    with store.connection() as conn:
        conn.execute(
            "UPDATE v01_source_settings SET selection_json = ? WHERE singleton = 1",
            (corrupt,),
        )

    prompts: list[CloudModelGrantRequest] = []
    bridge, _products = nika_windows.build_windows_bridge(
        config,
        cloud_permission_confirm=lambda request: prompts.append(request) or True,
    )

    result = bridge.dispatch(
        {
            "request_id": "corrupt-sources",
            "action_id": "task.create",
            "payload": {"command": "Порівняй ці два джерела."},
        }
    )

    assert result["status"] == "rejected"
    assert "пошкоджені" in result["message"]
    _assert_no_cloud_admission(config, prompts)
