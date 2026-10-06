from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from nika_core.builder.repository import AgentDefinitionRepository
from nika_core.config import AppConfig
from nika_core.data.sqlite import SQLiteStore
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
from nika_core.ui.bridge_models import UIResult
from scripts import nika_windows


class _RecordingHandler:
    def __init__(self, message: str) -> None:
        self.message = message
        self.calls: list[Mapping[str, Any]] = []

    def __call__(self, payload: Mapping[str, Any]) -> UIResult:
        self.calls.append(payload)
        return UIResult(
            request_id="desktop-handler",
            status="completed",
            message=self.message,
            focus_id="tasks-heading",
        )


def _builder(
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


@pytest.mark.parametrize(
    "command",
    (
        "Create an agent that summarizes accessible documents",
        "Configure an assistant for deterministic report triage",
        "Створи агента для аналізу доступних документів",
        "Налаштуй асистента для сортування звітів",
    ),
)
def test_explicit_agent_creation_routes_to_builder(command: str) -> None:
    decision = route_command(command)

    assert decision.route is CommandRouteKind.AGENT_BUILDER
    assert decision.requires_user_decision is False
    assert decision.project_id is None
    assert decision.normalized_goal == command


@pytest.mark.parametrize(
    "command",
    (
        "Run the existing agent and summarize its latest task log",
        "Make the existing assistant summarize the latest report",
    ),
)
def test_existing_agent_work_remains_ordinary(command: str) -> None:
    assert route_command(command).route is CommandRouteKind.AGENT_TASK


@pytest.mark.parametrize(
    "command",
    (
        "Create an agent with a missing plugin capability",
        "Build an agent application for accessible research",
        "Створи агента і додай потрібний плагін",
    ),
)
def test_mixed_specialized_agent_intent_is_ambiguous(command: str) -> None:
    decision = route_command(command)

    assert decision.route is CommandRouteKind.AMBIGUOUS
    assert decision.requires_user_decision is True


class _HostileCommand(str):
    def split(self, *args, **kwargs):
        del args, kwargs
        raise AssertionError("hostile split must not run")


def test_builder_handler_rejects_noncanonical_text_before_string_behavior(
    tmp_path: Path,
) -> None:
    handler, _repository, _store = _builder(tmp_path / "hostile.db")

    with pytest.raises(ValueError, match="звичайним текстом"):
        handler({"command": _HostileCommand("Create an agent")})


def test_handler_persists_review_only_restart_idempotent_draft(tmp_path: Path) -> None:
    path = tmp_path / "agent builder.db"
    command = "Створи агента для аналізу доступних документів"
    first, first_repository, first_store = _builder(path)

    result = first({"command": command})
    agent_id = _only_agent_id(first_store)
    stored = first_repository.get(agent_id, 1)

    assert result.status == "completed"
    assert result.focus_id == "agents-heading"
    assert stored is not None
    assert stored.status == "draft"
    assert stored.definition.goal == command
    assert stored.definition.model_profile == "deterministic"
    assert stored.definition.tool_grants == ()
    assert stored.definition.schedule_id is None
    assert stored.definition.resource_budget_ref is None
    assert stored.required_human_approvals == ()
    assert stored.highest_risk == 0
    assert first_repository.active(agent_id) is None

    second, second_repository, _second_store = _builder(path)
    replay = second({"command": command})

    assert replay.status == "completed"
    assert "вже збережена без змін" in replay.message
    assert second_repository.next_version(agent_id) == 2
    assert second_repository.active(agent_id) is None


def test_state_projection_is_bounded_and_integrity_validated(tmp_path: Path) -> None:
    handler, repository, _store = _builder(tmp_path / "projection.db")
    handler({"command": "Create an agent for accessible report triage"})
    projector = PackagedAgentBuilderStateProjector(repository)

    state = projector.decorate({"agents": []})

    projected = state["agent_builder_definitions"]
    assert len(projected) == 1
    assert set(projected[0]) == {
        "agent_id",
        "version",
        "name",
        "goal",
        "status",
        "highest_risk",
        "requires_human_approval",
    }
    assert projected[0]["status"] == "draft"
    assert projected[0]["highest_risk"] == 0
    assert projected[0]["requires_human_approval"] is False
    assert len(state["agents"]) == 1
    assert state["agents"][0]["name"].startswith("Agent Builder [чернетка]:")
    with pytest.raises(ValueError, match="exact integer"):
        repository.list_latest(limit=True)
    with pytest.raises(ValueError, match="1 to 100"):
        repository.list_latest(limit=101)


def test_packaged_router_delegates_builder_without_task_or_selection_leak(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "router.db")
    store.initialize()
    products = ProductProjectCommandService(ProductProjectRepository(store))
    ordinary = _RecordingHandler("ordinary")
    builder = _RecordingHandler("builder")
    router = PackagedProductCommandRouter(
        products=products,
        ordinary_handler=ordinary,
        agent_builder_handler=builder,
    )
    payload = {"command": "Створи агента для аналізу документів"}

    result = router.create(payload)

    assert result.message == "builder"
    assert builder.calls == [payload]
    assert ordinary.calls == []
    assert router.active_project_id is None

    unavailable = PackagedProductCommandRouter(
        products=products,
        ordinary_handler=ordinary,
        agent_builder_handler=None,
    )
    with pytest.raises(PackagedProductJourneyError, match="Agent Builder"):
        unavailable.create(payload)
    assert ordinary.calls == []


def test_windows_composition_creates_and_replays_builder_draft_without_task(
    tmp_path: Path,
) -> None:
    path = (tmp_path / "F10 packaged українська path.db").resolve()
    config = AppConfig(database_path=path)
    command = "Створи агента для аналізу доступних документів"

    first, _products = nika_windows.build_windows_bridge(
        config,
        start_startup_recovery=False,
    )
    before = first.get_state()
    assert before["ok"] is True
    assert before["state"]["tasks"] == []

    created = first.dispatch(
        {
            "request_id": "builder-first",
            "action_id": "task.create",
            "payload": {"command": command},
        }
    )
    assert created["status"] == "completed"
    assert created["focus_id"] == "agents-heading"
    first_state = first.get_state()
    assert first_state["ok"] is True
    state = first_state["state"]
    assert state["tasks"] == []
    assert state["product_project"] is None
    assert len(state["agent_builder_definitions"]) == 1
    definition = state["agent_builder_definitions"][0]
    assert definition["status"] == "draft"
    assert definition["requires_human_approval"] is False

    reopened, _reopened_products = nika_windows.build_windows_bridge(
        config,
        start_startup_recovery=False,
    )
    recovered = reopened.get_state()
    assert recovered["ok"] is True
    assert recovered["state"]["agent_builder_definitions"] == [definition]

    replay = reopened.dispatch(
        {
            "request_id": "builder-replay",
            "action_id": "task.create",
            "payload": {"command": command},
        }
    )
    assert replay["status"] == "completed"
    assert "вже збережена без змін" in replay["message"]
    replayed = reopened.get_state()["state"]
    assert replayed["tasks"] == []
    assert replayed["agent_builder_definitions"] == [definition]


def test_ambiguous_agent_command_invokes_no_handler(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "ambiguous.db")
    store.initialize()
    products = ProductProjectCommandService(ProductProjectRepository(store))
    ordinary = _RecordingHandler("ordinary")
    builder = _RecordingHandler("builder")
    router = PackagedProductCommandRouter(
        products=products,
        ordinary_handler=ordinary,
        agent_builder_handler=builder,
    )

    with pytest.raises(PackagedProductJourneyError, match="одночасно"):
        router.create({"command": "Створи агента і додай потрібний плагін"})

    assert builder.calls == []
    assert ordinary.calls == []
    assert router.active_project_id is None


def test_builder_route_preserves_current_product_selection(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "selection.db")
    store.initialize()
    products = ProductProjectCommandService(ProductProjectRepository(store))
    ordinary = _RecordingHandler("ordinary")
    builder = _RecordingHandler("builder")
    router = PackagedProductCommandRouter(
        products=products,
        ordinary_handler=ordinary,
        agent_builder_handler=builder,
    )
    product_command = "Створи застосунок для доступного каталогу"
    product_result = router.create({"command": product_command})
    selected = router.active_project_id

    result = router.create({"command": "Створи агента для перевірки каталогу"})

    assert product_result.status == "completed"
    assert selected is not None
    assert result.message == "builder"
    assert router.active_project_id == selected
    assert ordinary.calls == []
    assert len(builder.calls) == 1
