from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Awaitable, Callable

import httpx
import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog
from nika_core.model_gateway.contracts import (
    ModelErrorCode,
    ModelFailureEffect,
    ModelGatewayError,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    PrivacyClass,
    ProviderCapabilities,
    ProviderKind,
)
from nika_core.model_gateway.foundry_local import FoundryLocalProvider
from nika_core.model_gateway.gateway import ModelGateway
from nika_core.model_gateway.providers import OpenAICompatibleProvider


class _BlockingProvider:
    def __init__(self, *, provider_id: str = "blocking") -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0
        self._capabilities = ProviderCapabilities(
            provider_id=provider_id,
            kind=ProviderKind.LOCAL,
            supports_private_data=True,
            supports_hard_cancellation=False,
        )

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        self.started.set()
        await self.release.wait()
        return _response(request, provider=self)


class _ImmediateTimeoutProvider(_BlockingProvider):
    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        raise ModelGatewayError(
            ModelErrorCode.TIMEOUT,
            "fixture timeout",
            provider_id=self.capabilities.provider_id,
            retryable=False,
            failure_effect=ModelFailureEffect.UNKNOWN,
        )


class _CancelDuringValidationResponse(ModelResponse):
    """Inject cancellation after provider return and before durable completion audit."""

    def __getattribute__(self, name: str) -> Any:
        if name == "latency_ms":
            task = asyncio.current_task()
            if task is not None and task.cancelling() == 0:
                task.cancel()
        return super().__getattribute__(name)


class _PostResponseCancelProvider(_BlockingProvider):
    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        return _CancelDuringValidationResponse(
            request_id=request.request_id,
            text="late-success",
            provider_id=self.capabilities.provider_id,
            provider_kind=self.capabilities.kind,
            model=request.model or "fixture-model",
            usage=ModelUsage(input_tokens=1, output_tokens=1, total_tokens=2),
            latency_ms=1.0,
        )


def _request(*, request_id: str = "cancel-contract", provider_id: str = "blocking") -> ModelRequest:
    return ModelRequest(
        request_id=request_id,
        messages=(ModelMessage(role="user", content="hello"),),
        model="fixture-model",
        provider_id=provider_id,
        privacy=PrivacyClass.PUBLIC,
        timeout_seconds=5.0,
    )


def _response(request: ModelRequest, *, provider: _BlockingProvider) -> ModelResponse:
    return ModelResponse(
        request_id=request.request_id,
        text="ok",
        provider_id=provider.capabilities.provider_id,
        provider_kind=provider.capabilities.kind,
        model=request.model or "fixture-model",
        usage=ModelUsage(input_tokens=1, output_tokens=1, total_tokens=2),
        latency_ms=1.0,
    )


def _event_types(audit: AuditLog, request_id: str) -> list[str]:
    return [
        event.event_type
        for event in audit.list_for(entity_type="model_request", entity_id=request_id)
    ]


def test_cancel_before_inference_never_invokes_provider_or_commits_success(tmp_path) -> None:
    async def scenario() -> None:
        store = SQLiteStore(tmp_path / "before.db")
        store.initialize()
        audit = AuditLog(store)
        provider = _BlockingProvider()
        gateway = ModelGateway(audit_log=audit)
        gateway.register(provider)

        task = asyncio.create_task(gateway.complete(_request()))
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert provider.calls == 0
        assert "model.completed" not in _event_types(audit, "cancel-contract")

    asyncio.run(scenario())


def test_cancel_while_waiting_provider_wins_over_pending_timeout(tmp_path) -> None:
    async def scenario() -> None:
        store = SQLiteStore(tmp_path / "during.db")
        store.initialize()
        audit = AuditLog(store)
        provider = _BlockingProvider()
        gateway = ModelGateway(audit_log=audit)
        gateway.register(provider)

        task = asyncio.create_task(gateway.complete(_request()))
        await provider.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert _event_types(audit, "cancel-contract") == [
            "model.requested",
            "model.cancelled",
        ]

    asyncio.run(scenario())


def test_timeout_that_wins_never_falls_through_to_success(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "timeout.db")
    store.initialize()
    audit = AuditLog(store)
    provider = _ImmediateTimeoutProvider()
    gateway = ModelGateway(audit_log=audit)
    gateway.register(provider)

    with pytest.raises(ModelGatewayError) as caught:
        asyncio.run(gateway.complete(_request()))

    assert caught.value.code is ModelErrorCode.TIMEOUT
    assert _event_types(audit, "cancel-contract") == [
        "model.requested",
        "model.failed",
    ]


