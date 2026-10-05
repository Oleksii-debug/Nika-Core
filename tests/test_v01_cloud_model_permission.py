from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState
from nika_core.model_gateway.contracts import (
    ModelMessage,
    ModelRequest,
    PrivacyClass,
    ProviderCapabilities,
    ProviderKind,
)
from nika_core.v01_cloud_model_permission import (
    CloudModelGrantRequest,
    CloudModelPermissionDenied,
    V01CloudModelPermissionService,
)
from nika_core.v01_model_settings import V01ModelSettings


NOW = datetime(2026, 10, 5, 4, 30, tzinfo=UTC)


def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "cloud-permission.sqlite3")
    store.initialize()
    return store


def _settings(store: SQLiteStore, *, route: str = "openai_compatible") -> V01ModelSettings:
    settings = V01ModelSettings(store)
    if route == "openai_compatible":
        payload = {
            "schema_version": 1,
            "route_kind": route,
            "provider_id": "configured-api",
            "model": "api-model",
            "base_url": "https://api.example.test/v1",
            "credential_ref": "env:NIKA_TEST_SECRET_REF",
            "private_data_allowed": True,
            "timeout_seconds": 30,
            "revision": 0,
        }
    else:
        payload = {
            "schema_version": 1,
            "route_kind": "ollama",
            "provider_id": "ollama",
            "model": "qwen3:8b",
            "base_url": "http://localhost:11434",
            "credential_ref": None,
            "private_data_allowed": False,
            "timeout_seconds": 30,
            "revision": 0,
        }
    assert settings.configure(payload).status == "completed"
    return settings


def _task(store: SQLiteStore, settings: V01ModelSettings):
    queue = TaskQueue(store)
    payload = settings.prepare_task_payload({"command": "Run exact task."})
    return queue.create(workspace_id="default", agent_id="nika.default", payload=payload)


def _request() -> ModelRequest:
    return ModelRequest(
        request_id="request-1",
        messages=(ModelMessage(role="user", content="fixture"),),
        model="api-model",
        provider_id="configured-api",
        provider_kind=ProviderKind.CLOUD,
        privacy=PrivacyClass.PUBLIC,
        timeout_seconds=2.0,
    )


def _provider() -> ProviderCapabilities:
    return ProviderCapabilities(
        provider_id="configured-api",
        kind=ProviderKind.CLOUD,
        supports_private_data=True,
        effect_network_host="api.example.test",
    )


def _authorize(service: V01CloudModelPermissionService, task_id: str) -> None:
    authority = service.execution_authority_for_task(task_id)
    assert authority is not None

    async def scenario() -> None:
        with service.cloud_effect_authorizer.execution_scope(authority):
            service.cloud_effect_authorizer.authorize_cloud_effect(
                request=_request(),
                provider=_provider(),
            )

    asyncio.run(scenario())


def test_local_task_never_prompts_or_receives_cloud_authority(tmp_path: Path) -> None:
    store = _store(tmp_path)
    settings = _settings(store, route="ollama")
    record = _task(store, settings)
    prompts: list[CloudModelGrantRequest] = []
    service = V01CloudModelPermissionService(
        store=store,
        settings=settings,
        confirm=lambda request: prompts.append(request) or True,
        clock=lambda: NOW,
    )

    service.admit_created_task(record)

    queue = TaskQueue(store)
    queue.transition(record.task_id, TaskState.READY)
    queue.transition(record.task_id, TaskState.RUNNING)
    assert prompts == []
    assert service.execution_authority_for_task(record.task_id) is None


def test_cloud_denial_creates_no_spendable_authority(tmp_path: Path) -> None:
    store = _store(tmp_path)
    settings = _settings(store)
    record = _task(store, settings)
    service = V01CloudModelPermissionService(
        store=store,
        settings=settings,
        confirm=lambda _request: False,
        clock=lambda: NOW,
    )

    with pytest.raises(CloudModelPermissionDenied, match="не дозволено"):
        service.admit_created_task(record)

    with store.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM standing_permissions").fetchone()[0] == 0


@pytest.mark.parametrize("decision", [1, "yes", object()])
def test_non_boolean_host_confirmation_fails_closed(
    tmp_path: Path,
    decision: object,
) -> None:
    store = _store(tmp_path)
    settings = _settings(store)
    record = _task(store, settings)
    service = V01CloudModelPermissionService(
        store=store,
        settings=settings,
        confirm=lambda _request: decision,  # type: ignore[return-value]
        clock=lambda: NOW,
    )

    with pytest.raises(CloudModelPermissionDenied, match="некоректний результат"):
        service.admit_created_task(record)

    with store.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM standing_permissions").fetchone()[0] == 0


def test_exact_secret_free_grant_admits_only_running_task_and_revocation(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    settings = _settings(store)
    record = _task(store, settings)
    prompts: list[CloudModelGrantRequest] = []
    service = V01CloudModelPermissionService(
        store=store,
        settings=settings,
        confirm=lambda request: prompts.append(request) or True,
        clock=lambda: NOW,
    )

    service.admit_created_task(record)

    assert prompts == [
        CloudModelGrantRequest(
            task_id=record.task_id,
            provider_id="configured-api",
            model="api-model",
            network_host="api.example.test",
            private_data_allowed=True,
        )
    ]
    assert not hasattr(prompts[0], "credential_ref")
    assert "NIKA_TEST_SECRET_REF" not in repr(prompts[0])
    assert service.execution_authority_for_task(record.task_id) is None

    queue = TaskQueue(store)
    queue.transition(record.task_id, TaskState.READY)
    assert service.execution_authority_for_task(record.task_id) is None
    queue.transition(record.task_id, TaskState.RUNNING)

    _authorize(service, record.task_id)

    service.revoke_task(record.task_id)
    with pytest.raises(PermissionError, match="revoked"):
        _authorize(service, record.task_id)


def test_durable_grant_reconstructs_after_restart_without_reprompt(tmp_path: Path) -> None:
    store = _store(tmp_path)
    settings = _settings(store)
    record = _task(store, settings)
    first = V01CloudModelPermissionService(
        store=store,
        settings=settings,
        confirm=lambda _request: True,
        clock=lambda: NOW,
    )
    first.admit_created_task(record)

    queue = TaskQueue(store)
    queue.transition(record.task_id, TaskState.READY)
    queue.transition(record.task_id, TaskState.RUNNING)

    restarted_store = SQLiteStore(store.path)
    restarted_settings = V01ModelSettings(restarted_store)
    restarted = V01CloudModelPermissionService(
        store=restarted_store,
        settings=restarted_settings,
        confirm=lambda _request: (_ for _ in ()).throw(
            AssertionError("durable authority reconstruction must not prompt again")
        ),
        clock=lambda: NOW,
    )

    _authorize(restarted, record.task_id)


def test_paused_task_cannot_spend_existing_grant(tmp_path: Path) -> None:
    store = _store(tmp_path)
    settings = _settings(store)
    record = _task(store, settings)
    service = V01CloudModelPermissionService(
        store=store,
        settings=settings,
        confirm=lambda _request: True,
        clock=lambda: NOW,
    )
    service.admit_created_task(record)

    queue = TaskQueue(store)
    queue.transition(record.task_id, TaskState.READY)
    queue.transition(record.task_id, TaskState.RUNNING)
    assert service.execution_authority_for_task(record.task_id) is not None

    queue.transition(record.task_id, TaskState.PAUSED)
    assert service.execution_authority_for_task(record.task_id) is None
