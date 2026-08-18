from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from nika_core.builder.compiler import AgentCompiler, RiskTier
from nika_core.builder.drafting import AgentDraftService
from nika_core.builder.repository import AgentDefinitionRepository
from nika_core.builder.spec import AgentDefinition, ToolGrant
from nika_core.data.sqlite import SQLiteStore
from nika_core.model_gateway.contracts import ModelResponse, ModelUsage, ProviderKind
from nika_core.tools import ToolRisk, ToolSpec


def _definition(*, version: int = 1, grants: tuple[ToolGrant, ...] = ()) -> AgentDefinition:
    return AgentDefinition(
        agent_id="research.agent",
        version=version,
        name="Research agent",
        goal="Collect evidence",
        instructions="Use only declared tools and report sources.",
        model_profile="local-default",
        schedule_id="daily",
        resource_budget_ref="standard",
        tool_grants=grants,
    )


def _compiler(*tools: ToolSpec) -> AgentCompiler:
    return AgentCompiler(
        tools=tools,
        model_profiles={"local-default"},
        schedule_ids={"daily"},
        resource_budget_refs={"standard"},
    )


def test_compiler_fails_closed_for_unknown_tool_and_reference() -> None:
    definition = _definition(grants=(ToolGrant(tool_id="web.read", max_risk=0),))
    with pytest.raises(ValueError, match="unknown tool"):
        _compiler().compile(definition)

    bad_model = definition.model_copy(update={"tool_grants": (), "model_profile": "invented"})
    with pytest.raises(ValueError, match="unknown model profile"):
        _compiler().compile(bad_model)


def test_compiler_requires_exact_registered_risk_and_marks_r4() -> None:
    high_impact = ToolSpec("release.publish", "Publish a release", ToolRisk.HIGH_IMPACT)
    too_low = _definition(
        grants=(ToolGrant(tool_id="release.publish", max_risk=RiskTier.R2_EXTERNAL_WRITE),)
    )
    with pytest.raises(ValueError, match="requires R4_HIGH_IMPACT"):
        _compiler(high_impact).compile(too_low)

    accepted = _definition(
        grants=(ToolGrant(tool_id="release.publish", max_risk=RiskTier.R4_HIGH_IMPACT),)
    )
    result = _compiler(high_impact).compile(accepted)
    assert result.requires_human_approval is True
    assert result.required_human_approvals == ("release.publish",)
    assert result.highest_risk is RiskTier.R4_HIGH_IMPACT


def test_repository_versions_and_activation_are_fail_closed(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    repository = AgentDefinitionRepository(store)
    dangerous = ToolSpec("release.publish", "Publish a release", ToolRisk.HIGH_IMPACT)
    definition = _definition(
        grants=(ToolGrant(tool_id="release.publish", max_risk=RiskTier.R4_HIGH_IMPACT),)
    )
    compiled = _compiler(dangerous).compile(definition)

    repository.save_draft(compiled)
    persisted = repository.get(definition.agent_id, definition.version)
    assert persisted is not None
    assert persisted.required_human_approvals == ("release.publish",)
    assert persisted.highest_risk == RiskTier.R4_HIGH_IMPACT

    with pytest.raises(PermissionError, match="explicit human approval"):
        repository.activate(definition)
    assert repository.active(definition.agent_id) is None

    repository.activate(
        definition,
        approved_tool_ids=frozenset({"release.publish"}),
    )
    active = repository.active(definition.agent_id)
    assert active is not None
    assert active.definition == definition
    assert active.status == "active"

    safe = ToolSpec("web.read", "Read a page", ToolRisk.READ_ONLY)
    second = definition.model_copy(
        update={
            "version": 2,
            "goal": "Collect and compare evidence",
            "tool_grants": (ToolGrant(tool_id="web.read", max_risk=RiskTier.R0_READ_ONLY),),
        }
    )
    second_compiled = _compiler(safe).compile(second)
    repository.save_draft(second_compiled)
    repository.activate(second)
    current = repository.active(definition.agent_id)
    assert current is not None
    assert current.definition.version == 2
    retired = repository.get(definition.agent_id, 1)
    assert retired is not None
    assert retired.status == "retired"
    assert repository.next_version(definition.agent_id) == 3

    with pytest.raises(ValueError, match="expected 3"):
        repository.save_draft(second_compiled)

    assert store.schema_version() == 6


def test_activation_rejects_definition_mutation(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    repository = AgentDefinitionRepository(store)
    definition = _definition()
    repository.save_draft(_compiler().compile(definition))

    altered = definition.model_copy(update={"goal": "Changed after review"})
    with pytest.raises(ValueError, match="differs from persisted"):
        repository.activate(altered)


class _DraftGateway:
    def __init__(self, text: str) -> None:
        self.text = text
        self.last_request = None

    async def complete(self, request):
        self.last_request = request
        return ModelResponse(
            request_id=request.request_id,
            text=self.text,
            provider_id="test",
            provider_kind=ProviderKind.NO_LLM,
            model="fixture",
            usage=ModelUsage(),
        )


def test_natural_language_draft_is_schema_validated() -> None:
    fixture = _definition().model_dump_json()
    gateway = _DraftGateway(fixture)
    service = AgentDraftService(gateway)
    drafted = asyncio.run(service.draft("Create a research agent"))
    assert drafted.agent_id == "research.agent"
    assert gateway.last_request is not None
    assert gateway.last_request.temperature == 0
    assert gateway.last_request.metadata["purpose"] == "agent_builder_draft"

    invalid = AgentDraftService(_DraftGateway('{"agent_id":"bad"}'))
    with pytest.raises(ValueError, match="invalid AgentDefinition"):
        asyncio.run(invalid.draft("Create a broken draft"))
