from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from nika_core.config import AppConfig
from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.v01_model_settings import ModelSetupError, V01ModelSettings
from nika_core.v01_source_settings import V01SourceSettings


def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "Дані програми" / "ніка.db")
    store.initialize()
    return store


def _local(*, revision: int = 0, model: str = "qwen3:8b") -> dict[str, object]:
    return {
        "schema_version": 1,
        "route_kind": "ollama",
        "provider_id": "ollama",
        "model": model,
        "base_url": "http://localhost:11434",
        "credential_ref": None,
        "private_data_allowed": False,
        "timeout_seconds": 90,
        "revision": revision,
    }


def _api(*, revision: int = 0, model: str = "fixture-model") -> dict[str, object]:
    return {
        "schema_version": 1,
        "route_kind": "openai_compatible",
        "provider_id": "configured-api",
        "model": model,
        "base_url": "https://api.example.test/v1",
        "credential_ref": "env:NIKA_TEST_REFERENCE",
        "private_data_allowed": True,
        "timeout_seconds": 45,
        "revision": revision,
    }


def test_local_model_selection_is_durable_and_snapshot_is_ui_safe(tmp_path: Path) -> None:
    store = _store(tmp_path)
    settings = V01ModelSettings(store)

    result = settings.configure(_local())

    assert result.status == "completed"
    assert settings.snapshot() == {
        "status": "ready",
        "revision": 1,
        "intelligence_mode": "local_external",
        "route_kind": "ollama",
        "provider_id": "ollama",
        "provider_kind": "local",
        "model": "qwen3:8b",
        "base_url": "http://localhost:11434",
        "timeout_seconds": 90.0,
        "private_data_allowed": True,
        "credential_configured": False,
    }
    restarted = V01ModelSettings(SQLiteStore(store.path))
    assert restarted.snapshot() == settings.snapshot()


def test_api_selection_omits_credential_reference_from_snapshot_and_audit(tmp_path: Path) -> None:
    store = _store(tmp_path)
    settings = V01ModelSettings(store)
    assert settings.configure(_api()).status == "completed"

    snapshot = settings.snapshot()
    rendered = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
    assert snapshot["credential_configured"] is True
    assert "credential_ref" not in snapshot
    assert "NIKA_TEST_REFERENCE" not in rendered

    with store.connection() as conn:
        audit = [
            str(row["payload_json"])
            for row in conn.execute("SELECT payload_json FROM audit_events ORDER BY event_id")
        ]
    assert audit
    assert "NIKA_TEST_REFERENCE" not in repr(audit)
    assert "api.example.test" not in repr(audit)


@pytest.mark.parametrize(
    "change",
    (
        {"credential_ref": "unsupported:reference"},
        {"credential_ref": "env:INVALID-NAME"},
        {"base_url": "https://api.example.test/v1?mode=test"},
        {"provider_id": "ollama"},
        {"model": " model "},
        {"timeout_seconds": 0},
        {"timeout_seconds": True},
        {"private_data_allowed": "yes"},
    ),
)
def test_invalid_api_selection_does_not_replace_valid_route(
    tmp_path: Path, change: dict[str, object]
) -> None:
    store = _store(tmp_path)
    settings = V01ModelSettings(store)
    assert settings.configure(_api()).status == "completed"
    before = settings.snapshot()

    result = settings.configure({**_api(revision=1), **change})

    assert result.status == "rejected"
    assert settings.snapshot() == before


@pytest.mark.parametrize(
    "change",
    (
        {"provider_id": "local-other"},
        {"credential_ref": "env:NIKA_TEST_REFERENCE"},
        {"base_url": "http://localhost:11434/api"},
        {"base_url": "http://localhost:11434?mode=test"},
        {"base_url": "ftp://localhost"},
        {"base_url": "http://ollama.example.test:11434"},
        {"base_url": "https://192.0.2.10:11434"},
    ),
)
def test_invalid_ollama_selection_is_rejected(tmp_path: Path, change: dict[str, object]) -> None:
    settings = V01ModelSettings(_store(tmp_path))
    assert settings.configure({**_local(), **change}).status == "rejected"
    assert settings.snapshot()["status"] == "missing"


