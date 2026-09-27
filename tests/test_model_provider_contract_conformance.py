from __future__ import annotations

import asyncio
import copy
import json
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
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

_CANARY = "NIKA-CONFORMANCE-SECRET-DO-NOT-LEAK"
_MODEL = "conformance-model"
_MESSAGES = (
    ModelMessage(role="system", content="Follow the contract."),
    ModelMessage(role="user", content="hello"),
)


@dataclass(slots=True)
class _Observation:
    messages: list[dict[str, str]] | None = None
    model: str | None = None
    temperature: float | None = None
    timeout_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class _BlockingProbe:
    provider: Any
    wait_started: Callable[[], Awaitable[None]]
    release: Callable[[], None]
    wait_finished: Callable[[], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class _AdapterCase:
    name: str
    provider_id: str
    kind: ProviderKind
    success_provider: Callable[[_Observation], Any]
    invalid_provider: Callable[[], Any]
    timeout_provider: Callable[[], Any]
    secret_failure_provider: Callable[[str], Any]
    blocking_probe: Callable[[], _BlockingProbe]
    assert_request: Callable[[_Observation], None]
    assert_configuration_failure: Callable[[], None]


class _FallbackSpy:
    def __init__(self) -> None:
        self.calls = 0
        self._capabilities = ProviderCapabilities(
            provider_id="undeclared-fallback",
            kind=ProviderKind.LOCAL,
            supports_private_data=True,
        )

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        return ModelResponse(
            request_id=request.request_id,
            text="unexpected fallback",
            provider_id=self.capabilities.provider_id,
            provider_kind=self.capabilities.kind,
            model=request.model or _MODEL,
        )


def _request(provider_id: str, **overrides: object) -> ModelRequest:
    values: dict[str, object] = {
        "request_id": f"conformance-{provider_id}",
        "messages": _MESSAGES,
        "model": _MODEL,
        "provider_id": provider_id,
        "privacy": PrivacyClass.PRIVATE,
        "timeout_seconds": 2.0,
        "temperature": 0.25,
        "metadata": {"trace": "contract-only"},
    }
    values.update(overrides)
    return ModelRequest(**values)  # type: ignore[arg-type]


def _http_client_factory(
    handler: Callable[[httpx.Request], httpx.Response],
    observation: _Observation | None = None,
) -> Callable[..., httpx.AsyncClient]:
    transport = httpx.MockTransport(handler)

    def factory(**kwargs: Any) -> httpx.AsyncClient:
        if observation is not None:
            observation.timeout_seconds = kwargs.get("timeout")
        return httpx.AsyncClient(transport=transport, **kwargs)

    return factory


def _openai_success_provider(observation: _Observation) -> OpenAICompatibleProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read())
        observation.messages = body["messages"]
        observation.model = body["model"]
        observation.temperature = body.get("temperature")
        return httpx.Response(
            200,
            json={
                "model": _MODEL,
                "choices": [{"message": {"content": "answer"}}],
                "usage": {
                    "prompt_tokens": 5,
                    "completion_tokens": 3,
                    "total_tokens": 8,
                },
            },
        )

    return OpenAICompatibleProvider(
        provider_id="openai-compatible",
        base_url="https://provider.invalid/v1",
        kind=ProviderKind.CLOUD,
        default_model="default-model",
        supports_private_data=True,
        client_factory=_http_client_factory(handler, observation),
    )


def _ollama_success_provider(observation: _Observation) -> OllamaProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read())
        observation.messages = body["messages"]
        observation.model = body["model"]
        observation.temperature = body["options"]["temperature"]
        return httpx.Response(
            200,
            json={
                "model": _MODEL,
                "message": {"role": "assistant", "content": "answer"},
                "done": True,
                "prompt_eval_count": 5,
                "eval_count": 3,
            },
        )

    return OllamaProvider(
        default_model="default-model",
        client_factory=_http_client_factory(handler, observation),
    )


