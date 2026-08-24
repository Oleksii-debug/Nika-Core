from __future__ import annotations

import asyncio
import threading
import time
import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from nika_core.model_gateway.contracts import (
    ModelErrorCode,
    ModelGatewayError,
    ModelMessage,
    ModelRequest,
    ModelResourcePolicy,
    ModelResponse,
    ModelUsage,
    PrivacyClass,
    ProviderCapabilities,
    ProviderKind,
)
from nika_core.model_gateway.foundry_local import FoundryLocalProvider
from nika_core.model_gateway.gateway import ModelGateway
from nika_core.model_gateway.providers import OllamaProvider, OpenAICompatibleProvider
from nika_core.resources.contracts import ResourceSnapshot


OWNER_BLOCKED = pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "current main violates this DEV17-owned fail-closed oracle; strict XPASS requires "
        "conversion to an ordinary PASS assertion after the production fix integrates"
    ),
)


def _request(**overrides: object) -> ModelRequest:
    values: dict[str, object] = {
        "request_id": "chaos-request",
        "messages": (ModelMessage(role="user", content="private payload"),),
        "provider_id": "primary",
        "privacy": PrivacyClass.PRIVATE,
        "timeout_seconds": 1.0,
    }
    values.update(overrides)
    return ModelRequest(**values)  # type: ignore[arg-type]


class _RecordingProvider:
    def __init__(
        self,
        provider_id: str,
        *,
        kind: ProviderKind = ProviderKind.LOCAL,
        supports_private_data: bool = True,
        supports_hard_cancellation: bool = False,
    ) -> None:
        self._capabilities = ProviderCapabilities(
            provider_id=provider_id,
            kind=kind,
            supports_private_data=supports_private_data,
            supports_hard_cancellation=supports_hard_cancellation,
        )
        self.calls: list[str] = []

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls.append(request.request_id)
        return ModelResponse(
            request_id=request.request_id,
            text="ok",
            provider_id=self.capabilities.provider_id,
            provider_kind=self.capabilities.kind,
            model=request.model or "chaos-model",
        )


class _BlockingFoundryModel:
    def __init__(
        self,
        *,
        completion_gate: threading.Event | None = None,
        completion_delay: float = 0.0,
    ) -> None:
        self.id = "qa-chaos-model:1"
        self.alias = "qa-chaos-model"
        self.is_cached = True
        self.is_loaded = False
        self.context_length = 4096
        self.input_modalities = "text"
        self.output_modalities = "text"
        self.capabilities = "chat"
        self.supports_tool_calling = False
        self.settings = SimpleNamespace(temperature=None)
        self.completion_gate = completion_gate
        self.completion_delay = completion_delay
        self.started = threading.Event()
        self.finished = threading.Event()
        self.unloaded = False
        self.completion_count = 0
        self.active_completions = 0
        self.max_active_completions = 0
        self._counter_lock = threading.Lock()

    def load(self) -> None:
        self.is_loaded = True

    def unload(self) -> None:
        self.unloaded = True
        self.is_loaded = False

    def get_chat_client(self) -> object:
        model = self

        class Client:
            settings = model.settings

            def complete_chat(self, _messages: list[dict[str, str]]) -> object:
                with model._counter_lock:
                    model.completion_count += 1
                    model.active_completions += 1
                    model.max_active_completions = max(
                        model.max_active_completions,
                        model.active_completions,
                    )
                model.started.set()
                try:
                    if model.completion_delay:
                        time.sleep(model.completion_delay)
                    if model.completion_gate is not None:
                        model.completion_gate.wait(timeout=2.0)
                    return SimpleNamespace(
                        choices=[
                            SimpleNamespace(
                                message=SimpleNamespace(content="foundry ok")
                            )
                        ],
                        usage=SimpleNamespace(
                            prompt_tokens=2,
                            completion_tokens=1,
                            total_tokens=3,
                        ),
                    )
                finally:
                    with model._counter_lock:
                        model.active_completions -= 1
                    model.finished.set()

        return Client()


class _FoundryCatalog:
    def __init__(self, model: _BlockingFoundryModel) -> None:
        self._model = model

    def get_model(self, alias: str) -> _BlockingFoundryModel:
        self._model.alias = alias
        return self._model


class _FoundryManager:
    def __init__(self, model: _BlockingFoundryModel) -> None:
        self.catalog = _FoundryCatalog(model)


class _HighCpuObserver:
    def snapshot(self) -> ResourceSnapshot:
        return ResourceSnapshot(
            cpu_percent=95.0,
            memory_percent=40.0,
            available_memory_bytes=8 * 1024**3,
        )


def _foundry_request(request_id: str, *, timeout_seconds: float = 1.0) -> ModelRequest:
    return _request(
        request_id=request_id,
        provider_id="foundry-local",
        timeout_seconds=timeout_seconds,
    )


