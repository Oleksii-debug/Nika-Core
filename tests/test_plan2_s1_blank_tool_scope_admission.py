"""Plan 2 Section 1: explicit blank scopes must not expand tool authority."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from nika_core.builder.compiler import AgentCompiler
from nika_core.builder.spec import AgentDefinition, ToolGrant
from nika_core.tools import ToolRisk, ToolSpec


@pytest.mark.parametrize(
    "scopes",
    (
        ("",),
        (" ",),
        ("valid", "  "),
        ("\t", "valid"),
    ),
)
def test_explicit_blank_scope_is_rejected_before_compilation(scopes: tuple[str, ...]) -> None:
    with pytest.raises(ValidationError, match="tool scope must not be blank"):
        ToolGrant(tool_id="web.read", max_risk=0, scopes=scopes)


def test_unscoped_and_normalized_scopes_remain_compatible() -> None:
    assert ToolGrant(tool_id="web.read").scopes == ()
    assert ToolGrant(
        tool_id="web.read", scopes=("  repo:read  ", "repo:read")
    ).scopes == ("repo:read",)


def test_blank_scope_from_untrusted_json_is_denied() -> None:
    payload = {
        "agent_id": "fixture.scope-boundary",
        "name": "Scope admission",
        "goal": "Read the authorized repository.",
        "instructions": "Do not expand scope.",
        "tool_grants": [
            {"tool_id": "web.read", "max_risk": 0, "scopes": ["repo:read", " "]}
        ],
    }
    with pytest.raises(ValidationError, match="tool scope must not be blank"):
        AgentDefinition.import_json(json.dumps(payload))


def test_compiler_readmission_rejects_a_bypassed_blank_scope() -> None:
    grant = ToolGrant(tool_id="web.read", scopes=("repo:read",))
    forged = grant.model_copy(update={"scopes": (" ",)})
    definition = AgentDefinition(
        agent_id="fixture.scope-bypass",
        name="Scope bypass",
        goal="Protect exact grants.",
        instructions="A copied model is not evidence of permission.",
        tool_grants=(grant,),
    ).model_copy(update={"tool_grants": (forged,)})
    compiler = AgentCompiler(
        tools=(ToolSpec("web.read", "Read", ToolRisk.READ_ONLY),),
        model_profiles={"default"},
    )
    with pytest.raises(ValidationError, match="tool scope must not be blank"):
        compiler.compile(definition)
