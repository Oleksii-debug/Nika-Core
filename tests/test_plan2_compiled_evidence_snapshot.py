"""Plan 2 Section 1: compiled authorization evidence must be an inert snapshot."""

from __future__ import annotations

import pytest

import nika_core.builder.repository as builder_repository
from nika_core.builder.compiler import AgentCompiler, CompilationResult, RiskTier
from nika_core.builder.repository import AgentDefinitionRepository
from nika_core.builder.spec import AgentDefinition, ToolGrant
from nika_core.data.sqlite import SQLiteStore
from nika_core.tools import ToolRisk, ToolSpec


def _compiled() -> tuple[AgentDefinition, CompilationResult]:
    definition = AgentDefinition(
        agent_id="fixture.agent",
        name="Fixture",
        goal="Require explicit human approval",
        instructions="Use only the approved high-impact tool.",
        model_profile="local",
        tool_grants=(ToolGrant(tool_id="release.publish", max_risk=4),),
    )
    compiler = AgentCompiler(
        tools=(ToolSpec("release.publish", "Publish", ToolRisk.HIGH_IMPACT),),
        model_profiles={"local"},
    )
    return definition, compiler.compile(definition)


def test_compiled_evidence_is_not_reread_after_validation(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "nika.db"
    SQLiteStore(path).initialize()
    repository = AgentDefinitionRepository(SQLiteStore(path))
    definition, compiled = _compiled()
    original_clock = builder_repository.datetime

    class MutatingClock:
        @staticmethod
        def now(timezone):
            # A shallow-frozen dataclass can still be mutated by a concurrent
            # caller after repository validation but before the SQLite INSERT.
            object.__setattr__(compiled, "highest_risk", RiskTier.R0_READ_ONLY)
            object.__setattr__(compiled, "required_human_approvals", ())
            return original_clock.now(timezone)

    monkeypatch.setattr(builder_repository, "datetime", MutatingClock)
    repository.save_draft(compiled)
    monkeypatch.setattr(builder_repository, "datetime", original_clock)

    restarted = AgentDefinitionRepository(SQLiteStore(path))
    stored = restarted.get(definition.agent_id, definition.version)
    assert stored is not None
    assert stored.status == "draft"
    assert stored.highest_risk == 4
    assert stored.required_human_approvals == ("release.publish",)
    with pytest.raises(PermissionError, match="explicit human approval"):
        restarted.activate(definition)
    assert restarted.active(definition.agent_id) is None
    restarted.activate(
        definition, approved_tool_ids=frozenset({"release.publish"})
    )
    assert restarted.require_active(definition.agent_id, definition.version).status == "active"


def test_compilation_subclass_cannot_run_behavioral_accessors(tmp_path) -> None:
    path = tmp_path / "nika.db"
    SQLiteStore(path).initialize()
    repository = AgentDefinitionRepository(SQLiteStore(path))
    definition, compiled = _compiled()

    class BehavioralCompilation(CompilationResult):
        def __getattribute__(self, key: str):
            raise AssertionError(f"behavioral compiled evidence accessed: {key}")

    forged = BehavioralCompilation(
        definition=compiled.definition,
        required_human_approvals=compiled.required_human_approvals,
        highest_risk=compiled.highest_risk,
    )
    with pytest.raises(TypeError, match="plain CompilationResult"):
        repository.save_draft(forged)

    restarted = AgentDefinitionRepository(SQLiteStore(path))
    assert restarted.get(definition.agent_id, definition.version) is None
    assert restarted.next_version(definition.agent_id) == 1
    # Inert canonical compiler output still follows the normal durable path.
    restarted.save_draft(compiled)
    assert restarted.get(definition.agent_id, definition.version) is not None
