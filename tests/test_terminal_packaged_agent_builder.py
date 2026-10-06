from __future__ import annotations

from pathlib import Path

import pytest

from nika_core.builder.repository import AgentDefinitionRepository
from nika_core.config import AppConfig
from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog
from nika_core.packaged_agent_builder import (
    PackagedAgentBuilderDraftHandler,
    PackagedAgentBuilderStateProjector,
)
from nika_core.product_command.contracts import CommandRouteKind
from nika_core.product_command.product_project_adapter import ProductProjectCommandService
from nika_core.product_command.routing import route_command
from nika_core.product_factory_packaged_journey import (
    PackagedProductCommandRouter,
    PackagedProductJourneyError,
)
from nika_core.product_project import ProductProjectRepository
from scripts import nika_windows


def _handler(
    path: Path,
) -> tuple[PackagedAgentBuilderDraftHandler, AgentDefinitionRepository, SQLiteStore]:
    store = SQLiteStore(path)
    store.initialize()
    repository = AgentDefinitionRepository(store)
    return PackagedAgentBuilderDraftHandler(repository), repository, store


def test_agent_builder_route_is_explicit_and_ambiguous_intent_fails_closed() -> None:
    assert (
        route_command("Create an agent that summarizes accessible documents").route
        is CommandRouteKind.AGENT_BUILDER
    )
    assert (
        route_command("Створи агента для аналізу доступних документів").route
        is CommandRouteKind.AGENT_BUILDER
    )
    assert (
        route_command("Create an application and configure an agent").route
        is CommandRouteKind.AMBIGUOUS
    )


def test_packaged_builder_persists_least_privilege_draft_and_projects_state(
    tmp_path: Path,
) -> None:
    handler, repository, store = _handler(tmp_path / "builder.db")
    command = "Create an agent that summarizes accessible documents"

    result = handler({"command": command})
    latest = repository.list_latest()

    assert result.status == "completed"
    assert result.focus_id == "agents-heading"
    assert len(latest) == 1
    stored = latest[0]
    assert stored.status == "draft"
    assert stored.definition.goal == command
    assert stored.definition.model_profile == "deterministic"
    assert stored.definition.tool_grants == ()
    assert stored.definition.schedule_id is None
    assert stored.definition.resource_budget_ref is None
    assert stored.required_human_approvals == ()
    assert stored.highest_risk == 0
    assert repository.active(stored.definition.agent_id) is None

    projected = PackagedAgentBuilderStateProjector(repository).decorate({"agents": []})
    assert len(projected["agent_builder_definitions"]) == 1
    item = projected["agent_builder_definitions"][0]
    assert set(item) == {
        "agent_id",
        "version",
        "name",
        "goal",
        "status",
        "highest_risk",
        "requires_human_approval",
    }
    assert item["requires_human_approval"] is False
    assert projected["agents"][0]["agent_id"] == stored.definition.agent_id

    events = AuditLog(store).list_for(
        entity_type="agent_definition",
        entity_id=f"{stored.definition.agent_id}:1",
    )
    assert [event.event_type for event in events] == ["agent_definition.draft_saved"]


def test_identical_builder_command_is_restart_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "restart.db"
    first, first_repository, _store = _handler(path)
    command = "Create an agent for accessible report triage"
    first({"command": command})
    first_id = first_repository.list_latest()[0].definition.agent_id

    second_store = SQLiteStore(path)
    second_store.initialize()
    second_repository = AgentDefinitionRepository(second_store)
    result = PackagedAgentBuilderDraftHandler(second_repository)({"command": command})

    assert result.status == "completed"
    assert "вже збережена без змін" in result.message
    latest = second_repository.list_latest()
    assert len(latest) == 1
    assert latest[0].definition.agent_id == first_id
    assert latest[0].definition.version == 1
    assert second_repository.next_version(first_id) == 2