def _client_factory(handler: Any) -> Any:
    transport = httpx.MockTransport(handler)

    def factory(**kwargs: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport, **kwargs)

    return factory


def test_primary_local_slow_ollama_times_out_without_unsafe_fallback() -> None:
    fallback = _RecordingProvider("fallback")

    async def handler(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.2)
        return httpx.Response(
            200,
            json={
                "model": "qwen3:8b",
                "message": {"content": "late"},
                "done": True,
            },
        )

    gateway = ModelGateway()
    gateway.register(
        OllamaProvider(
            default_model="qwen3:8b",
            client_factory=_client_factory(handler),
        )
    )
    gateway.register(fallback)

    with pytest.raises(ModelGatewayError) as raised:
        asyncio.run(
            gateway.complete(
                _request(
                    provider_id="ollama",
                    fallback_provider_ids=("fallback",),
                    timeout_seconds=0.05,
                )
            )
        )

    assert raised.value.code is ModelErrorCode.TIMEOUT
    assert raised.value.retryable is False
    assert fallback.calls == []


def test_typed_timeout_remains_typed_and_nonretryable() -> None:
    class TypedTimeout(_RecordingProvider):
        async def complete(self, request: ModelRequest) -> ModelResponse:
            self.calls.append(request.request_id)
            raise ModelGatewayError(
                ModelErrorCode.TIMEOUT,
                "typed provider timeout",
                provider_id=self.capabilities.provider_id,
                retryable=False,
            )

    primary = TypedTimeout("primary")
    fallback = _RecordingProvider("fallback")
    gateway = ModelGateway()
    gateway.register(primary)
    gateway.register(fallback)

    with pytest.raises(ModelGatewayError) as raised:
        asyncio.run(
            gateway.complete(_request(fallback_provider_ids=("fallback",)))
        )

    assert raised.value.code is ModelErrorCode.TIMEOUT
    assert raised.value.retryable is False
    assert primary.calls == ["chaos-request"]
    assert fallback.calls == []


@OWNER_BLOCKED
def test_raw_timeout_cannot_masquerade_as_gateway_deadline_or_trigger_fallback() -> None:
    class RawTimeout(_RecordingProvider):
        async def complete(self, request: ModelRequest) -> ModelResponse:
            self.calls.append(request.request_id)
            raise TimeoutError("raw provider timeout with unknown native state")

    primary = RawTimeout("primary", supports_hard_cancellation=True)
    fallback = _RecordingProvider("fallback")
    gateway = ModelGateway()
    gateway.register(primary)
    gateway.register(fallback)

    asyncio.run(gateway.complete(_request(fallback_provider_ids=("fallback",))))

    assert fallback.calls == []


def test_foundry_cancellation_while_queued_never_starts_second_native_request() -> None:
    gate = threading.Event()
    model = _BlockingFoundryModel(completion_gate=gate)
    provider = FoundryLocalProvider(
        default_model="qa-chaos-model",
        manager_factory=lambda: _FoundryManager(model),
    )

    async def scenario() -> None:
        first = asyncio.create_task(provider.complete(_foundry_request("first")))
        assert await asyncio.to_thread(model.started.wait, 1.0) is True

        second = asyncio.create_task(provider.complete(_foundry_request("second")))
        await asyncio.sleep(0)
        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second

        assert model.completion_count == 1
        gate.set()
        assert (await first).request_id == "first"

    asyncio.run(scenario())
    assert model.completion_count == 1


def test_foundry_cancellation_after_native_start_never_runs_fallback() -> None:
    gate = threading.Event()
    model = _BlockingFoundryModel(completion_gate=gate)
    provider = FoundryLocalProvider(
        default_model="qa-chaos-model",
        manager_factory=lambda: _FoundryManager(model),
    )
    fallback = _RecordingProvider("fallback")
    gateway = ModelGateway()
    gateway.register(provider)
    gateway.register(fallback)

    async def scenario() -> None:
        task = asyncio.create_task(
            gateway.complete(
                _request(
                    provider_id="foundry-local",
                    fallback_provider_ids=("fallback",),
                )
            )
        )
        assert await asyncio.to_thread(model.started.wait, 1.0) is True
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert fallback.calls == []
        assert model.completion_count == 1
        gate.set()
        assert await asyncio.to_thread(model.finished.wait, 1.0) is True
        await asyncio.sleep(0)

    asyncio.run(scenario())
    assert fallback.calls == []


