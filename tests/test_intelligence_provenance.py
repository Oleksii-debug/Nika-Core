from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from nika_core.builder.compiler import AgentCompiler
from nika_core.builder.repository import AgentDefinitionRepository
from nika_core.builder.spec import AgentDefinition
from nika_core.data.sqlite import SQLiteStore
from nika_core.intelligence.modes import IntelligenceMode
from nika_core.intelligence.provenance import (
    IntelligenceProvenance,
    IntelligenceResultStatus,
)
from nika_core.model_gateway.contracts import (
    ModelErrorCode,
    ModelFailureEffect,
    ModelGatewayError,
    ModelRequest,
    ModelResponse,
    ProviderCapabilities,
    ProviderKind,
)
from nika_core.model_gateway.gateway import ModelGateway, model_identity_fingerprint
from nika_core.multi_agent.model_gateway_runtime import ModelGatewayAgentRuntime
from nika_core.runtime.contracts import RuntimeOutcome, RuntimeRequest

_SECRET_PROMPT_CANARY = "prompt-secret-canary-dev40"


def _definitions(tmp_path: Path) -> AgentDefinitionRepository:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    repository = AgentDefinitionRepository(store)
    compiler = AgentCompiler(tools=(), model_profiles={"configured"})
    definition = AgentDefinition(
        agent_id="worker",
        name="worker",
        goal="Return bounded evidence.",
        instructions=f"Never reveal {_SECRET_PROMPT_CANARY}.",
        model_profile="configured",
    )
    repository.save_draft(compiler.compile(definition))
    repository.activate(definition)
    return repository


def _request() -> RuntimeRequest:
    return RuntimeRequest(
        task_id="task-provenance",
        thread_id="thread-worker",
        payload={
            "agent_id": "worker",
            "agent_version": 1,
            "handoff": {"private_note": _SECRET_PROMPT_CANARY},
        },
    )


class _StaticProvider:
    def __init__(self, *, provider_id: str, kind: ProviderKind, model: str) -> None:
        self._capabilities = ProviderCapabilities(
            provider_id=provider_id,
            kind=kind,
            supports_private_data=True,
        )
        self._model = model

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    async def complete(self, request: ModelRequest) -> ModelResponse:
        return ModelResponse(
            request_id=request.request_id,
            text="bounded model result",
            provider_id=self._capabilities.provider_id,
            provider_kind=self._capabilities.kind,
            model=self._model,
        )


class _FailingProvider(_StaticProvider):
    async def complete(self, request: ModelRequest) -> ModelResponse:
        del request
        raise ModelGatewayError(
            ModelErrorCode.PROVIDER_ERROR,
            f"provider diagnostic {_SECRET_PROMPT_CANARY}",
            provider_id=self.capabilities.provider_id,
            retryable=False,
            failure_effect=ModelFailureEffect.NO_EFFECT,
        )


@pytest.mark.parametrize(
    ("provider_id", "kind", "mode", "model"),
    (
        ("ollama", ProviderKind.LOCAL, IntelligenceMode.EXTERNAL_LOCAL, "qwen3:8b"),
        (
            "configured-api",
            ProviderKind.CLOUD,
            IntelligenceMode.EXTERNAL_API,
            "api-model",
        ),
    ),
)
def test_model_success_has_truthful_content_free_provenance(
    tmp_path: Path,
    provider_id: str,
    kind: ProviderKind,
    mode: IntelligenceMode,
    model: str,
) -> None:
    gateway = ModelGateway()
    gateway.register(
        _StaticProvider(provider_id=provider_id, kind=kind, model=model),
        default=True,
    )
    runtime = ModelGatewayAgentRuntime(
        gateway=gateway,
        definitions=_definitions(tmp_path),
        provider_id=provider_id,
        provider_kind=kind,
        intelligence_mode=mode,
        model=model,
    )

    result = asyncio.run(runtime.run(_request()))

    assert result.outcome is RuntimeOutcome.COMPLETED
    provenance = result.output["intelligence_provenance"]
    assert provenance == {
        "schema": "nika.intelligence.provenance.v1",
        "origin": "model",
        "intelligence_mode": mode.value,
        "provider_kind": kind.value,
        "provider_id": provider_id,
        "model_fingerprint": model_identity_fingerprint(model),
        "request_correlation_id": "task-provenance:thread-worker",
        "status": "succeeded",
    }
    assert IntelligenceProvenance.from_payload(provenance).status is (
        IntelligenceResultStatus.SUCCEEDED
    )
    assert _SECRET_PROMPT_CANARY not in repr(provenance)
    assert model not in repr(provenance)


