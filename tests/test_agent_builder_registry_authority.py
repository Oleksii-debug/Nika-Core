"""Regression tests for Agent Builder's immutable registered-tool authority."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

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


@pytest.mark.parametrize("untrusted_risk", (True, False, 1.0, "1"))
def test_untrusted_numeric_coercion_cannot_select_agent_tool_risk(untrusted_risk: object) -> None:
    with pytest.raises(ValidationError):
        ToolGrant(tool_id="web.read", max_risk=untrusted_risk)  # type: ignore[arg-type]
    assert ToolGrant(tool_id="web.read", max_risk=RiskTier.R0_READ_ONLY).max_risk == 0


def test_compilation_detaches_nested_caller_grants_before_draft_persistence() -> None:
    source = _definition("web.read", RiskTier.R0_READ_ONLY)
    compiled = _compiler(ToolSpec("web.read", "Read", ToolRisk.READ_ONLY)).compile(source)
    object.__setattr__(source.tool_grants[0], "max_risk", RiskTier.R4_HIGH_IMPACT)
    object.__setattr__(source.tool_grants[0], "tool_id", "release.publish")

    assert compiled.definition.tool_grants[0].tool_id == "web.read"
    assert compiled.definition.tool_grants[0].max_risk == RiskTier.R0_READ_ONLY
    assert compiled.required_human_approvals == ()
    assert compiled.highest_risk is RiskTier.R0_READ_ONLY

@pytest.mark.parametrize(
    ("field", "untrusted"),
    (
        ("format_version", True),
        ("format_version", 1.0),
        ("format_version", "1"),
        ("version", True),
        ("version", False),
        ("version", 1.0),
        ("version", "1"),
        ("max_steps", True),
        ("max_steps", False),
        ("max_steps", 2.0),
        ("max_steps", "2"),
    ),
)
def test_agent_document_rejects_ambiguous_version_or_budget(
    field: str, untrusted: object,
) -> None:
    source = _definition("web.read", RiskTier.R0_READ_ONLY)
    fields = source.model_dump()
    fields[field] = untrusted
    with pytest.raises(ValidationError):
        AgentDefinition.model_validate(fields)


@pytest.mark.parametrize(
    ("field", "encoded"),
    (
        ("format_version", "true"),
        ("format_version", "1.0"),
        ("version", "true"),
        ("version", "1.0"),
        ("version", '"1"'),
        ("max_steps", "false"),
        ("max_steps", "2.0"),
        ("max_steps", '"2"'),
    ),
)
def test_imported_agent_document_rejects_ambiguous_version_or_budget(
    field: str, encoded: str,
) -> None:
    import json

    source = _definition("web.read", RiskTier.R0_READ_ONLY)
    fields = source.model_dump()
    fields[field] = None
    raw = json.dumps(fields, ensure_ascii=False)
    raw = raw.replace('"' + field + '": null', '"' + field + '": ' + encoded)
    with pytest.raises(ValidationError):
        AgentDefinition.import_json(raw)


@pytest.mark.parametrize(
    ("version", "max_steps"),
    ((1, 1), (2, 100_000), (1, 100)),
)
def test_agent_document_preserves_exact_integer_versions_and_budget(
    version: int, max_steps: int,
) -> None:
    source = _definition("web.read", RiskTier.R0_READ_ONLY)
    payload = source.model_dump()
    payload.update(format_version=1, version=version, max_steps=max_steps)
    agent = AgentDefinition.model_validate(payload)
    restored = AgentDefinition.import_json(agent.export_json())
    assert restored.format_version == 1
    assert type(restored.version) is int and restored.version == version
    assert type(restored.max_steps) is int and restored.max_steps == max_steps


@pytest.mark.parametrize(
    ("field", "value"),
    (("format_version", 2), ("version", 0), ("max_steps", 0), ("max_steps", 100_001)),
)
def test_agent_document_retains_version_and_budget_bounds(field: str, value: int) -> None:
    payload = _definition("web.read", RiskTier.R0_READ_ONLY).model_dump()
    payload[field] = value
    with pytest.raises(ValidationError):
        AgentDefinition.model_validate(payload)
