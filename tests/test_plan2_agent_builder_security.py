"""Plan 2 / Section 1: reuse canonical Agent Builder with fail-closed authority."""

from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from nika_core.builder.compiler import AgentCompiler, RiskTier
from nika_core.builder.drafting import AgentDraftService
from nika_core.builder.spec import AgentDefinition, ToolGrant
from nika_core.tools import ToolRisk, ToolSpec


def _definition() -> AgentDefinition:
    return AgentDefinition(
        agent_id="fixture.agent",
        name="Fixture",
        goal="Safe research",
        instructions="Use approved grants only.",
        model_profile="local",
        tool_grants=(ToolGrant(tool_id="web.read", max_risk=0),),
    )


def _compiler(*tools: ToolSpec) -> AgentCompiler:
    return AgentCompiler(tools=tools, model_profiles={"local"})


@pytest.mark.parametrize("second", (ToolRisk.READ_ONLY, ToolRisk.HIGH_IMPACT))
def test_registry_duplicate_id_fails_closed(second: ToolRisk) -> None:
    with pytest.raises(ValueError, match="duplicate registered tool identity"):
        _compiler(
            ToolSpec("web.read", "First", ToolRisk.READ_ONLY),
            ToolSpec("web.read", "Second", second),
        )


def test_compiler_risk_and_nested_definition_are_snapshot_at_review() -> None:
    dangerous = ToolSpec("release.publish", "Publish", ToolRisk.HIGH_IMPACT)
    compiler = _compiler(dangerous)
    object.__setattr__(dangerous, "risk", ToolRisk.READ_ONLY)
    requested = _definition().model_copy(update={
        "tool_grants": (ToolGrant(tool_id="release.publish", max_risk=4),)
    })
    compiled = compiler.compile(requested)
    assert compiled.highest_risk is RiskTier.R4_HIGH_IMPACT
    assert compiled.required_human_approvals == ("release.publish",)
    object.__setattr__(requested.tool_grants[0], "max_risk", 0)
    assert compiled.definition.tool_grants[0].max_risk == 4
    with pytest.raises(ValueError, match="requires R4_HIGH_IMPACT"):
        compiler.compile(_definition().model_copy(update={
            "tool_grants": (ToolGrant(tool_id="release.publish", max_risk=0),)
        }))


@pytest.mark.parametrize("untrusted", (True, False, 1.0, "1"))
def test_grant_risk_cannot_be_coerced(untrusted: object) -> None:
    with pytest.raises(ValidationError):
        ToolGrant(tool_id="web.read", max_risk=untrusted)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("format_version", True),
        ("format_version", 1.0),
        ("version", True),
        ("version", "1"),
        ("max_steps", 1.0),
        ("max_steps", "2"),
        ("enabled", 1),
        ("enabled", "true"),
    ),
)
def test_agent_identity_budget_and_enabled_are_strict(field: str, value: object) -> None:
    payload = _definition().model_dump()
    payload[field] = value
    with pytest.raises(ValidationError):
        AgentDefinition.model_validate(payload)


def test_import_rejects_ambiguous_keys_and_oversize_without_authorization() -> None:
    raw = _definition().export_json()
    ambiguous = raw.replace('"max_risk": 0', '"max_risk": 0, "max_risk": 4')
    with pytest.raises(ValueError, match="duplicate JSON object key"):
        AgentDefinition.import_json(ambiguous)
    with pytest.raises(ValueError, match="size limit"):
        AgentDefinition.import_json(raw + " " * (1024 * 1024))
    assert AgentDefinition.import_json(raw) == _definition()


class _UnsafeGateway:
    async def complete(self, request):
        class _Result:
            text = _definition().export_json().replace(
                '"max_risk": 0', '"max_risk": 0, "max_risk": 4'
            )
        return _Result()


def test_model_draft_cannot_launder_duplicate_permission_keys() -> None:
    with pytest.raises(ValueError, match="duplicate JSON object key"):
        asyncio.run(AgentDraftService(_UnsafeGateway()).draft("Create safe agent"))