def test_concurrent_settings_writers_use_revision_authority(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = V01ModelSettings(store)
    second = V01ModelSettings(store)
    barrier = Barrier(2)

    def save(settings: V01ModelSettings, payload: dict[str, object]) -> str:
        barrier.wait(timeout=5)
        return settings.configure(payload).status

    with ThreadPoolExecutor(max_workers=2) as executor:
        left = executor.submit(save, first, _local())
        right = executor.submit(save, second, _api())
        statuses = sorted((left.result(timeout=10), right.result(timeout=10)))

    assert statuses == ["completed", "rejected"]
    assert first.snapshot()["revision"] == 1


def test_task_payload_captures_selection_and_restart_never_rebinds_to_new_default(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    settings = V01ModelSettings(store)
    assert settings.configure(_api(model="api-original")).status == "completed"

    prepared = settings.prepare_task_payload({"command": "Run the real team."})
    task = TaskQueue(store).create(
        workspace_id="default",
        agent_id="nika.default",
        payload=prepared,
    )
    first = settings.for_task(task.task_id)
    assert first.provider_id == "configured-api"
    assert first.model == "api-original"

    assert settings.configure(_local(revision=1, model="qwen3:8b")).status == "completed"
    restarted = V01ModelSettings(SQLiteStore(store.path))
    restored = restarted.for_task(task.task_id)

    assert restored == first
    assert restarted.snapshot()["provider_id"] == "ollama"
    task_payload = TaskQueue(SQLiteStore(store.path)).get(task.task_id).payload
    assert set(task_payload) == {"command", "v01_model_selection"}
    reference = task_payload["v01_model_selection"]
    assert isinstance(reference, str) and len(reference) == 64
    rendered = json.dumps(task_payload, ensure_ascii=False, sort_keys=True)
    assert "configured-api" not in rendered
    assert "api-original" not in rendered
    assert "NIKA_TEST_REFERENCE" not in rendered
    assert "api.example.test" not in rendered


def test_prepare_task_payload_rejects_caller_supplied_model_binding(tmp_path: Path) -> None:
    settings = V01ModelSettings(_store(tmp_path))
    assert settings.configure(_local()).status == "completed"

    with pytest.raises(ModelSetupError, match="лише Nika"):
        settings.prepare_task_payload(
            {"command": "Run", "v01_model_selection": "0" * 64}
        )


def test_task_without_accepted_model_reference_fails_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    settings = V01ModelSettings(store)
    assert settings.configure(_local()).status == "completed"
    task = TaskQueue(store).create(
        workspace_id="default",
        agent_id="nika.default",
        payload={"command": "Legacy task without a route."},
    )

    with pytest.raises(ModelSetupError, match="посилання"):
        settings.for_task(task.task_id)


def test_corrupt_selection_content_fails_closed_without_rebinding(tmp_path: Path) -> None:
    store = _store(tmp_path)
    settings = V01ModelSettings(store)
    assert settings.configure(_api()).status == "completed"
    task = TaskQueue(store).create(
        workspace_id="default",
        agent_id="nika.default",
        payload=settings.prepare_task_payload({"command": "Run"}),
    )
    reference = str(task.payload["v01_model_selection"])
    with store.connection() as conn:
        conn.execute(
            "UPDATE v01_model_selections SET selection_json = ? WHERE selection_id = ?",
            ('{"schema_version":1}', reference),
        )

    with pytest.raises(ModelSetupError, match="перевірити"):
        settings.for_task(task.task_id)
    with store.connection() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM v01_task_model_bindings WHERE task_id = ?",
                (task.task_id,),
            ).fetchone()[0]
            == 0
        )


def test_existing_binding_rejects_later_task_reference_substitution(tmp_path: Path) -> None:
    store = _store(tmp_path)
    settings = V01ModelSettings(store)
    assert settings.configure(_api(model="first")).status == "completed"
    task = TaskQueue(store).create(
        workspace_id="default",
        agent_id="nika.default",
        payload=settings.prepare_task_payload({"command": "Run"}),
    )
    assert settings.for_task(task.task_id).model == "first"

    assert settings.configure(_local(revision=1)).status == "completed"
    replacement = settings.prepare_task_payload({"command": "Other"})
    with store.connection() as conn:
        conn.execute(
            "UPDATE tasks SET payload_json = ? WHERE task_id = ?",
            (json.dumps(replacement, sort_keys=True), task.task_id),
        )

    with pytest.raises(ModelSetupError, match="не збігається"):
        settings.for_task(task.task_id)


def test_model_capture_composes_after_existing_source_capture(tmp_path: Path) -> None:
    store = _store(tmp_path)
    root = tmp_path / "Джерела команди"
    root.mkdir()
    source_a = root / "перше.txt"
    source_b = root / "друге.txt"
    source_a.write_text("alpha", encoding="utf-8")
    source_b.write_text("beta", encoding="utf-8")

    sources = V01SourceSettings(store, AppConfig())
    assert (
        sources.configure(
            {
                "schema_version": 1,
                "root": str(root),
                "source_a": str(source_a),
                "source_b": str(source_b),
                "revision": 0,
            }
        ).status
        == "completed"
    )
    models = V01ModelSettings(store)
    assert models.configure(_local()).status == "completed"

    payload = sources.prepare_task_payload({"command": "Run composed task."})
    payload = models.prepare_task_payload(payload)
    task = TaskQueue(store).create(
        workspace_id="default",
        agent_id="nika.default",
        payload=payload,
    )

    assert set(task.payload) == {
        "command",
        "v01_source_selection",
        "v01_model_selection",
    }
    bound_sources = sources.for_task(task.task_id)
    bound_model = models.for_task(task.task_id)
    assert bound_sources.source_a == str(source_a.resolve())
    assert bound_sources.source_b == str(source_b.resolve())
    assert bound_model.provider_id == "ollama"
    assert bound_model.model == "qwen3:8b"
