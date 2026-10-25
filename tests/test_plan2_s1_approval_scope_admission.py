"""Plan 2 Section 1: activation approvals cannot exceed the reviewed definition."""

from __future__ import annotations

import pytest

from nika_core.builder.compiler import AgentCompiler
from nika_core.builder.repository import AgentDefinitionRepository
from nika_core.builder.spec import AgentDefinition, ToolGrant
from nika_core.data.sqlite import SQLiteStore
from nika_core.tools import ToolRisk, ToolSpec


def _definition(*, high_impact: bool) -> AgentDefinition:
    return AgentDefinition(
        agent_id="fixture.approval-scope",
        name="Exact approval scope",
        goal="Use only reviewed tools.",
        instructions="Respect exact tool grant boundaries.",
        model_profile="local",
        tool_grants=(
            ToolGrant(
                tool_id="release.publish" if high_impact else "web.read",
                max_risk=4 if high_impact else 0,
            ),
        ),
    )


@pytest.mark.parametrize("high_impact", (False, True))
def test_extra_approval_cannot_activate_unreviewed_tool_or_consume_draft(
    tmp_path, high_impact: bool,
) -> None:
    path = tmp_path / "agent.db"
    store = SQLiteStore(path)
    store.initialize()
    definition = _definition(high_impact=high_impact)
    tool = ToolSpec(
        "release.publish" if high_impact else "web.read",
        "Reviewed tool",
        ToolRisk.HIGH_IMPACT if high_impact else ToolRisk.READ_ONLY,
    )
    repository = AgentDefinitionRepository(store)
    repository.save_draft(
        AgentCompiler(tools=(tool,), model_profiles={"local"}).compile(definition)
    )
    authorized = {"release.publish"} if high_impact else set()
    # A caller cannot make an irrelevant tool look human-approved in the audit.
    with pytest.raises(PermissionError, match="unrequested high-impact tool approvals"):
        repository.activate(
            definition,
            approved_tool_ids=frozenset(authorized | {"unreviewed.tool"}),
        )
    with store.connection() as conn:
        row = conn.execute(
            "SELECT status, activated_at FROM agent_definitions "
            "WHERE agent_id = ? AND version = ?",
            (definition.agent_id, definition.version),
        ).fetchone()
        assert row["status"] == "draft"
        assert row["activated_at"] is None

    restarted = AgentDefinitionRepository(SQLiteStore(path))
    assert restarted.active(definition.agent_id) is None
    # The rejected attempt did not spend the immutable draft or prevent recovery.
    restarted.activate(definition, approved_tool_ids=frozenset(authorized))
    assert restarted.require_active(definition.agent_id, definition.version).status == "active"


def test_overbroad_approval_is_rejected_even_on_committed_lost_ack_replay(tmp_path) -> None:
    path = tmp_path / "agent.db"
    store = SQLiteStore(path)
    store.initialize()
    definition = _definition(high_impact=True)
    repository = AgentDefinitionRepository(store)
    repository.save_draft(
        AgentCompiler(
            tools=(ToolSpec("release.publish", "Publish", ToolRisk.HIGH_IMPACT),),
            model_profiles={"local"},
        ).compile(definition)
    )
    repository.activate(definition, approved_tool_ids=frozenset({"release.publish"}))
    with store.connection() as conn:
        original = conn.execute(
            "SELECT activated_at FROM agent_definitions WHERE agent_id = ? AND version = ?",
            (definition.agent_id, definition.version),
        ).fetchone()["activated_at"]

    restarted = AgentDefinitionRepository(SQLiteStore(path))
    with pytest.raises(PermissionError, match="unrequested high-impact tool approvals"):
        restarted.activate(definition, approved_tool_ids=frozenset({"another.publish"}))
    # No second approval is needed when the prior authorized activation committed.
    restarted.activate(definition)
    with store.connection() as conn:
        after = conn.execute(
            "SELECT activated_at FROM agent_definitions WHERE agent_id = ? AND version = ?",
            (definition.agent_id, definition.version),
        ).fetchone()["activated_at"]
    assert after == original

@pytest.mark.parametrize("damage", ("oversized", "deeply_nested"))
def test_damaged_approval_evidence_fails_closed_after_sqlite_restart(
    tmp_path, damage: str,
) -> None:
    """A corrupt durable row cannot authorize activation or crash JSON admission."""
    path = tmp_path / "damaged-approval.db"
    store = SQLiteStore(path)
    store.initialize()
    definition = _definition(high_impact=True)
    repository = AgentDefinitionRepository(store)
    repository.save_draft(
        AgentCompiler(
            tools=(ToolSpec("release.publish", "Publish", ToolRisk.HIGH_IMPACT),),
            model_profiles={"local"},
        ).compile(definition)
    )
    corrupted = (
        '["release.publish"]' + " " * 1_048_577
        if damage == "oversized"
        else "[" * 1200 + '"release.publish"' + "]" * 1200
    )
    with store.connection() as conn:
        conn.execute(
            "UPDATE agent_definitions SET required_approvals_json = ? "
            "WHERE agent_id = ? AND version = ?",
            (corrupted, definition.agent_id, definition.version),
        )

    restarted = AgentDefinitionRepository(SQLiteStore(path))
    with pytest.raises(ValueError, match="persisted agent risk/approval evidence is invalid"):
        restarted.get(definition.agent_id, definition.version)
    with pytest.raises(ValueError, match="persisted agent risk/approval evidence is invalid"):
        restarted.activate(
            definition, approved_tool_ids=frozenset({"release.publish"})
        )
    with store.connection() as conn:
        row = conn.execute(
            "SELECT status, activated_at, required_approvals_json "
            "FROM agent_definitions WHERE agent_id = ? AND version = ?",
            (definition.agent_id, definition.version),
        ).fetchone()
    assert row["status"] == "draft"
    assert row["activated_at"] is None
    assert row["required_approvals_json"] == corrupted