@pytest.mark.parametrize("limit", (True, 0, 101))
def test_latest_definition_projection_is_bounded(tmp_path: Path, limit: object) -> None:
    _handler_instance, repository, _store = _handler(tmp_path / "bounded.db")

    with pytest.raises(ValueError, match="exact integer from 1 to 100"):
        repository.list_latest(limit=limit)  # type: ignore[arg-type]


class _HostileString(str):
    def split(self, *args: object, **kwargs: object) -> list[str]:
        del args, kwargs
        raise AssertionError("untrusted string method must not run")


class _OrdinaryStringSubclass(str):
    pass


@pytest.mark.parametrize(
    "command",
    (
        _HostileString("Create an agent for accessible report triage"),
        _OrdinaryStringSubclass("Create an agent for accessible report triage"),
    ),
)
def test_direct_builder_rejects_str_subclasses_before_methods_or_writes(
    tmp_path: Path,
    command: str,
) -> None:
    handler, _repository, store = _handler(tmp_path / "hostile.db")

    with pytest.raises(TypeError, match="має бути текстом"):
        handler({"command": command})

    with store.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM agent_definitions").fetchone()[0] == 0


def test_router_without_builder_handler_fails_closed_before_ordinary_task(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "no-handler.db")
    store.initialize()

    def ordinary_handler(_payload: object):
        raise AssertionError("Builder intent must not become an ordinary task")

    router = PackagedProductCommandRouter(
        products=ProductProjectCommandService(ProductProjectRepository(store)),
        ordinary_handler=ordinary_handler,
    )

    with pytest.raises(PackagedProductJourneyError, match="Agent Builder"):
        router.create({"command": "Create an agent for accessible report triage"})

    with store.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM product_projects").fetchone()[0] == 0



def test_builder_preserves_existing_product_project_selection(tmp_path: Path) -> None:
    database = (tmp_path / "builder with product.db").resolve()
    bridge, _products = nika_windows.build_windows_bridge(
        AppConfig(database_path=database),
        start_startup_recovery=False,
    )

    product_response = bridge.dispatch(
        {
            "request_id": "create-product-before-builder",
            "action_id": "task.create",
            "payload": {
                "command": "Create an accessible Windows application for expense tracking"
            },
        }
    )
    before = bridge.get_state()
    assert product_response["status"] == "completed"
    assert before["ok"] is True
    selected = before["state"]["product_project"]
    assert selected is not None
    selected_id = selected["project_id"]

    builder_response = bridge.dispatch(
        {
            "request_id": "create-builder-after-product",
            "action_id": "task.create",
            "payload": {"command": "Create an agent for accessible report triage"},
        }
    )
    after = bridge.get_state()

    assert builder_response["status"] == "completed"
    assert after["ok"] is True
    assert after["state"]["product_project"]["project_id"] == selected_id
    assert after["state"]["tasks"] == []
    assert len(after["state"]["agent_builder_definitions"]) == 1


def test_current_windows_bridge_routes_builder_without_creating_task_or_project(
    tmp_path: Path,
) -> None:
    database = (tmp_path / "windows builder.db").resolve()
    bridge, _products = nika_windows.build_windows_bridge(
        AppConfig(database_path=database),
        start_startup_recovery=False,
    )
    command = "Create an agent that summarizes accessible documents"

    response = bridge.dispatch(
        {
            "request_id": "builder-current-terminal",
            "action_id": "task.create",
            "payload": {"command": command},
        }
    )
    state_response = bridge.get_state()

    assert response["status"] == "completed"
    assert state_response["ok"] is True
    state = state_response["state"]
    assert state["tasks"] == []
    assert state["product_project"] is None
    assert len(state["agent_builder_definitions"]) == 1
    draft = state["agent_builder_definitions"][0]
    assert draft["status"] == "draft"
    assert draft["version"] == 1
    assert any(
        agent["agent_id"] == draft["agent_id"]
        and agent["name"].startswith("Agent Builder [чернетка]:")
        for agent in state["agents"]
    )

    store = SQLiteStore(database)
    store.initialize()
    with store.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM product_projects").fetchone()[0] == 0
