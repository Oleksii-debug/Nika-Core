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


def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "profile" / "nika.db")
    store.initialize()
    return store


def _bound_task(
    store: SQLiteStore,
    *,
    route_kind: str,
    provider_id: str,
    model: str,
    base_url: str | None,
) -> tuple[V01ModelSettings, str]:
    settings = V01ModelSettings(store)
    result = settings.configure(
        {
            "schema_version": 1,
            "route_kind": route_kind,
            "provider_id": provider_id,
            "model": model,
            "base_url": base_url,
            "credential_ref": None,
            "private_data_allowed": False,
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


def test_disabled_embedded_policy_blocks_bound_foundry_route(tmp_path: Path) -> None:
    store = _store(tmp_path)
    settings, task_id = _bound_task(
        store,
        route_kind="foundry_local",
        provider_id="foundry-local",
        model="embedded-small",
        base_url=None,
    )
    factory = V01BoundModelRuntimeFactory(
        store=store,
        definitions=AgentDefinitionRepository(store),
        settings=settings,
        intelligence_policy=IntelligenceModePolicy(embedded_local_enabled=False),
    )

    with pytest.raises(ModelSetupError, match="заборонено політикою Nika"):
        factory.for_task(task_id)


def test_disabled_external_local_policy_blocks_bound_ollama_route(tmp_path: Path) -> None:
    store = _store(tmp_path)
    settings, task_id = _bound_task(
        store,
        route_kind="ollama",
        provider_id="ollama",
        model="qwen3:8b",
        base_url="http://127.0.0.1:11434",
    )
    factory = V01BoundModelRuntimeFactory(
        store=store,
        definitions=AgentDefinitionRepository(store),
        settings=settings,
        intelligence_policy=IntelligenceModePolicy(external_local_enabled=False),
    )

    with pytest.raises(ModelSetupError, match="заборонено політикою Nika"):
        factory.for_task(task_id)


@pytest.mark.parametrize(
    ("route_kind", "provider_id", "model", "base_url", "policy"),
    (
        (
            "foundry_local",
            "foundry-local",
            "embedded-small",
            None,
            IntelligenceModePolicy(embedded_provider_id="other-embedded"),
        ),
        (
            "ollama",
            "ollama",
            "qwen3:8b",
            "http://localhost:11434",
            IntelligenceModePolicy(external_local_provider_id="other-local"),
        ),
    ),
)
def test_local_policy_provider_identity_must_match_frozen_selection(
    tmp_path: Path,
    route_kind: str,
    provider_id: str,
    model: str,
    base_url: str | None,
    policy: IntelligenceModePolicy,
) -> None:
    store = _store(tmp_path)
    settings, task_id = _bound_task(
        store,
        route_kind=route_kind,
        provider_id=provider_id,
        model=model,
        base_url=base_url,
    )
    factory = V01BoundModelRuntimeFactory(
        store=store,
        definitions=AgentDefinitionRepository(store),
        settings=settings,
        intelligence_policy=policy,
    )

    with pytest.raises(ModelSetupError, match="не дозволений політикою Nika"):
        factory.for_task(task_id)
