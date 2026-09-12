from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.intelligence.modes import (
    IntelligenceMode,
    IntelligenceModePolicy,
    IntelligenceModeRouter,
)
from nika_core.kernel.audit import AuditLog
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState
from nika_core.model_gateway.contracts import (
    ModelErrorCode,
    ModelFailureEffect,
    ModelGatewayError,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    PrivacyClass,
    ProviderCapabilities,
    ProviderKind,
)
from nika_core.model_gateway.gateway import ModelGateway
from nika_core.model_gateway.providers import OllamaProvider
from nika_core.runtime.connectivity_wait import ConnectivityWaitService
from nika_core.runtime.retry import (
    RetryPolicy,
    ScriptRetryCondition,
    ScriptRetryDisposition,
    plan_script_retry,
)
from nika_core.scheduler import ScheduledJob, ScheduledJobStore


@dataclass
class _NetworkState:
    internet_available: bool
    probe_calls: int = 0

    def is_available(self) -> bool:
        self.probe_calls += 1
        return self.internet_available


class _NetworkScopedProvider:
    def __init__(
        self,
        *,
        provider_id: str,
        kind: ProviderKind,
        network: _NetworkState,
        requires_internet: bool,
        dependency_available: bool = True,
    ) -> None:
        self._capabilities = ProviderCapabilities(
            provider_id=provider_id,
            kind=kind,
            supports_private_data=True,
        )
        self._network = network
        self._requires_internet = requires_internet
        self.dependency_available = dependency_available
        self.requests: list[ModelRequest] = []

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        available = self.dependency_available and (
            not self._requires_internet or self._network.internet_available
        )
        if not available:
            raise ModelGatewayError(
                ModelErrorCode.UNAVAILABLE,
                "deterministic fixture unavailable",
                provider_id=self.capabilities.provider_id,
                retryable=True,
                failure_effect=ModelFailureEffect.NO_EFFECT,
            )
        return ModelResponse(
            request_id=request.request_id,
            text="fixture response",
            provider_id=self.capabilities.provider_id,
            provider_kind=self.capabilities.kind,
            model=request.model or "fixture-model",
        )


class _RecordingScheduler:
    def __init__(self) -> None:
        self.jobs: list[ScheduledJob] = []

    def start(self) -> None:
        return None

    def shutdown(self, *, wait: bool = True) -> None:
        del wait

    def upsert(self, job: ScheduledJob) -> None:
        self.jobs.append(job)

    def remove(self, job_id: str) -> bool:
        del job_id
        return False

    def pause(self, job_id: str) -> None:
        del job_id

    def resume(self, job_id: str) -> None:
        del job_id


def _request() -> ModelRequest:
    return ModelRequest(
        request_id="offline-intelligence-request",
        messages=(ModelMessage(role="user", content="offline fixture"),),
        provider_id="untrusted-cloud-default",
        provider_kind=ProviderKind.CLOUD,
        fallback_provider_ids=("offline-local-rescue",),
        privacy=PrivacyClass.PRIVATE,
        timeout_seconds=10.0,
    )


def _running_task(queue: TaskQueue) -> str:
    task = queue.create(
        workspace_id="default",
        agent_id="nika.default",
        payload={"command": "offline cloud retry fixture"},
    )
    queue.transition(task.task_id, TaskState.READY)
    queue.transition(task.task_id, TaskState.RUNNING)
    return task.task_id


def test_mode_boundaries_do_not_turn_internet_loss_into_global_ai_offline() -> None:
    network = _NetworkState(internet_available=False)
    router = IntelligenceModeRouter(
        gateway=ModelGateway(),
        policy=IntelligenceModePolicy(
            external_api_enabled=True,
            external_provider_id="approved-cloud",
        ),
    )

    routes = {mode: router.resolve(mode) for mode in IntelligenceMode}

    assert routes[IntelligenceMode.DETERMINISTIC].provider_kind is ProviderKind.NO_LLM
    assert routes[IntelligenceMode.DETERMINISTIC].uses_model_gateway is False
    assert routes[IntelligenceMode.EMBEDDED_LOCAL].provider_kind is ProviderKind.LOCAL
    assert routes[IntelligenceMode.EXTERNAL_LOCAL].provider_id == "ollama"
    assert routes[IntelligenceMode.EXTERNAL_LOCAL].provider_kind is ProviderKind.LOCAL
    assert routes[IntelligenceMode.EXTERNAL_API].provider_kind is ProviderKind.CLOUD
    assert network.probe_calls == 0


