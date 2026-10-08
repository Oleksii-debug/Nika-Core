"""Plan 2 §1: an active marker cannot replace durable activation evidence."""

from __future__ import annotations

import pytest

from nika_core.builder.compiler import AgentCompiler
from nika_core.builder.repository import AgentDefinitionRepository
from nika_core.builder.spec import AgentDefinition, ToolGrant
from nika_core.data.sqlite import SQLiteStore
from nika_core.tools import ToolRisk, ToolSpec


@pytest.mark.parametrize(
    "corrupt_receipt",
    [
        None,
        "",
        "not-a-timestamp",
        "2026-10-08",
        "2026-10-08T12:00:00",
        "2026-10-08T12:00:00+02:00",
        "2026-10-08T12:00:00Z",
    ],
)
def test_active_without_activation_receipt_fails_closed_after_restart(
    tmp_path, corrupt_receipt: str | None
) -> None:
    path = tmp_path / "nika.db"
    store = SQLiteStore(path)
    store.initialize()
    definition = AgentDefinition(
        agent_id="fixture.receipt",
        name="Durable activation evidence",
        goal="Protect high-impact publication.",
        instructions="Require explicit approval before publishing.",
        model_profile="local",
        tool_grants=(ToolGrant(tool_id="release.publish", max_risk=4),),
    )
    compiler = AgentCompiler(
        tools=(ToolSpec("release.publish", "Publish", ToolRisk.HIGH_IMPACT),),
        model_profiles={"local"},
    )
    repository = AgentDefinitionRepository(store)
    repository.save_draft(compiler.compile(definition))
    repository.activate(definition, approved_tool_ids=frozenset({"release.publish"}))

    with store.connection() as conn:
        before = conn.execute(
            "SELECT activated_at FROM agent_definitions WHERE agent_id = ? AND version = ?",
            (definition.agent_id, definition.version),
        ).fetchone()
        assert before["activated_at"]

        # SQLite permits this legacy/corrupted row shape: status='active' does
        # not itself prove the effect was acknowledged and durably recorded.
        conn.execute(
            "UPDATE agent_definitions SET activated_at = ? "
            "WHERE agent_id = ? AND version = ?",
            (corrupt_receipt, definition.agent_id, definition.version),
        )

    restarted = AgentDefinitionRepository(SQLiteStore(path))
    for read in (
        lambda: restarted.get(definition.agent_id, definition.version),
        lambda: restarted.active(definition.agent_id),
        lambda: restarted.require_active(definition.agent_id, definition.version),
        lambda: restarted.activate(definition),
    ):
        with pytest.raises(ValueError, match="lacks valid activation evidence"):
            read()

    with store.connection() as conn:
        still_corrupted = conn.execute(
            "SELECT status, activated_at FROM agent_definitions "
            "WHERE agent_id = ? AND version = ?",
            (definition.agent_id, definition.version),
        ).fetchone()
    assert still_corrupted["status"] == "active"
    assert still_corrupted["activated_at"] == corrupt_receipt
