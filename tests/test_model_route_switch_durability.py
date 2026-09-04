from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from nika_core.config import AppConfig
from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue, TaskRecord
from nika_core.model_gateway.contracts import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    PrivacyClass,
    ProviderCapabilities,
    ProviderKind,
)
from nika_core.model_gateway.gateway import ModelGateway


class _ExactProvider:
    def __init__(self, provider_id: str, kind: ProviderKind) -> None:
        self._capabilities = ProviderCapabilities(
            provider_id=provider_id,
            kind=kind,
            supports_private_data=True,
        )
        self.requests: list[ModelRequest] = []

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return ModelResponse(
            request_id=request.request_id,
            text=f"{self.capabilities.provider_id} ok",
            provider_id=self.capabilities.provider_id,
            provider_kind=self.capabilities.kind,
            model=request.model or "provider-default",
            usage=ModelUsage(input_tokens=1, output_tokens=1, total_tokens=2),
        )


def _create_route_bound_task(
    queue: TaskQueue,
    *,
    settings: AppConfig,
    model: str,
) -> TaskRecord:
    return queue.create(
        workspace_id="worker48-workspace",
        agent_id="worker48-agent",
        payload={
            "model_route": {
                "provider_id": settings.model_provider,
                "model": model,
            }
        },
    )


def _route(record: TaskRecord) -> tuple[str, str]:
    raw_route = record.payload.get("model_route")
    assert isinstance(raw_route, dict)
    provider_id = raw_route.get("provider_id")
    model = raw_route.get("model")
    assert isinstance(provider_id, str) and provider_id
    assert isinstance(model, str) and model
    return provider_id, model


def _request(record: TaskRecord, *, suffix: str) -> ModelRequest:
    provider_id, model = _route(record)
    return ModelRequest(
        request_id=f"{record.task_id}:{suffix}",
        messages=(ModelMessage(role="user", content="worker48 durable route fixture"),),
        provider_id=provider_id,
        model=model,
        privacy=PrivacyClass.PUBLIC,
        timeout_seconds=2.0,
    )


@pytest.mark.parametrize(
    ("selected_provider", "selected_model", "updated_provider", "updated_model"),
    (
        ("ollama", "qwen3:8b", "approved-api", "model-a"),
        ("approved-api", "model-a", "ollama", "qwen3:8b"),
    ),
)
def test_task_queue_pins_route_across_default_change_and_sqlite_restart(
    tmp_path: Path,
    selected_provider: str,
    selected_model: str,
    updated_provider: str,
    updated_model: str,
) -> None:
    database_path = tmp_path / "worker 48 route durability.db"
    first_store = SQLiteStore(database_path)
    first_store.initialize()
    first_queue = TaskQueue(first_store)

    settings = AppConfig(model_provider=selected_provider)
    running_task = _create_route_bound_task(
        first_queue,
        settings=settings,
        model=selected_model,
    )

    settings.model_provider = updated_provider
    new_task = _create_route_bound_task(
        first_queue,
        settings=settings,
        model=updated_model,
    )

    assert _route(running_task) == (selected_provider, selected_model)
    assert _route(new_task) == (updated_provider, updated_model)
    assert "credential" not in repr(running_task.payload).lower()
    assert "secret" not in repr(running_task.payload).lower()

    reopened_store = SQLiteStore(database_path)
    reopened_store.initialize()
    reopened_queue = TaskQueue(reopened_store)
    restored_running = reopened_queue.get(running_task.task_id)
    restored_new = reopened_queue.get(new_task.task_id)

    assert _route(restored_running) == (selected_provider, selected_model)
    assert _route(restored_new) == (updated_provider, updated_model)

    local = _ExactProvider("ollama", ProviderKind.LOCAL)
    api = _ExactProvider("approved-api", ProviderKind.CLOUD)
    restart_gateway = ModelGateway()
    restart_gateway.register(local)
    restart_gateway.register(api)

    running_response = asyncio.run(
        restart_gateway.complete(_request(restored_running, suffix="restart-old"))
    )
    new_response = asyncio.run(
        restart_gateway.complete(_request(restored_new, suffix="restart-new"))
    )

    assert (running_response.provider_id, running_response.model) == (
        selected_provider,
        selected_model,
    )
    assert (new_response.provider_id, new_response.model) == (
        updated_provider,
        updated_model,
    )
    assert _request(restored_running, suffix="inspect").fallback_provider_ids == ()

    expected_local_calls = int(selected_provider == "ollama") + int(updated_provider == "ollama")
    expected_api_calls = int(selected_provider == "approved-api") + int(
        updated_provider == "approved-api"
    )
    assert len(local.requests) == expected_local_calls
    assert len(api.requests) == expected_api_calls