@pytest.mark.parametrize(
    ("mode", "provider_id"),
    (
        (IntelligenceMode.EMBEDDED_LOCAL, "foundry-local"),
        (IntelligenceMode.EXTERNAL_LOCAL, "ollama"),
    ),
)
def test_local_model_modes_remain_usable_without_internet_when_dependency_is_up(
    mode: IntelligenceMode,
    provider_id: str,
) -> None:
    network = _NetworkState(internet_available=False)
    provider = _NetworkScopedProvider(
        provider_id=provider_id,
        kind=ProviderKind.LOCAL,
        network=network,
        requires_internet=False,
    )
    gateway = ModelGateway()
    gateway.register(provider)
    router = IntelligenceModeRouter(gateway=gateway)

    response = asyncio.run(router.complete_model(mode, _request()))

    assert response.text == "fixture response"
    assert response.provider_id == provider_id
    assert response.provider_kind is ProviderKind.LOCAL
    assert len(provider.requests) == 1
    assert provider.requests[0].fallback_provider_ids == ()
    assert network.internet_available is False
    assert network.probe_calls == 0


def test_localhost_ollama_is_local_even_when_internet_is_unavailable() -> None:
    network = _NetworkState(internet_available=False)
    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        assert network.internet_available is False
        assert request.url.host == "localhost"
        return httpx.Response(
            200,
            json={
                "model": "offline-model",
                "message": {"role": "assistant", "content": "local answer"},
                "done": True,
            },
        )

    transport = httpx.MockTransport(handler)

    def client_factory(**kwargs: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport, **kwargs)

    provider = OllamaProvider(
        default_model="offline-model",
        client_factory=client_factory,
    )
    gateway = ModelGateway()
    gateway.register(provider)
    router = IntelligenceModeRouter(gateway=gateway)

    response = asyncio.run(
        router.complete_model(IntelligenceMode.EXTERNAL_LOCAL, _request())
    )

    assert provider.capabilities.kind is ProviderKind.LOCAL
    assert response.provider_kind is ProviderKind.LOCAL
    assert response.text == "local answer"
    assert seen_urls == ["http://localhost:11434/api/chat"]
    assert network.probe_calls == 0


def test_offline_cloud_fails_once_without_fake_success_or_local_substitution() -> None:
    network = _NetworkState(internet_available=False)
    cloud = _NetworkScopedProvider(
        provider_id="approved-cloud",
        kind=ProviderKind.CLOUD,
        network=network,
        requires_internet=True,
    )
    local_rescue = _NetworkScopedProvider(
        provider_id="offline-local-rescue",
        kind=ProviderKind.LOCAL,
        network=network,
        requires_internet=False,
    )
    gateway = ModelGateway()
    gateway.register(cloud)
    gateway.register(local_rescue)
    router = IntelligenceModeRouter(
        gateway=gateway,
        policy=IntelligenceModePolicy(
            external_api_enabled=True,
            external_provider_id="approved-cloud",
        ),
    )

    with pytest.raises(ModelGatewayError) as caught:
        asyncio.run(router.complete_model(IntelligenceMode.EXTERNAL_API, _request()))

    assert caught.value.code is ModelErrorCode.UNAVAILABLE
    assert caught.value.provider_id == "approved-cloud"
    assert caught.value.retryable is True
    assert caught.value.failure_effect is ModelFailureEffect.NO_EFFECT
    assert len(cloud.requests) == 1
    assert cloud.requests[0].fallback_provider_ids == ()
    assert local_rescue.requests == []