class _FoundryModel:
    def __init__(
        self,
        observation: _Observation | None = None,
        *,
        mode: str = "success",
        canary: str = _CANARY,
        started: threading.Event | None = None,
        release: threading.Event | None = None,
        finished: threading.Event | None = None,
    ) -> None:
        self.id = "conformance-model:1"
        self.alias = _MODEL
        self.is_cached = True
        self.is_loaded = True
        self.settings = SimpleNamespace(temperature=None)
        self._observation = observation
        self._mode = mode
        self._canary = canary
        self._started = started
        self._release = release
        self._finished = finished

    def get_chat_client(self) -> object:
        model = self

        class Client:
            settings = model.settings

            def complete_chat(self, messages: list[dict[str, str]]) -> object:
                if model._observation is not None:
                    model._observation.messages = messages
                    model._observation.temperature = model.settings.temperature
                if model._started is not None:
                    model._started.set()
                try:
                    if model._release is not None and not model._release.wait(timeout=1.0):
                        raise RuntimeError("conformance release barrier timed out")
                    if model._mode == "invalid":
                        return SimpleNamespace(
                            choices=[SimpleNamespace(message=SimpleNamespace(content=None))],
                            usage=None,
                        )
                    if model._mode == "timeout":
                        raise TimeoutError(model._canary)
                    if model._mode == "secret":
                        raise RuntimeError(model._canary)
                    return SimpleNamespace(
                        choices=[SimpleNamespace(message=SimpleNamespace(content="answer"))],
                        usage=SimpleNamespace(
                            prompt_tokens=5,
                            completion_tokens=3,
                            total_tokens=8,
                        ),
                    )
                finally:
                    if model._finished is not None:
                        model._finished.set()

        return Client()


class _FoundryCatalog:
    def __init__(self, model: _FoundryModel, observation: _Observation | None = None) -> None:
        self._model = model
        self._observation = observation

    def get_model(self, alias: str) -> _FoundryModel:
        if self._observation is not None:
            self._observation.model = alias
        self._model.alias = alias
        return self._model


class _FoundryManager:
    def __init__(self, model: _FoundryModel, observation: _Observation | None = None) -> None:
        self.catalog = _FoundryCatalog(model, observation)


def _foundry_provider(
    *,
    observation: _Observation | None = None,
    mode: str = "success",
    canary: str = _CANARY,
    started: threading.Event | None = None,
    release: threading.Event | None = None,
    finished: threading.Event | None = None,
) -> FoundryLocalProvider:
    model = _FoundryModel(
        observation,
        mode=mode,
        canary=canary,
        started=started,
        release=release,
        finished=finished,
    )
    manager = _FoundryManager(model, observation)
    return FoundryLocalProvider(
        default_model="default-model",
        manager_factory=lambda: manager,
    )


def _openai_invalid_provider() -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        provider_id="openai-compatible",
        base_url="https://provider.invalid/v1",
        kind=ProviderKind.CLOUD,
        default_model="default-model",
        supports_private_data=True,
        client_factory=_http_client_factory(
            lambda _request: httpx.Response(200, json={"choices": []})
        ),
    )


def _ollama_invalid_provider() -> OllamaProvider:
    return OllamaProvider(
        default_model="default-model",
        client_factory=_http_client_factory(
            lambda _request: httpx.Response(200, json={"message": {"content": None}})
        ),
    )


def _foundry_invalid_provider() -> FoundryLocalProvider:
    return _foundry_provider(mode="invalid")


def _openai_timeout_provider() -> OpenAICompatibleProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("synthetic timeout", request=request)

    return OpenAICompatibleProvider(
        provider_id="openai-compatible",
        base_url="https://provider.invalid/v1",
        kind=ProviderKind.CLOUD,
        default_model="default-model",
        supports_private_data=True,
        client_factory=_http_client_factory(handler),
    )


def _ollama_timeout_provider() -> OllamaProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("synthetic timeout", request=request)

    return OllamaProvider(
        default_model="default-model",
        client_factory=_http_client_factory(handler),
    )


def _foundry_timeout_provider() -> FoundryLocalProvider:
    return _foundry_provider(mode="timeout")


def _openai_secret_failure_provider(canary: str) -> OpenAICompatibleProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(canary, request=request)

    return OpenAICompatibleProvider(
        provider_id="openai-compatible",
        base_url="https://provider.invalid/v1",
        kind=ProviderKind.CLOUD,
        default_model="default-model",
        supports_private_data=True,
        api_key=canary,
        client_factory=_http_client_factory(handler),
    )


def _ollama_secret_failure_provider(canary: str) -> OllamaProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(canary, request=request)

    return OllamaProvider(
        default_model="default-model",
        client_factory=_http_client_factory(handler),
    )


