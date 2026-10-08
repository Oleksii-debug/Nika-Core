"""Plan 2 Section 1: behavioral AgentDefinition subclasses never enter authority reads."""

from __future__ import annotations

from dataclasses import replace

import pytest

from nika_core.builder.compiler import AgentCompiler
from nika_core.builder.repository import AgentDefinitionRepository
from nika_core.builder.spec import AgentDefinition, ToolGrant
from nika_core.data.sqlite import SQLiteStore
from nika_core.tools import ToolRisk, ToolSpec


def _definition() -> AgentDefinition:
    return AgentDefinition(
        agent_id="fixture.agent",
        name="Fixture",
        goal="Use an approved read-only tool",
        instructions="Read only.",
        model_profile="local",
        tool_grants=(ToolGrant(tool_id="web.read", max_risk=0),),
    )


def _compiler() -> AgentCompiler:
    return AgentCompiler(
        tools=(ToolSpec("web.read", "Read", ToolRisk.READ_ONLY),),
        model_profiles={"local"},
    )


class _BehavioralDefinition(AgentDefinition):
    def model_dump(self, *args, **kwargs):
        raise AssertionError("behavioral definition serializer was invoked")


def _behavioral(definition: AgentDefinition) -> AgentDefinition:
    return _BehavioralDefinition(**definition.model_dump(mode="python"))


def test_compiler_rejects_behavioral_definition_without_serializing() -> None:
    definition = _definition()
    with pytest.raises(TypeError, match="plain AgentDefinition"):
        _compiler().compile(_behavioral(definition))
    assert _compiler().compile(definition).definition == definition


def test_draft_rejects_behavioral_definition_before_sqlite_write(tmp_path) -> None:
    path = tmp_path / "nika.db"
    SQLiteStore(path).initialize()
    repository = AgentDefinitionRepository(SQLiteStore(path))
    definition = _definition()
    compiled = _compiler().compile(definition)
    forged = replace(compiled, definition=_behavioral(definition))
    with pytest.raises(TypeError, match="plain AgentDefinition"):
        repository.save_draft(forged)
    reopened = AgentDefinitionRepository(SQLiteStore(path))
    assert reopened.get(definition.agent_id, definition.version) is None
    assert reopened.next_version(definition.agent_id) == 1
    reopened.save_draft(compiled)
    assert reopened.get(definition.agent_id, definition.version) is not None


def test_activation_rejects_behavioral_definition_and_recovers(tmp_path) -> None:
    path = tmp_path / "nika.db"
    SQLiteStore(path).initialize()
    definition = _definition()
    repository = AgentDefinitionRepository(SQLiteStore(path))
    repository.save_draft(_compiler().compile(definition))
    restarted = AgentDefinitionRepository(SQLiteStore(path))
    with pytest.raises(TypeError, match="plain AgentDefinition"):
        restarted.activate(_behavioral(definition))
    assert restarted.active(definition.agent_id) is None
    assert restarted.get(definition.agent_id, definition.version).status == "draft"
    restarted.activate(definition)
    assert restarted.require_active(definition.agent_id, definition.version).status == "active"
