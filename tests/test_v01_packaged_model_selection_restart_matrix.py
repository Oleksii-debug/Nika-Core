from __future__ import annotations

from pathlib import Path

import pytest

from nika_core.builder.repository import AgentDefinitionRepository
from nika_core.data.sqlite import SQLiteStore
from nika_core.intelligence.modes import IntelligenceModePolicy
from nika_core.kernel.task_queue import TaskQueue
from nika_core.v01_model_settings import V01BoundModelRuntimeFactory, V01ModelSettings


def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "profile" / "nika.db")
    store.initialize()
    return store


@pytest.mark.parametrize(
    ("payload", "mode", "runtime_id"),
    (
        (
            {
                "schema_version": 1,
                "route_kind": "deterministic",
                "provider_id": None,
                "model": None,
                "base_url": None,
                "credential_ref": None,
                "private_data_allowed": False,
                "timeout_seconds": 30,
                "revision": 0,
            },
            "no_llm",
            None,
        ),
        (
            {
                "schema_version": 1,
                "route_kind": "foundry_local",
                "provider_id": "foundry-local",
                "model": "embedded-small",
                "base_url": None,
                "credential_ref": None,
                "private_data_allowed": False,
                "timeout_seconds": 30,
                "revision": 0,
            },
            "embedded",
            "model-gateway:foundry-local",
        ),
        (
            {
                "schema_version": 1,
                "route_kind": "ollama",
                "provider_id": "ollama",
                "model": "qwen3:8b",
                "base_url": "http://localhost:11434",
                "credential_ref": None,
                "private_data_allowed": False,
                "timeout_seconds": 30,
                "revision": 0,
            },
            "local_external",
            "model-gateway:ollama",
        ),
        (
            {
                "schema_version": 1,
                "route_kind": "openai_compatible",
                "provider_id": "configured-api",
                "model": "api-v1",
                "base_url": "https://api.example.test/v1",
                "credential_ref": "env:NIKA_TEST_REFERENCE",
                "private_data_allowed": True,
                "timeout_seconds": 30,
                "revision": 0,
            },
            "api_configured",
            "model-gateway:configured-api",
        ),
    ),
)
def test_each_packaged_model_mode_reconstructs_from_fresh_store(
    tmp_path: Path,
    payload: dict[str, object],
    mode: str,
    runtime_id: str | None,
) -> None:
    store = _store(tmp_path)
    settings = V01ModelSettings(store)
    result = settings.configure(payload)
    assert result.status == "completed"

    task = TaskQueue(store).create(
        workspace_id="default",
        agent_id="nika.default",
        payload=settings.prepare_task_payload({"command": f"restart {mode}"}),
    )
    accepted = settings.for_task(task.task_id)

    restarted_store = SQLiteStore(store.path)
    restarted_settings = V01ModelSettings(restarted_store)
    restored = restarted_settings.for_task(task.task_id)
    assert restored == accepted
    assert restored.intelligence_mode == mode

    policy = IntelligenceModePolicy()
    if payload["route_kind"] == "openai_compatible":
        provider_id = payload["provider_id"]
        assert isinstance(provider_id, str)
        policy = IntelligenceModePolicy(
            external_api_enabled=True,
            external_provider_id=provider_id,
        )
    runtime = V01BoundModelRuntimeFactory(
        store=restarted_store,
        definitions=AgentDefinitionRepository(restarted_store),
        settings=restarted_settings,
        intelligence_policy=policy,
    ).for_task(task.task_id)
    if runtime_id is None:
        assert runtime is None
    else:
        assert runtime is not None
        assert runtime.runtime_id == runtime_id
