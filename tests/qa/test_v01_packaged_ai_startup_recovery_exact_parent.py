from __future__ import annotations

from pathlib import Path

from nika_core.data.sqlite import SQLiteStore
from nika_core.v01_model_settings import V01ModelSettings


def _store(db_path: Path) -> SQLiteStore:
    store = SQLiteStore(db_path)
    store.initialize()
    return store


def _local_selection() -> dict[str, object]:
    return {
        "schema_version": 1,
        "route_kind": "ollama",
        "provider_id": "ollama",
        "model": "qwen3:8b",
        "base_url": "http://localhost:11434",
        "credential_ref": None,
        "private_data_allowed": False,
        "timeout_seconds": 90,
        "revision": 0,
    }


def test_restart_restores_selection_without_restoring_transient_ready(
    tmp_path: Path,
) -> None:
    """Durable config survives restart; process-local provider readiness must not."""

    db_path = tmp_path / "nika.db"
    first_store = _store(db_path)
    first_process = V01ModelSettings(first_store)
    assert first_process.configure(_local_selection()).status == "completed"

    first_snapshot = first_process.snapshot()
    assert first_snapshot["provider_id"] == "ollama"
    assert first_snapshot["model"] == "qwen3:8b"
    assert first_snapshot["base_url"] == "http://localhost:11434"

    # Model a packaged app close/restart by constructing a new store/settings graph
    # over the same durable database. No provider call has occurred in this process,
    # so a READY status here would be stale process-local truth inferred from config.
    restarted_store = SQLiteStore(db_path)
    restarted = V01ModelSettings(restarted_store)
    restored = restarted.snapshot()

    assert restored["provider_id"] == "ollama"
    assert restored["model"] == "qwen3:8b"
    assert restored["base_url"] == "http://localhost:11434"
    assert restored["revision"] == 1
    assert restored["status"] != "ready", (
        "restart restored transient READY from durable model configuration before "
        "the selected provider/model was revalidated"
    )
