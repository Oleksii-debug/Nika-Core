from __future__ import annotations

import asyncio
from contextlib import contextmanager
from typing import Iterator

from nika_core.model_gateway.contracts import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ProviderCapabilities,
    ProviderKind,
)
from nika_core.model_gateway.gateway import ModelGateway
from nika_core.model_gateway.parallel import complete_parallel


class _ScopeProbe:
    def __init__(self) -> None:
        self.entered_tasks: dict[str, asyncio.Task[object] | None] = {}
        self.exited: list[str] = []

    @contextmanager
    def scope_for(self, *, request_id: str) -> Iterator[None]:
        self.entered_tasks[request_id] = asyncio.current_task()
        try:
            yield
        finally:
            self.exited.append(request_id)


class _TaskIdentityProvider:
    def __init__(
        self,
        *,
        provider_id: str,
        observed_tasks: dict[str, asyncio.Task[object] | None],
    ) -> None:
        self._capabilities = ProviderCapabilities(
            provider_id=provider_id,
            kind=ProviderKind.LOCAL,
            supports_private_data=True,
        )
        self._observed_tasks = observed_tasks

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self._observed_tasks[request.request_id] = asyncio.current_task()
        return ModelResponse(
            request_id=request.request_id,
            text="ok",
            provider_id=self.capabilities.provider_id,
            provider_kind=self.capabilities.kind,
            model=request.model or "m",
        )


class _BlockingProvider:
    def __init__(
        self,
        *,
        entered: asyncio.Event,
        release: asyncio.Event,
    ) -> None:
        self._capabilities = ProviderCapabilities(
            provider_id="provider",
            kind=ProviderKind.LOCAL,
            supports_private_data=True,
        )
        self._entered = entered
        self._release = release
        self.calls: list[str] = []

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls.append(request.request_id)
        self._entered.set()
        await self._release.wait()
        return ModelResponse(
            request_id=request.request_id,
            text="ok",
            provider_id=self.capabilities.provider_id,
            provider_kind=self.capabilities.kind,
            model=request.model or "m",
        )


def _request(
    request_id: str,
    *,
    provider_id: str,
    timeout_seconds: float = 1.0,
) -> ModelRequest:
    return ModelRequest(
        request_id=request_id,
        messages=(ModelMessage(role="user", content="child scope proof"),),
        provider_id=provider_id,
        model="m",
        timeout_seconds=timeout_seconds,
    )


def test_execution_scope_is_entered_inside_each_exact_child_task() -> None:
    async def scenario() -> tuple[
        asyncio.Task[object] | None,
        dict[str, asyncio.Task[object] | None],
        dict[str, asyncio.Task[object] | None],
        list[str],
    ]:
        parent_task = asyncio.current_task()
        scope = _ScopeProbe()
        observed_tasks: dict[str, asyncio.Task[object] | None] = {}
        gateway = ModelGateway()
        gateway.register(
            _TaskIdentityProvider(
                provider_id="route-a",
                observed_tasks=observed_tasks,
            )
        )
        gateway.register(
            _TaskIdentityProvider(
                provider_id="route-b",
                observed_tasks=observed_tasks,
            )
        )

        result = await complete_parallel(
            gateway,
            (
                _request("request-a", provider_id="route-a"),
                _request("request-b", provider_id="route-b"),
            ),
            max_parallel=2,
            execution_scopes=scope,
        )
        assert result.fully_successful is True
        return parent_task, scope.entered_tasks, observed_tasks, scope.exited

    parent_task, scope_tasks, provider_tasks, exited = asyncio.run(scenario())

    assert set(scope_tasks) == {"request-a", "request-b"}
    assert set(provider_tasks) == {"request-a", "request-b"}
    assert scope_tasks["request-a"] is provider_tasks["request-a"]
    assert scope_tasks["request-b"] is provider_tasks["request-b"]
    assert scope_tasks["request-a"] is not scope_tasks["request-b"]
    assert scope_tasks["request-a"] is not parent_task
    assert scope_tasks["request-b"] is not parent_task
    assert sorted(exited) == ["request-a", "request-b"]


def test_admission_timeout_never_enters_child_execution_scope() -> None:
    async def scenario() -> tuple[list[str], list[str]]:
        entered = asyncio.Event()
        release = asyncio.Event()
        provider = _BlockingProvider(entered=entered, release=release)
        gateway = ModelGateway()
        gateway.register(provider)
        scope = _ScopeProbe()

        batch = asyncio.create_task(
            complete_parallel(
                gateway,
                (
                    _request("blocking", provider_id="provider", timeout_seconds=1.0),
                    _request("queued", provider_id="provider", timeout_seconds=0.02),
                ),
                max_parallel=1,
                execution_scopes=scope,
            )
        )
        await asyncio.wait_for(entered.wait(), timeout=1.0)
        await asyncio.sleep(0.05)
        release.set()
        result = await asyncio.wait_for(batch, timeout=1.0)
        assert result.outcomes[1].failure is not None
        return list(scope.entered_tasks), provider.calls

    scope_entries, provider_calls = asyncio.run(scenario())

    assert scope_entries == ["blocking"]
    assert provider_calls == ["blocking"]
