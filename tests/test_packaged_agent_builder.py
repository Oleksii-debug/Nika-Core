from __future__ import annotations

from pathlib import Path

import pytest

from nika_core.builder.repository import AgentDefinitionRepository
from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog
from nika_core.packaged_agent_builder import (
    PackagedAgentBuilderDraftHandler,
    PackagedAgentBuilderStateProjector,
)


def _handler(
    path: Path,
) -> tuple[PackagedAgentBuilderDraftHandler, AgentDefinitionRepository, SQLiteStore]:
    store = SQLiteStore(path)
    store.initialize()
    repository = AgentDefinitionRepository(store)
    return PackagedAgentBuilderDraftHandler(repository), repository, store


def _only_agent_id(store: SQLiteStore) -> str:
    with store.connection() as conn:
        rows = conn.execute(
            "SELECT agent_id FROM agent_definitions ORDER BY agent_id, version"
        ).fetchall()
    assert len(rows) == 1
    return str(rows[0]["agent_id"])


def test_packaged_handler_persists_safe_review_only_draft(tmp_path: Path) -> None:
    handler, repository, store = _handler(tmp_path / "agent builder.db")
    command = "Створи агента для аналізу доступних документів"

    result = handler({"command": command})

    agent_id = _only_agent_id(store)
    stored = repository.get(agent_id, 1)
    assert stored is not None
    assert stored.status == "draft"
    assert stored.definition.goal == command
    assert stored.definition.model_profile == "deterministic"
    assert stored.definition.tool_grants == ()
    assert stored.definition.schedule_id is None
    assert stored.definition.resource_budget_ref is None
    assert stored.required_human_approvals == ()
    assert stored.highest_risk == 0
    assert repository.active(agent_id) is None

    assert result.status == "completed"
    assert result.focus_id == "agents-heading"
    assert agent_id in result.message
    assert "версія 1" in result.message
    assert "не активована" in result.message

    events = AuditLog(store).list_for(
        entity_type="agent_definition",
        entity_id=f"{agent_id}:1",
    )
    assert [event.event_type for event in events] == ["agent_definition.draft_saved"]
    assert events[0].payload == {
        "agent_id": agent_id,
        "highest_risk": 0,
        "required_approvals": [],
        "version": 1,
    }


def test_identical_command_is_restart_idempotent_while_draft_is_unchanged(
    tmp_path: Path,
) -> None:
    path = tmp_path / "restart idempotent.db"
    first, _repository, first_store = _handler(path)
    command = "Create an agent that summarizes accessible documents"
    first_result = first({"command": command})
    agent_id = _only_agent_id(first_store)

    second_store = SQLiteStore(path)
    second_store.initialize()
    second_repository = AgentDefinitionRepository(second_store)
    second = PackagedAgentBuilderDraftHandler(second_repository)

    second_result = second({"command": command})

    assert first_result.status == second_result.status == "completed"
    assert agent_id in second_result.message
    assert "вже збережена без змін" in second_result.message
    assert second_repository.next_version(agent_id) == 2
    with second_store.connection() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM agent_definitions WHERE agent_id = ?",
            (agent_id,),
        ).fetchone()[0]
    assert count == 1


@pytest.mark.parametrize(
    "command",
    (
        "",
        "   ",
        "Create an agent\x00with a hidden control",
        "Create an agent " + ("x" * 4000),
    ),
)
def test_packaged_handler_rejects_invalid_text_without_persisting(
    tmp_path: Path,
    command: str,
) -> None:
    handler, _repository, store = _handler(tmp_path / "invalid.db")

    with pytest.raises(ValueError):
        handler({"command": command})

    with store.connection() as conn:
        count = conn.execute("SELECT COUNT(*) FROM agent_definitions").fetchone()[0]
    assert count == 0


def test_distinct_explicit_goals_get_distinct_draft_identities(tmp_path: Path) -> None:
    handler, _repository, store = _handler(tmp_path / "distinct.db")

    first = handler({"command": "Create an agent for report triage"})
    second = handler({"command": "Create an agent for invoice triage"})

    with store.connection() as conn:
        rows = conn.execute(
            "SELECT agent_id, version, status FROM agent_definitions ORDER BY agent_id"
        ).fetchall()
    assert len(rows) == 2
    assert {str(row["status"]) for row in rows} == {"draft"}
    assert {int(row["version"]) for row in rows} == {1}
    assert first.message != second.message


def test_state_projector_exposes_bounded_review_state_without_authority_fields(
    tmp_path: Path,
) -> None:
    handler, repository, _store = _handler(tmp_path / "projected.db")
    handler({"command": "Create an agent for accessible report triage"})
    projector = PackagedAgentBuilderStateProjector(repository)

    state = projector.decorate(
        {
            "agents": [
                {
                    "agent_id": "nika.default",
                    "version": 1,
                    "name": "Nika",
                    "goal": "Default packaged agent",
                }
            ]
        }
    )

    assert len(state["agent_builder_definitions"]) == 1
    projected = state["agent_builder_definitions"][0]
    assert projected["status"] == "draft"
    assert projected["version"] == 1
    assert projected["highest_risk"] == 0
    assert projected["requires_human_approval"] is False
    assert set(projected) == {
        "agent_id",
        "version",
        "name",
        "goal",
        "status",
        "highest_risk",
        "requires_human_approval",
    }
    assert len(state["agents"]) == 2
    assert state["agents"][1]["agent_id"] == projected["agent_id"]
    assert state["agents"][1]["name"].startswith("Agent Builder [чернетка]:")
    assert repository.active(projected["agent_id"]) is None


def test_repository_latest_view_is_bounded_and_integrity_validated(tmp_path: Path) -> None:
    handler, repository, _store = _handler(tmp_path / "latest.db")
    handler({"command": "Create an agent for first review"})
    handler({"command": "Create an agent for second review"})

    latest = repository.list_latest(limit=1)

    assert len(latest) == 1
    assert latest[0].status == "draft"
    with pytest.raises(ValueError, match="exact integer"):
        repository.list_latest(limit=True)
    with pytest.raises(ValueError, match="1 to 100"):
        repository.list_latest(limit=101)


class _PersistThenConflictRepository(AgentDefinitionRepository):
    def __init__(self, store: SQLiteStore) -> None:
        super().__init__(store)
        self._conflict_once = True

    def save_draft(self, compilation) -> None:
        if self._conflict_once:
            self._conflict_once = False
            super().save_draft(compilation)
            raise ValueError("simulated concurrent version observation")
        super().save_draft(compilation)


def test_identical_concurrent_persistence_conflict_recovers_as_idempotent(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "concurrent.db")
    store.initialize()
    repository = _PersistThenConflictRepository(store)
    handler = PackagedAgentBuilderDraftHandler(repository)

    result = handler({"command": "Create an agent for concurrent report triage"})

    assert result.status == "completed"
    assert "вже збережена без змін" in result.message
    agent_id = _only_agent_id(store)
    assert repository.next_version(agent_id) == 2
    assert repository.active(agent_id) is None
