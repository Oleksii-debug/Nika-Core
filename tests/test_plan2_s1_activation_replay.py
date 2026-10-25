"""Plan 2 §1: acknowledged activation replay is durable, effect-safe and fail-closed."""

from __future__ import annotations

import pytest

from nika_core.builder.compiler import AgentCompiler
from nika_core.builder.repository import AgentDefinitionRepository
from nika_core.builder.spec import AgentDefinition, ToolGrant
from nika_core.data.sqlite import SQLiteStore
from nika_core.tools import ToolRisk, ToolSpec


def _definition() -> AgentDefinition:
    return AgentDefinition(
        agent_id="fixture.replay",
        name="Replay test",
        goal="High-impact work only after human approval.",
        instructions="Do not execute without approval.",
        model_profile="local",
        tool_grants=(ToolGrant(tool_id="release.publish", max_risk=4),),
    )


def test_lost_ack_replay_of_approved_activation_does_not_require_new_approval(tmp_path) -> None:
    path = tmp_path / "nika.db"
    store = SQLiteStore(path)
    store.initialize()
    definition = _definition()
    compiled = AgentCompiler(
        tools=(ToolSpec("release.publish", "Publish", ToolRisk.HIGH_IMPACT),),
        model_profiles={"local"},
    ).compile(definition)
    repository = AgentDefinitionRepository(store)
    repository.save_draft(compiled)

    # First admission MUST still require human approval and produce no active state.
    with pytest.raises(PermissionError, match="explicit human approval"):
        repository.activate(definition)
    assert repository.active(definition.agent_id) is None

    repository.activate(definition, approved_tool_ids=frozenset({"release.publish"}))
    with store.connection() as conn:
        first = conn.execute(
            "SELECT status, activated_at FROM agent_definitions "
            "WHERE agent_id = ? AND version = ?",
            (definition.agent_id, definition.version),
        ).fetchone()
        assert first["status"] == "active"
        first_timestamp = first["activated_at"]
        assert first_timestamp is not None

    # The authorized write committed but its acknowledgement was lost. A restart
    # and a request replay must not require a second approval or write again.
    restarted = AgentDefinitionRepository(SQLiteStore(path))
    restarted.activate(definition)
    with SQLiteStore(path).connection() as conn:
        after = conn.execute(
            "SELECT status, activated_at FROM agent_definitions "
            "WHERE agent_id = ? AND version = ?",
            (definition.agent_id, definition.version),
        ).fetchone()
    assert after["status"] == "active"
    assert after["activated_at"] == first_timestamp
    assert restarted.require_active(definition.agent_id, definition.version).definition == definition


def test_replay_never_launders_changed_definition_after_activation(tmp_path) -> None:
    path = tmp_path / "nika.db"
    store = SQLiteStore(path)
    store.initialize()
    definition = _definition()
    compiled = AgentCompiler(
        tools=(ToolSpec("release.publish", "Publish", ToolRisk.HIGH_IMPACT),),
        model_profiles={"local"},
    ).compile(definition)
    repository = AgentDefinitionRepository(store)
    repository.save_draft(compiled)
    repository.activate(definition, approved_tool_ids=frozenset({"release.publish"}))

    changed = definition.model_copy(update={"instructions": "Different instructions."})
    restarted = AgentDefinitionRepository(SQLiteStore(path))
    with pytest.raises(ValueError, match="persisted immutable draft"):
        restarted.activate(changed)
    assert restarted.require_active(definition.agent_id, definition.version).definition == definition
