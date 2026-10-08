"""Plan 2 Section 1: authority-bearing references reject ambiguous Unicode."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from nika_core.builder.compiler import AgentCompiler
from nika_core.builder.spec import AgentDefinition


def _definition(**updates: object) -> AgentDefinition:
    fields: dict[str, object] = {
        "agent_id": "fixture.ref-admission",
        "name": "Reference admission",
        "goal": "Use only registered authority.",
        "instructions": "Do not choose ambiguous references.",
        "model_profile": "local",
    }
    fields.update(updates)
    return AgentDefinition(**fields)


@pytest.mark.parametrize(
    "field",
    ("model_profile", "schedule_id", "resource_budget_ref"),
)
@pytest.mark.parametrize(
    "suffix",
    ("\u2028", "\u2029", "\u202e", "\x00", "\u0301"),
)
def test_reference_identity_rejects_ambiguous_unicode_before_strip(
    field: str, suffix: str
) -> None:
    with pytest.raises(ValidationError, match="ambiguous Unicode"):
        _definition(**{field: "local" + suffix})


def test_ascii_spaces_remain_compatible_without_identity_change() -> None:
    definition = _definition(
        model_profile=" local ",
        schedule_id=" daily ",
        resource_budget_ref=" budget ",
    )
    assert definition.model_profile == "local"
    assert definition.schedule_id == "daily"
    assert definition.resource_budget_ref == "budget"


def test_model_copy_cannot_launder_ambiguous_registered_reference() -> None:
    definition = _definition()
    corrupted = definition.model_copy(update={"model_profile": "local\u2028"})
    compiler = AgentCompiler(tools=(), model_profiles={"local"})
    with pytest.raises(ValidationError, match="ambiguous Unicode"):
        compiler.compile(corrupted)
    assert compiler.compile(definition).definition.model_profile == "local"