def test_ollama_malformed_usage_fails_closed() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "qwen3:8b",
                "message": {"content": "ok"},
                "done": True,
                "prompt_eval_count": "not-an-integer",
                "eval_count": 1,
            },
        )

    gateway = ModelGateway()
    gateway.register(
        OllamaProvider(
            default_model="qwen3:8b",
            client_factory=_client_factory(handler),
        )
    )

    with pytest.raises(ModelGatewayError) as raised:
        asyncio.run(gateway.complete(_request(provider_id="ollama")))

    assert raised.value.code is ModelErrorCode.PROVIDER_ERROR
    assert raised.value.retryable is False


@OWNER_BLOCKED
def test_gateway_rejects_malformed_normalized_usage_from_any_provider() -> None:
    class MalformedUsage(_RecordingProvider):
        async def complete(self, request: ModelRequest) -> ModelResponse:
            self.calls.append(request.request_id)
            return ModelResponse(
                request_id=request.request_id,
                text="looks valid",
                provider_id=self.capabilities.provider_id,
                provider_kind=self.capabilities.kind,
                model="chaos-model",
                usage=ModelUsage(input_tokens=-1, output_tokens=2, total_tokens=1),
            )

    gateway = ModelGateway()
    gateway.register(MalformedUsage("primary"))

    response = asyncio.run(gateway.complete(_request()))

    assert response.usage.input_tokens is None or response.usage.input_tokens >= 0


@OWNER_BLOCKED
def test_ollama_incomplete_final_response_is_rejected() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "qwen3:8b",
                "message": {"content": "partial output"},
                "done": False,
            },
        )

    gateway = ModelGateway()
    gateway.register(
        OllamaProvider(
            default_model="qwen3:8b",
            client_factory=_client_factory(handler),
        )
    )

    response = asyncio.run(gateway.complete(_request(provider_id="ollama")))

    assert response.text != "partial output"


def test_authentication_denial_never_falls_back() -> None:
    fallback = _RecordingProvider("fallback")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "denied"})

    gateway = ModelGateway()
    gateway.register(
        OpenAICompatibleProvider(
            provider_id="primary",
            base_url="https://provider.invalid/v1",
            kind=ProviderKind.CLOUD,
            default_model="controlled-model",
            supports_private_data=True,
            client_factory=_client_factory(handler),
        )
    )
    gateway.register(fallback)

    with pytest.raises(ModelGatewayError) as raised:
        asyncio.run(
            gateway.complete(_request(fallback_provider_ids=("fallback",)))
        )

    assert raised.value.code is ModelErrorCode.AUTHENTICATION
    assert raised.value.retryable is False
    assert fallback.calls == []


def test_foundry_resource_denial_precedes_native_start_and_fallback() -> None:
    model = _BlockingFoundryModel()
    provider = FoundryLocalProvider(
        default_model="qa-chaos-model",
        resource_policy=ModelResourcePolicy(max_cpu_percent=50.0),
        resource_observer=_HighCpuObserver(),
        manager_factory=lambda: _FoundryManager(model),
    )
    fallback = _RecordingProvider("fallback")
    gateway = ModelGateway()
    gateway.register(provider)
    gateway.register(fallback)

    with pytest.raises(ModelGatewayError) as raised:
        asyncio.run(
            gateway.complete(
                _request(
                    provider_id="foundry-local",
                    fallback_provider_ids=("fallback",),
                )
            )
        )

    assert raised.value.code is ModelErrorCode.RESOURCE_LIMIT
    assert raised.value.retryable is False
    assert model.completion_count == 0
    assert fallback.calls == []


@OWNER_BLOCKED
def test_private_route_is_rejected_before_any_candidate_receives_payload() -> None:
    class BusyPrimary(_RecordingProvider):
        async def complete(self, request: ModelRequest) -> ModelResponse:
            self.calls.append(request.request_id)
            raise ModelGatewayError(
                ModelErrorCode.UNAVAILABLE,
                "retryable primary failure",
                provider_id=self.capabilities.provider_id,
                retryable=True,
            )

    primary = BusyPrimary("primary")
    unapproved = _RecordingProvider(
        "unapproved-cloud",
        kind=ProviderKind.CLOUD,
        supports_private_data=False,
    )
    gateway = ModelGateway()
    gateway.register(primary)
    gateway.register(unapproved)

    asyncio.run(
        gateway.complete(_request(fallback_provider_ids=("unapproved-cloud",)))
    )

    assert primary.calls == []
    assert unapproved.calls == []


def test_missing_fallback_fails_preflight_before_primary_execution() -> None:
    primary = _RecordingProvider("primary")
    gateway = ModelGateway()
    gateway.register(primary)

    with pytest.raises(ModelGatewayError) as raised:
        asyncio.run(
            gateway.complete(_request(fallback_provider_ids=("missing",)))
        )

    assert raised.value.code is ModelErrorCode.UNAVAILABLE
    assert raised.value.provider_id == "missing"
    assert primary.calls == []