def _foundry_secret_failure_provider(canary: str) -> FoundryLocalProvider:
    return _foundry_provider(mode="secret", canary=canary)


def _http_blocking_probe(*, ollama: bool) -> _BlockingProbe:
    started = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()

    async def handler(_request: httpx.Request) -> httpx.Response:
        started.set()
        try:
            await release.wait()
        finally:
            finished.set()
        if ollama:
            return httpx.Response(
                200,
                json={
                    "model": _MODEL,
                    "message": {"content": "late"},
                    "done": True,
                },
            )
        return httpx.Response(
            200,
            json={
                "model": _MODEL,
                "choices": [{"message": {"content": "late"}}],
            },
        )

    transport = httpx.MockTransport(handler)

    def factory(**kwargs: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport, **kwargs)

    if ollama:
        provider: Any = OllamaProvider(
            default_model="default-model",
            client_factory=factory,
        )
    else:
        provider = OpenAICompatibleProvider(
            provider_id="openai-compatible",
            base_url="https://provider.invalid/v1",
            kind=ProviderKind.CLOUD,
            default_model="default-model",
            supports_private_data=True,
            client_factory=factory,
        )

    async def wait_started() -> None:
        await asyncio.wait_for(started.wait(), timeout=1.0)

    async def wait_finished() -> None:
        await asyncio.wait_for(finished.wait(), timeout=1.0)

    return _BlockingProbe(provider, wait_started, release.set, wait_finished)


def _foundry_blocking_probe() -> _BlockingProbe:
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    provider = _foundry_provider(
        started=started,
        release=release,
        finished=finished,
    )

    async def wait_started() -> None:
        assert await asyncio.to_thread(started.wait, 1.0)

    async def wait_finished() -> None:
        assert await asyncio.to_thread(finished.wait, 1.0)

    return _BlockingProbe(provider, wait_started, release.set, wait_finished)


def _assert_request(observation: _Observation) -> None:
    assert observation.messages == [
        {"role": "system", "content": "Follow the contract."},
        {"role": "user", "content": "hello"},
    ]
    assert observation.model == _MODEL
    assert observation.temperature == 0.25


def _assert_http_request(observation: _Observation) -> None:
    _assert_request(observation)
    assert observation.timeout_seconds == 2.0


def _assert_openai_configuration_failure() -> None:
    with pytest.raises(ValueError, match="no_llm"):
        OpenAICompatibleProvider(
            provider_id="invalid",
            base_url="https://provider.invalid/v1",
            kind=ProviderKind.NO_LLM,
            default_model="model",
        )


def _assert_ollama_configuration_failure() -> None:
    with pytest.raises(ValueError, match="default_model"):
        OllamaProvider(default_model=" ")


def _assert_foundry_configuration_failure() -> None:
    with pytest.raises(ValueError, match="resource_observer"):
        FoundryLocalProvider(
            default_model="model",
            resource_policy=ModelResourcePolicy(max_cpu_percent=80),
        )


_CASES = (
    _AdapterCase(
        name="openai-compatible",
        provider_id="openai-compatible",
        kind=ProviderKind.CLOUD,
        success_provider=_openai_success_provider,
        invalid_provider=_openai_invalid_provider,
        timeout_provider=_openai_timeout_provider,
        secret_failure_provider=_openai_secret_failure_provider,
        blocking_probe=lambda: _http_blocking_probe(ollama=False),
        assert_request=_assert_http_request,
        assert_configuration_failure=_assert_openai_configuration_failure,
    ),
    _AdapterCase(
        name="ollama",
        provider_id="ollama",
        kind=ProviderKind.LOCAL,
        success_provider=_ollama_success_provider,
        invalid_provider=_ollama_invalid_provider,
        timeout_provider=_ollama_timeout_provider,
        secret_failure_provider=_ollama_secret_failure_provider,
        blocking_probe=lambda: _http_blocking_probe(ollama=True),
        assert_request=_assert_http_request,
        assert_configuration_failure=_assert_ollama_configuration_failure,
    ),
    _AdapterCase(
        name="foundry-local",
        provider_id="foundry-local",
        kind=ProviderKind.LOCAL,
        success_provider=lambda observation: _foundry_provider(observation=observation),
        invalid_provider=_foundry_invalid_provider,
        timeout_provider=_foundry_timeout_provider,
        secret_failure_provider=_foundry_secret_failure_provider,
        blocking_probe=_foundry_blocking_probe,
        assert_request=_assert_request,
        assert_configuration_failure=_assert_foundry_configuration_failure,
    ),
)


