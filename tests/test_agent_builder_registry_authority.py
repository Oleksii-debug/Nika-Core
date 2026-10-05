"""Regression tests for Agent Builder's immutable registered-tool authority."""

from __future__ import annotations

import pytest

from nika_core.builder.compiler import AgentCompiler, RiskTier
from nika_core.builder.spec import AgentDefinition, ToolGrant
from nika_core.tools import ToolRisk, ToolSpec


def _compiler(*tools: ToolSpec, permissions: dict[str, set[str]] | None = None) -> AgentCompiler:
    return AgentCompiler(
        tools=tools,
        model_profiles={"local-default"},
        permission_catalog=permissions,
    )


def _definition(tool_id: str, risk: RiskTier, *, scopes: tuple[str, ...] = ()) -> AgentDefinition:
    return AgentDefinition(
        agent_id="test.agent",
        name="Test agent",
        goal="Verify registered tool classification",
        instructions="Use declared tools only.",
        model_profile="local-default",
        tool_grants=(ToolGrant(tool_id=tool_id, max_risk=risk, scopes=scopes),),
    )


@pytest.mark.parametrize(
    "second_risk",
    (ToolRisk.READ_ONLY, ToolRisk.HIGH_IMPACT),
)
def test_duplicate_registry_id_rejected_even_for_identical_risks(
    second_risk: ToolRisk,
) -> None:
    first = ToolSpec("web.read", "Read a page", ToolRisk.READ_ONLY)
    second = ToolSpec("web.read", "Another definition", second_risk)
    with pytest.raises(ValueError, match="duplicate registered tool identity"):
        _compiler(first, second)


def test_mutated_caller_risk_cannot_downgrade_compiler_authority() -> None:
    dangerous = ToolSpec("release.publish", "Publish a release", ToolRisk.HIGH_IMPACT)
    compiler = _compiler(dangerous)
    object.__setattr__(dangerous, "risk", ToolRisk.READ_ONLY)

    with pytest.raises(ValueError, match="requires R4_HIGH_IMPACT"):
        compiler.compile(_definition("release.publish", RiskTier.R0_READ_ONLY))
    result = compiler.compile(_definition("release.publish", RiskTier.R4_HIGH_IMPACT))
    assert result.required_human_approvals == ("release.publish",)
    assert result.highest_risk is RiskTier.R4_HIGH_IMPACT


def test_mutated_caller_id_cannot_remap_registered_identity() -> None:
    dangerous = ToolSpec("release.publish", "Publish a release", ToolRisk.HIGH_IMPACT)
    compiler = _compiler(dangerous)
    object.__setattr__(dangerous, "tool_id", "web.read")

    with pytest.raises(ValueError, match="unknown tool"):
        compiler.compile(_definition("web.read", RiskTier.R0_READ_ONLY))
    result = compiler.compile(_definition("release.publish", RiskTier.R4_HIGH_IMPACT))
    assert result.requires_human_approval


def test_valid_scope_catalog_still_compiles_without_escalation() -> None:
    read = ToolSpec("web.read", "Read a page", ToolRisk.READ_ONLY)
    compiler = _compiler(read, permissions={"web.read": {"network.read"}})
    result = compiler.compile(
        _definition("web.read", RiskTier.R0_READ_ONLY, scopes=("network.read",))
    )
    assert result.highest_risk is RiskTier.R0_READ_ONLY
    assert result.required_human_approvals == ()

    with pytest.raises(ValueError, match="unknown permission scope"):
        compiler.compile(
            _definition("web.read", RiskTier.R0_READ_ONLY, scopes=("network.write",))
        )
