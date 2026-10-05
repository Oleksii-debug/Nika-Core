from __future__ import annotations

import asyncio

from nika_core.model_gateway.contracts import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    PrivacyClass,
    ProviderCapabilities,
    ProviderKind,
)
from nika_core.model_gateway.gateway import ModelGateway


class _RecordingCloudAuthorizer:
    def __init__(self) -> None:
        self.task: asyncio.Task[object] | None = None

    def authorize_cloud_effect(
        self,
        *,
        request: ModelRequest,
        provider: ProviderCapabilities,
    ) -> None:
        assert request.provider_id == "cloud-provider"
        assert provider.provider_id == "cloud-provider"
        self.task = asyncio.current_task()
        assert self.task is not None


class _RecordingCloudProvider:
    def __init__(self) -> None:
        self.task: asyncio.Task[object] | None = None

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider_id="cloud-provider",
            kind=ProviderKind.CLOUD,
            supports_private_data=False,
            effect_network_host="api.example.test",
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.task = asyncio.current_task()
        assert self.task is not None
        return ModelResponse(
            request_id=request.request_id,
            text="same-task response",
            provider_id="cloud-provider",
            provider_kind=ProviderKind.CLOUD,
            model=request.model or "model-a",
        )


def _request() -> ModelRequest:
    return ModelRequest(
        request_id="cloud-task-linearization",
        messages=(ModelMessage(role="user", content="fixture"),),
        model="model-a",
        provider_id="cloud-provider",
        provider_kind=ProviderKind.CLOUD,
        privacy=PrivacyClass.PUBLIC,
        timeout_seconds=2.0,
    )


def test_cloud_authorization_and_provider_admission_share_caller_task() -> None:
    authorizer = _RecordingCloudAuthorizer()
    provider = _RecordingCloudProvider()
    gateway = ModelGateway(cloud_effect_authorizer=authorizer)
    gateway.register(provider)

    async def run() -> None:
        caller_task = asyncio.current_task()
        assert caller_task is not None

        response = await gateway.complete(_request())

        assert response.text == "same-task response"
        assert authorizer.task is caller_task
        assert provider.task is caller_task

    asyncio.run(run())
