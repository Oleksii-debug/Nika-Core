from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from typing import get_type_hints

from nika_core.intelligence import modes as modes_module
from nika_core.intelligence.modes import (
    IntelligenceMode,
    IntelligenceModePolicy,
    IntelligenceModeRouter,
    ModelCompletionPort,
)
from nika_core.model_gateway.contracts import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ProviderKind,
)


class CompletionOnlyGateway:
    """Fixture with only the provider-neutral completion surface."""

    def __init__(self) -> None:
        self.request: ModelRequest | None = None

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.request = request
        assert request.provider_id is not None
        assert request.provider_kind is not None
        return ModelResponse(
            request_id=request.request_id,
            text="ok",
            provider_id=request.provider_id,
            provider_kind=request.provider_kind,
            model=request.model or "fixture-model",
        )


def test_mode_router_contract_uses_provider_neutral_completion_port() -> None:
    hints = get_type_hints(IntelligenceModeRouter.__init__)

    assert hints["gateway"] is ModelCompletionPort

    source_path = Path(modes_module.__file__ or "")
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imported_modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    imported_modules.update(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    forbidden_concrete_modules = {
        "nika_core.model_gateway.gateway",
        "nika_core.model_gateway.providers",
        "nika_core.model_gateway.foundry_local",
        "foundry_local",
        "foundry_local_sdk",
        "ollama",
        "openai",
    }
    assert imported_modules.isdisjoint(forbidden_concrete_modules)


def test_completion_only_port_preserves_explicit_mode_routing() -> None:
    gateway = CompletionOnlyGateway()
    router = IntelligenceModeRouter(
        gateway=gateway,
        policy=IntelligenceModePolicy(external_local_provider_id="fixture-local"),
    )
    request = ModelRequest(
        request_id="request-1",
        messages=(ModelMessage(role="user", content="task"),),
        provider_id="untrusted-cloud",
        provider_kind=ProviderKind.CLOUD,
        fallback_provider_ids=("untrusted-fallback",),
        model="fixture-model",
    )

    response = asyncio.run(router.complete_model(IntelligenceMode.EXTERNAL_LOCAL, request))

    assert response.provider_id == "fixture-local"
    assert response.provider_kind is ProviderKind.LOCAL
    assert gateway.request is not None
    assert gateway.request.provider_id == "fixture-local"
    assert gateway.request.provider_kind is ProviderKind.LOCAL
    assert gateway.request.fallback_provider_ids == ()
    assert not hasattr(gateway, "register")
