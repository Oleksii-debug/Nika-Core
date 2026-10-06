from __future__ import annotations

from pathlib import Path

import pytest

from nika_core.builder.compiler import AgentCompiler
from nika_core.builder.repository import AgentDefinitionRepository
from nika_core.builder.spec import AgentDefinition
from nika_core.data.sqlite import SQLiteStore


def _definition(*, version: int = 1, enabled: bool = True) -> AgentDefinition:
    return AgentDefinition(
        agent_id="generic.worker",
        version=version,
        name="Generic worker",
        goal="Complete a bounded generic task.",
        instructions="Use only activated capabilities.",
        model_profile="test",
        enabled=enabled,
    )


def _repository(tmp_path: Path) -> tuple[AgentDefinitionRepository, AgentCompiler]:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    return AgentDefinitionRepository(store), AgentCompiler(tools=(), model_profiles={"test"})


def test_disabled_definition_cannot_be_activated(tmp_path: Path) -> None:
    repository, compiler = _repository(tmp_path)
    definition = _definition(enabled=False)
    repository.save_draft(compiler.compile(definition))

    with pytest.raises(ValueError, match="disabled agent definition cannot be activated"):
        repository.activate(definition)

    stored = repository.get(definition.agent_id, definition.version)
    assert stored is not None
    assert stored.status == "draft"
    assert repository.active(definition.agent_id) is None


def test_require_active_rejects_retired_version(tmp_path: Path) -> None:
    repository, compiler = _repository(tmp_path)
    first = _definition(version=1)
    repository.save_draft(compiler.compile(first))
    repository.activate(first)
    assert repository.require_active(first.agent_id, 1).definition == first

    second = _definition(version=2)
    repository.save_draft(compiler.compile(second))
    repository.activate(second)

    with pytest.raises(PermissionError, match="not active"):
        repository.require_active(first.agent_id, 1)
    assert repository.require_active(second.agent_id, 2).definition == second


def test_require_active_rejects_unknown_definition(tmp_path: Path) -> None:
    repository, _compiler = _repository(tmp_path)

    with pytest.raises(KeyError, match="unknown agent definition"):
        repository.require_active("missing.agent", 1)