def test_cancel_after_provider_response_cannot_commit_completed(tmp_path) -> None:
    """Regression oracle for the response -> durable-commit cancellation window.

    The response object requests cancellation during trusted response validation,
    i.e. strictly after provider.complete() returned and before ModelGateway writes
    model.completed.  A terminal cancellation must be observed before that durable
    success evidence is appended.
    """

    async def scenario() -> None:
        store = SQLiteStore(tmp_path / "post-response.db")
        store.initialize()
        audit = AuditLog(store)
        provider = _PostResponseCancelProvider()
        gateway = ModelGateway(audit_log=audit)
        gateway.register(provider)

        task = asyncio.create_task(gateway.complete(_request()))
        with pytest.raises(asyncio.CancelledError):
            await task

        events = _event_types(audit, "cancel-contract")
        assert "model.completed" not in events
        assert events[-1] == "model.cancelled"

    asyncio.run(scenario())


@dataclass
class _AdapterHarness:
    provider: Any
    request: Callable[[str], ModelRequest]
    wait_started: Callable[[], Awaitable[None]]
    release_late_result: Callable[[], None]
    wait_drained: Callable[[], Awaitable[None]]


class _GenericClientState:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0


class _GenericClient:
    def __init__(self, state: _GenericClientState) -> None:
        self._state = state

    async def __aenter__(self) -> _GenericClient:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def post(self, url: str, *, headers: object, json: object) -> httpx.Response:
        self._state.calls += 1
        if self._state.calls == 1:
            self._state.started.set()
            await self._state.release.wait()
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={
                "model": "fixture-model",
                "choices": [{"message": {"content": "generic-ok"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )


def _generic_harness() -> _AdapterHarness:
    state = _GenericClientState()
    provider = OpenAICompatibleProvider(
        provider_id="generic-test",
        base_url="https://provider.invalid/v1",
        kind=ProviderKind.CLOUD,
        default_model="fixture-model",
        supports_private_data=True,
        client_factory=lambda **_kwargs: _GenericClient(state),
    )

    async def wait_started() -> None:
        await state.started.wait()

    async def wait_drained() -> None:
        await asyncio.sleep(0)

    return _AdapterHarness(
        provider=provider,
        request=lambda request_id: _request(
            request_id=request_id,
            provider_id="generic-test",
        ),
        wait_started=wait_started,
        release_late_result=state.release.set,
        wait_drained=wait_drained,
    )


class _FoundryModel:
    def __init__(self) -> None:
        self.id = "fixture-id"
        self.alias = "fixture-model"
        self.is_cached = True
        self.is_loaded = False
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()

    def load(self) -> None:
        self.is_loaded = True

    def unload(self) -> None:
        self.is_loaded = False

    def get_chat_client(self) -> object:
        model = self

        class Client:
            settings = SimpleNamespace(temperature=None)

            def complete_chat(self, messages: object) -> object:
                del messages
                model.started.set()
                model.release.wait()
                model.finished.set()
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content="local-ok"))],
                    usage=SimpleNamespace(
                        prompt_tokens=1,
                        completion_tokens=1,
                        total_tokens=2,
                    ),
                )

        return Client()


class _FoundryManager:
    def __init__(self, model: _FoundryModel) -> None:
        self.catalog = SimpleNamespace(get_model=lambda _alias: model)


def _foundry_harness() -> _AdapterHarness:
    model = _FoundryModel()
    provider = FoundryLocalProvider(
        default_model="fixture-model",
        manager_factory=lambda: _FoundryManager(model),
    )

    async def wait_started() -> None:
        assert await asyncio.to_thread(model.started.wait, 1.0)

    async def wait_drained() -> None:
        assert await asyncio.to_thread(model.finished.wait, 1.0)
        # Let the worker done callback release the provider's inference slot.
        await asyncio.sleep(0)

    return _AdapterHarness(
        provider=provider,
        request=lambda request_id: _request(
            request_id=request_id,
            provider_id="foundry-local",
        ),
        wait_started=wait_started,
        release_late_result=model.release.set,
        wait_drained=wait_drained,
    )


@pytest.mark.parametrize("harness_factory", [_generic_harness, _foundry_harness], ids=["generic", "local"])
def test_provider_contract_discards_late_result_after_cancel_and_allows_fresh_request(
    harness_factory: Callable[[], _AdapterHarness],
) -> None:
    async def scenario() -> None:
        harness = harness_factory()
        cancelled = asyncio.create_task(harness.provider.complete(harness.request("cancelled")))
        await harness.wait_started()

        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        assert cancelled.cancelled()

        # Local native inference may finish after caller cancellation. Releasing
        # it must not resurrect the cancelled awaiter. Generic HTTP cancellation
        # has no background worker, but follows the same provider contract.
        harness.release_late_result()
        await harness.wait_drained()
        assert cancelled.cancelled()

        fresh = await harness.provider.complete(harness.request("fresh"))
        assert fresh.request_id == "fresh"
        assert fresh.text in {"generic-ok", "local-ok"}

    asyncio.run(scenario())