def test_offline_cloud_uses_canonical_scheduled_wait_before_retry(tmp_path) -> None:
    network = _NetworkState(internet_available=False)
    cloud = _NetworkScopedProvider(
        provider_id="approved-cloud",
        kind=ProviderKind.CLOUD,
        network=network,
        requires_internet=True,
    )
    local_rescue = _NetworkScopedProvider(
        provider_id="offline-local-rescue",
        kind=ProviderKind.LOCAL,
        network=network,
        requires_internet=False,
    )
    gateway = ModelGateway()
    gateway.register(cloud)
    gateway.register(local_rescue)
    router = IntelligenceModeRouter(
        gateway=gateway,
        policy=IntelligenceModePolicy(
            external_api_enabled=True,
            external_provider_id="approved-cloud",
        ),
    )

    with pytest.raises(ModelGatewayError) as caught:
        asyncio.run(router.complete_model(IntelligenceMode.EXTERNAL_API, _request()))
    failure = caught.value
    assert failure.retryable is True
    assert failure.failure_effect is ModelFailureEffect.NO_EFFECT

    policy = RetryPolicy(
        max_retries=2,
        base_delay_seconds=5,
        max_delay_seconds=20,
    )
    now = datetime(2026, 9, 10, 17, 0, tzinfo=UTC)
    decision = plan_script_retry(
        policy,
        operation_id="cloud-offline-request",
        condition=ScriptRetryCondition.RECOVERABLE_NETWORK_FAILURE,
        retries_used=0,
        now=now,
        replay_safe=(
            failure.retryable
            and failure.failure_effect is ModelFailureEffect.NO_EFFECT
        ),
    )
    assert decision.disposition is ScriptRetryDisposition.SCHEDULED
    assert decision.intent is not None
    assert decision.intent.not_before_utc == now + timedelta(seconds=5)

    store = SQLiteStore(tmp_path / "offline-model-retry.db")
    store.initialize()
    queue = TaskQueue(store)
    jobs = ScheduledJobStore(store)
    scheduler = _RecordingScheduler()
    task_id = _running_task(queue)
    service = ConnectivityWaitService(
        queue=queue,
        jobs=jobs,
        audit=AuditLog(store),
        probe=network,
        scheduler=scheduler,
    )
    service.defer(
        task_id=task_id,
        job_id="cloud-offline-wait",
        action_id="runtime.resume_after_connectivity",
        intent=decision.intent,
    )

    early = service.evaluate(
        job_id="cloud-offline-wait",
        policy=policy,
        now=now + timedelta(seconds=4),
        replay_safe=True,
    )
    assert early.disposition is ScriptRetryDisposition.WAITING
    assert early.continuation_granted is False
    assert network.probe_calls == 0
    assert len(cloud.requests) == 1

    still_offline = service.evaluate(
        job_id="cloud-offline-wait",
        policy=policy,
        now=now + timedelta(seconds=5),
        replay_safe=True,
    )
    assert still_offline.disposition is ScriptRetryDisposition.SCHEDULED
    assert still_offline.continuation_granted is False
    assert still_offline.intent is not None
    assert still_offline.intent.not_before_utc == now + timedelta(seconds=15)
    assert network.probe_calls == 1
    assert len(cloud.requests) == 1
    assert scheduler.jobs[-1].trigger == {
        "run_date": still_offline.intent.not_before_utc.isoformat()
    }

    network.internet_available = True
    connected = service.evaluate(
        job_id="cloud-offline-wait",
        policy=policy,
        now=still_offline.intent.not_before_utc,
        replay_safe=True,
    )
    assert connected.disposition is ScriptRetryDisposition.READY
    assert connected.continuation_granted is True
    assert network.probe_calls == 3

    response = asyncio.run(
        router.complete_model(IntelligenceMode.EXTERNAL_API, _request())
    )
    assert response.provider_id == "approved-cloud"
    assert response.provider_kind is ProviderKind.CLOUD
    assert len(cloud.requests) == 2
    assert local_rescue.requests == []