@pytest.mark.parametrize(
    ("provider_id", "kind", "mode"),
    (
        ("ollama", ProviderKind.LOCAL, IntelligenceMode.EXTERNAL_LOCAL),
        ("configured-api", ProviderKind.CLOUD, IntelligenceMode.EXTERNAL_API),
    ),
)
def test_model_failure_keeps_route_provenance_without_provider_diagnostic(
    tmp_path: Path,
    provider_id: str,
    kind: ProviderKind,
    mode: IntelligenceMode,
) -> None:
    model = "fixture-model"
    gateway = ModelGateway()
    gateway.register(
        _FailingProvider(provider_id=provider_id, kind=kind, model=model),
        default=True,
    )
    runtime = ModelGatewayAgentRuntime(
        gateway=gateway,
        definitions=_definitions(tmp_path),
        provider_id=provider_id,
        provider_kind=kind,
        intelligence_mode=mode,
        model=model,
    )

    result = asyncio.run(runtime.run(_request()))

    assert result.outcome is RuntimeOutcome.FAILED
    provenance = result.output["intelligence_provenance"]
    parsed = IntelligenceProvenance.from_payload(provenance)
    assert parsed.intelligence_mode is mode
    assert parsed.provider_kind is kind
    assert parsed.status is IntelligenceResultStatus.FAILED
    assert parsed.request_correlation_id == "task-provenance:thread-worker"
    assert _SECRET_PROMPT_CANARY not in repr(result)


def test_cloud_and_local_provenance_cannot_be_relabelled(tmp_path: Path) -> None:
    definitions = _definitions(tmp_path)

    with pytest.raises(ValueError, match="provider kind"):
        ModelGatewayAgentRuntime(
            gateway=ModelGateway(),
            definitions=definitions,
            provider_id="configured-api",
            provider_kind=ProviderKind.CLOUD,
            intelligence_mode=IntelligenceMode.EXTERNAL_LOCAL,
            model="fixture-model",
        )

    payload = IntelligenceProvenance(
        intelligence_mode=IntelligenceMode.EXTERNAL_API,
        provider_kind=ProviderKind.CLOUD,
        provider_id="configured-api",
        model_fingerprint=model_identity_fingerprint("fixture-model"),
        request_correlation_id="task:thread",
        status=IntelligenceResultStatus.SUCCEEDED,
    ).to_payload()
    payload["provider_kind"] = "local"
    with pytest.raises(ValueError, match="provider kind"):
        IntelligenceProvenance.from_payload(payload)


def test_deterministic_brain_cannot_claim_model_generated_provenance(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="deterministic"):
        IntelligenceProvenance(
            intelligence_mode=IntelligenceMode.DETERMINISTIC,
            provider_kind=ProviderKind.NO_LLM,
            provider_id="deterministic-brain",
            model_fingerprint=model_identity_fingerprint(None),
            request_correlation_id="task:deterministic",
            status=IntelligenceResultStatus.SUCCEEDED,
        )

    with pytest.raises(ValueError, match="deterministic"):
        ModelGatewayAgentRuntime(
            gateway=ModelGateway(),
            definitions=_definitions(tmp_path),
            provider_id="deterministic-brain",
            provider_kind=ProviderKind.NO_LLM,
            intelligence_mode=IntelligenceMode.DETERMINISTIC,
        )


def test_supported_route_inference_is_explicit_and_truthful(tmp_path: Path) -> None:
    definitions = _definitions(tmp_path)
    foundry = ModelGatewayAgentRuntime(
        gateway=ModelGateway(),
        definitions=definitions,
        provider_id="foundry-local",
        provider_kind=ProviderKind.LOCAL,
    )
    ollama = ModelGatewayAgentRuntime(
        gateway=ModelGateway(),
        definitions=definitions,
        provider_id="ollama",
        provider_kind=ProviderKind.LOCAL,
    )
    cloud = ModelGatewayAgentRuntime(
        gateway=ModelGateway(),
        definitions=definitions,
        provider_id="configured-api",
        provider_kind=ProviderKind.CLOUD,
    )

    assert foundry.intelligence_mode is IntelligenceMode.EMBEDDED_LOCAL
    assert ollama.intelligence_mode is IntelligenceMode.EXTERNAL_LOCAL
    assert cloud.intelligence_mode is IntelligenceMode.EXTERNAL_API