def test_overall_deadline_is_shared_across_primary_and_fallback() -> None:
    fallback_timeouts: list[float] = []

    class DelayedSafeTimeout(_RecordingProvider):
        async def complete(self, request: ModelRequest) -> ModelResponse:
            self.calls.append(request.request_id)
            await asyncio.sleep(0.08)
            raise ModelGatewayError(
                ModelErrorCode.TIMEOUT,
                "typed provider timeout with hard-cancellation evidence",
                provider_id=self.capabilities.provider_id,
                retryable=True,
            )

    class SlowFallback(_RecordingProvider):
        async def complete(self, request: ModelRequest) -> ModelResponse:
            self.calls.append(request.request_id)
            fallback_timeouts.append(request.timeout_seconds)
            await asyncio.sleep(1.0)
            return ModelResponse(
                request_id=request.request_id,
                text="late fallback",
                provider_id=self.capabilities.provider_id,
                provider_kind=self.capabilities.kind,
                model="chaos-model",
            )

    gateway = ModelGateway()
    gateway.register(DelayedSafeTimeout("primary", supports_hard_cancellation=True))
    gateway.register(SlowFallback("fallback"))

    started = time.perf_counter()
    with pytest.raises(ModelGatewayError) as raised:
        asyncio.run(
            gateway.complete(
                _request(
                    fallback_provider_ids=("fallback",),
                    timeout_seconds=0.2,
                )
            )
        )
    elapsed = time.perf_counter() - started

    assert raised.value.code is ModelErrorCode.TIMEOUT
    assert fallback_timeouts
    assert 0 < fallback_timeouts[0] < 0.2
    assert elapsed < 0.5


@OWNER_BLOCKED
def test_ambiguous_first_provider_effect_blocks_retryable_fallback() -> None:
    effects: list[str] = []
    fallback = _RecordingProvider("fallback")

    class EffectfulPrimary(_RecordingProvider):
        async def complete(self, request: ModelRequest) -> ModelResponse:
            self.calls.append(request.request_id)
            effects.append("native-effect-observed")
            raise ModelGatewayError(
                ModelErrorCode.UNAVAILABLE,
                "provider failed after an externally visible effect",
                provider_id=self.capabilities.provider_id,
                retryable=True,
            )

    gateway = ModelGateway()
    gateway.register(EffectfulPrimary("primary"))
    gateway.register(fallback)

    asyncio.run(gateway.complete(_request(fallback_provider_ids=("fallback",))))

    assert effects == ["native-effect-observed"]
    assert fallback.calls == []


def test_foundry_concurrent_requests_are_serialized_per_provider() -> None:
    model = _BlockingFoundryModel(completion_delay=0.02)
    provider = FoundryLocalProvider(
        default_model="qa-chaos-model",
        manager_factory=lambda: _FoundryManager(model),
    )

    async def scenario() -> None:
        responses = await asyncio.gather(
            *(
                provider.complete(_foundry_request(f"concurrent-{index}"))
                for index in range(8)
            )
        )
        assert [response.request_id for response in responses] == [
            f"concurrent-{index}" for index in range(8)
        ]

    asyncio.run(scenario())
    assert model.completion_count == 8
    assert model.max_active_completions == 1


def test_foundry_provider_restart_has_no_stale_request_state() -> None:
    model = _BlockingFoundryModel()
    manager = _FoundryManager(model)

    first_provider = FoundryLocalProvider(
        default_model="qa-chaos-model",
        manager_factory=lambda: manager,
    )
    first = asyncio.run(first_provider.complete(_foundry_request("before-restart")))
    first_provider.close()

    assert first.request_id == "before-restart"
    assert model.unloaded is True
    assert model.is_loaded is False

    model.unloaded = False
    second_provider = FoundryLocalProvider(
        default_model="qa-chaos-model",
        manager_factory=lambda: manager,
    )
    second = asyncio.run(second_provider.complete(_foundry_request("after-restart")))
    second_provider.close()

    assert second.request_id == "after-restart"
    assert model.completion_count == 2
    assert model.unloaded is True


def test_base_windows_release_keeps_foundry_and_ollama_optional() -> None:
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    base_dependencies = tuple(project["dependencies"])
    optional = project["optional-dependencies"]
    embedded_dependencies = tuple(optional["embedded-ai"])

    assert not any("foundry-local-sdk" in item for item in base_dependencies)
    assert any("foundry-local-sdk" in item for item in embedded_dependencies)
    assert not any("ollama" in item.lower() for item in base_dependencies)

    workflow = (root / ".github/workflows/m11-windows-release.yml").read_text(
        encoding="utf-8"
    )
    assert 'pip install -e ".[gui,qa,dev]"' in workflow
    assert "embedded-ai" not in workflow
