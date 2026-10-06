from __future__ import annotations

from pathlib import Path

import pytest

from nika_core.builder.repository import AgentDefinitionRepository
from nika_core.data.sqlite import SQLiteStore
from nika_core.intelligence.modes import IntelligenceModePolicy
from nika_core.kernel.task_queue import TaskQueue
from nika_core.v01_model_settings import (
    ModelSetupError,
    V01BoundModelRuntimeFactory,
    V01ModelSettings,
)


class _RecordingResolver:
    def __init__(self) -> None:
        self.references: list[str] = []

    def resolve(self, credential_ref: str) -> str:
        self.references.append(credential_ref)
        raise AssertionError("policy denial must happen before credential resolution")


def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "profile" / "nika.db")
    store.initialize()
    return store


def _bound_cloud_task(store: SQLiteStore) -> tuple[V01ModelSettings, str]:
    settings = V01ModelSettings(store)
    result = settings.configure(
        {
            "schema_version": 1,
            "route_kind": "openai_compatible",
            "provider_id": "configured-api",
            "model": "api-model",
            "base_url": "https://api.example.test/v1",
            "credential_ref": "env:NIKA_CLOUD_POLICY_TEST",
            "private_data_allowed": True,
            "timeout_seconds": 30,
            "revision": 0,
        }
    )
    assert result.status == "completed"
    payload = settings.prepare_task_payload({"command": "Use the selected model."})
    task_id = TaskQueue(store).create(
        workspace_id="default",
        agent_id="nika.default",
        payload=payload,
    ).task_id
    return settings, task_id


def test_default_external_api_policy_denies_before_credential_boundary(tmp_path: Path) -> None:
    store = _store(tmp_path)
    settings, task_id = _bound_cloud_task(store)
    resolver = _RecordingResolver()
    factory = V01BoundModelRuntimeFactory(
        store=store,
        definitions=AgentDefinitionRepository(store),
        settings=settings,
        credential_resolver=resolver,
    )

    with pytest.raises(ModelSetupError, match="заборонено політикою Nika"):
        factory.for_task(task_id)

    assert resolver.references == []


def test_enabled_external_api_policy_rejects_wrong_provider_before_credentials(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    settings, task_id = _bound_cloud_task(store)
    resolver = _RecordingResolver()
    factory = V01BoundModelRuntimeFactory(
        store=store,
        definitions=AgentDefinitionRepository(store),
        settings=settings,
        credential_resolver=resolver,
        intelligence_policy=IntelligenceModePolicy(
            external_api_enabled=True,
            external_provider_id="other-api",
        ),
    )

    with pytest.raises(ModelSetupError, match="не дозволений політикою Nika"):
        factory.for_task(task_id)

    assert resolver.references == []


def test_enabled_external_api_policy_allows_only_exact_provider_without_resolving_early(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    settings, task_id = _bound_cloud_task(store)
    resolver = _RecordingResolver()
    factory = V01BoundModelRuntimeFactory(
        store=store,
        definitions=AgentDefinitionRepository(store),
        settings=settings,
        credential_resolver=resolver,
        intelligence_policy=IntelligenceModePolicy(
            external_api_enabled=True,
            external_provider_id="configured-api",
        ),
    )

    runtime = factory.for_task(task_id)

    assert runtime is not None
    assert resolver.references == []