@pytest.mark.parametrize("case", _CASES, ids=lambda case: case.name)
def test_provider_conformance_preserves_request_and_returns_canonical_success(
    case: _AdapterCase,
) -> None:
    observation = _Observation()
    provider = case.success_provider(observation)
    request = _request(case.provider_id)
    original = copy.deepcopy(request)

    response = asyncio.run(provider.complete(request))

    assert request == original
    case.assert_request(observation)
    assert response.request_id == request.request_id
    assert response.text == "answer"
    assert response.provider_id == case.provider_id
    assert response.provider_kind is case.kind
    assert response.model == _MODEL
    assert isinstance(response.usage, ModelUsage)
    assert response.usage == ModelUsage(input_tokens=5, output_tokens=3, total_tokens=8)
    for count in (
        response.usage.input_tokens,
        response.usage.output_tokens,
        response.usage.total_tokens,
    ):
        assert count is None or (isinstance(count, int) and not isinstance(count, bool))


@pytest.mark.parametrize("case", _CASES, ids=lambda case: case.name)
def test_provider_conformance_invalid_response_fails_closed(case: _AdapterCase) -> None:
    provider = case.invalid_provider()

    with pytest.raises(ModelGatewayError) as exc_info:
        asyncio.run(provider.complete(_request(case.provider_id)))

    assert exc_info.value.code is ModelErrorCode.PROVIDER_ERROR
    assert exc_info.value.provider_id == case.provider_id
    assert exc_info.value.retryable is False


@pytest.mark.parametrize("case", _CASES, ids=lambda case: case.name)
def test_provider_conformance_timeout_is_typed_and_bounded(case: _AdapterCase) -> None:
    provider = case.timeout_provider()

    with pytest.raises(ModelGatewayError) as exc_info:
        asyncio.run(provider.complete(_request(case.provider_id)))

    assert exc_info.value.code is ModelErrorCode.TIMEOUT
    assert exc_info.value.provider_id == case.provider_id
    assert exc_info.value.retryable is False


@pytest.mark.parametrize("case", _CASES, ids=lambda case: case.name)
def test_provider_conformance_cancel_propagates_without_success(case: _AdapterCase) -> None:
    async def scenario() -> None:
        probe = case.blocking_probe()
        task = asyncio.create_task(probe.provider.complete(_request(case.provider_id)))
        await probe.wait_started()
        task.cancel()
        try:
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            probe.release()
            await probe.wait_finished()

    asyncio.run(scenario())


@pytest.mark.parametrize("case", _CASES, ids=lambda case: case.name)
def test_provider_conformance_configuration_failure_is_preflighted(case: _AdapterCase) -> None:
    case.assert_configuration_failure()


@pytest.mark.parametrize("case", _CASES, ids=lambda case: case.name)
def test_provider_conformance_never_uses_undeclared_fallback(case: _AdapterCase) -> None:
    gateway = ModelGateway()
    gateway.register(case.timeout_provider())
    fallback = _FallbackSpy()
    gateway.register(fallback, default=True)

    with pytest.raises(ModelGatewayError) as exc_info:
        asyncio.run(gateway.complete(_request(case.provider_id)))

    assert exc_info.value.code is ModelErrorCode.TIMEOUT
    assert exc_info.value.provider_id == case.provider_id
    assert fallback.calls == 0


@pytest.mark.parametrize("case", _CASES, ids=lambda case: case.name)
def test_provider_conformance_public_failure_redacts_upstream_diagnostics(
    case: _AdapterCase,
) -> None:
    gateway = ModelGateway()
    gateway.register(case.secret_failure_provider(_CANARY))

    with pytest.raises(ModelGatewayError) as exc_info:
        asyncio.run(gateway.complete(_request(case.provider_id)))

    error = exc_info.value
    assert error.code is ModelErrorCode.PROVIDER_ERROR
    assert error.provider_id == case.provider_id
    assert _CANARY not in str(error)
    assert _CANARY not in repr(error)
    assert error.__cause__ is None
    assert error.__context__ is None