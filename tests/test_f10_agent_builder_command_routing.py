from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from nika_core.config import AppConfig
from nika_core.data.sqlite import SQLiteStore
from nika_core.product_command.contracts import CommandRouteKind
from nika_core.product_command.product_project_adapter import ProductProjectCommandService
from nika_core.product_command.routing import route_command
from nika_core.product_factory_packaged_journey import (
    PackagedProductCommandRouter,
    PackagedProductJourneyError,
    product_project_identity,
)
from nika_core.product_project import ProductProjectRepository
from nika_core.ui.bridge_models import UIResult


class RecordingHandler:
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


def _router(
    path: Path,
    *,
    agent_builder_handler: RecordingHandler | None,
) -> tuple[PackagedProductCommandRouter, ProductProjectRepository, RecordingHandler]:
    store = SQLiteStore(path)
    store.initialize()
    repository = ProductProjectRepository(store)
    ordinary = RecordingHandler("ordinary-task")
    return (
        PackagedProductCommandRouter(
            products=ProductProjectCommandService(repository),
            ordinary_handler=ordinary,
            agent_builder_handler=agent_builder_handler,
        ),
        repository,
        ordinary,
    )


@pytest.mark.parametrize(
    "command",
    (
        "Create an agent that summarizes accessible documents",
        "Configure an assistant for deterministic report triage",
        "Створи агента для аналізу доступних документів",
        "Налаштуй асистента для сортування звітів",
    ),
)
def test_explicit_agent_creation_routes_to_agent_builder(command: str) -> None:
    decision = route_command(command)

    assert decision.route is CommandRouteKind.AGENT_BUILDER
    assert decision.requires_user_decision is False
    assert decision.project_id is None
    assert decision.normalized_goal == command


def test_running_an_existing_agent_remains_an_ordinary_agent_task() -> None:
    decision = route_command("Run the existing agent and summarize its latest task log")

    assert decision.route is CommandRouteKind.AGENT_TASK


def test_make_existing_assistant_do_work_is_not_agent_creation() -> None:
    decision = route_command("Make the existing assistant summarize the latest report")

    assert decision.route is CommandRouteKind.AGENT_TASK


def test_agent_builder_and_toolsmith_intent_is_ambiguous() -> None:
    decision = route_command("Create an agent with a missing plugin capability")

    assert decision.route is CommandRouteKind.AMBIGUOUS
    assert decision.requires_user_decision is True


def test_agent_builder_and_product_intent_is_ambiguous() -> None:
    decision = route_command("Build an agent application for accessible research")

    assert decision.route is CommandRouteKind.AMBIGUOUS
    assert decision.requires_user_decision is True


def test_packaged_router_delegates_agent_intent_only_to_agent_builder_handler(
    tmp_path: Path,
) -> None:
    agent_builder = RecordingHandler("agent-builder")
    router, _repository, ordinary = _router(
        tmp_path / "agent builder.db",
        agent_builder_handler=agent_builder,
    )
    payload = {"command": "Створи агента для аналізу документів"}

    result = router.create(payload)

    assert result.message == "agent-builder"
    assert agent_builder.calls == [payload]
    assert ordinary.calls == []
    assert router.active_project_id is None


def test_packaged_router_fails_closed_when_agent_builder_is_not_composed(
    tmp_path: Path,
) -> None:
    router, repository, ordinary = _router(
        tmp_path / "agent builder unavailable.db",
        agent_builder_handler=None,
    )
    command = "Create an agent for report triage"

    with pytest.raises(PackagedProductJourneyError, match="Agent Builder"):
        router.create({"command": command})

    assert ordinary.calls == []
    assert router.active_project_id is None
    with pytest.raises(KeyError):
        repository.get(product_project_identity(command))


def test_real_windows_composition_creates_review_only_agent_draft_without_task_leak(
    tmp_path: Path,
) -> None:
    from scripts.nika_windows import build_windows_bridge

    database_path = tmp_path / "F10 packaged українська path.db"
    bridge, _products = build_windows_bridge(AppConfig(database_path=database_path))
    before = bridge.get_state()
    assert before["ok"] is True
    assert before["state"]["tasks"] == []
    assert before["state"]["product_project"] is None

    result = bridge.dispatch(
        {
            "request_id": "f10-packaged-agent-builder",
            "action_id": "task.create",
            "payload": {"command": "Створи агента для аналізу доступних документів"},
        }
    )

    assert result["status"] == "completed"
    assert "Чернетку Agent Builder збережено" in result["message"]
    assert "не активована" in result["message"]
    assert result["focus_id"] == "agents-heading"

    after = bridge.get_state()
    assert after["ok"] is True
    assert after["state"]["tasks"] == []
    assert after["state"]["product_project"] is None
    assert len(after["state"]["agent_builder_definitions"]) == 1
    projected = after["state"]["agent_builder_definitions"][0]
    assert projected["status"] == "draft"
    assert projected["requires_human_approval"] is False
    assert any(
        item["agent_id"] == projected["agent_id"]
        and item["name"].startswith("Agent Builder [чернетка]:")
        for item in after["state"]["agents"]
    )

    store = SQLiteStore(database_path)
    with store.connection() as conn:
        rows = conn.execute(
            "SELECT agent_id, version, status FROM agent_definitions"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["version"] == 1
    assert rows[0]["status"] == "draft"

    restarted_bridge, _restarted_products = build_windows_bridge(
        AppConfig(database_path=database_path)
    )
    recovered = restarted_bridge.get_state()
    assert recovered["ok"] is True
    recovered_definitions = recovered["state"]["agent_builder_definitions"]
    assert len(recovered_definitions) == 1
    assert recovered_definitions[0]["agent_id"] == projected["agent_id"]
    assert recovered_definitions[0]["status"] == "draft"

    replay = restarted_bridge.dispatch(
        {
            "request_id": "f10-packaged-agent-builder-replay",
            "action_id": "task.create",
            "payload": {"command": "Створи агента для аналізу доступних документів"},
        }
    )
    assert replay["status"] == "completed"
    assert "вже збережена без змін" in replay["message"]
    with store.connection() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM agent_definitions WHERE agent_id = ?",
            (projected["agent_id"],),
        ).fetchone()[0]
    assert count == 1


def test_ambiguous_agent_command_invokes_no_specialized_or_ordinary_handler(
    tmp_path: Path,
) -> None:
    agent_builder = RecordingHandler("agent-builder")
    router, _repository, ordinary = _router(
        tmp_path / "ambiguous agent.db",
        agent_builder_handler=agent_builder,
    )

    with pytest.raises(PackagedProductJourneyError, match="одночасно"):
        router.create({"command": "Створи агента і додай потрібний плагін"})

    assert agent_builder.calls == []
    assert ordinary.calls == []
    assert router.active_project_id is None


def test_agent_builder_route_cannot_replace_current_product_selection(tmp_path: Path) -> None:
    agent_builder = RecordingHandler("agent-builder")
    router, _repository, ordinary = _router(
        tmp_path / "selection isolation.db",
        agent_builder_handler=agent_builder,
    )
    product_command = "Створи застосунок для доступного каталогу"
    project_id = product_project_identity(product_command)
    router.create({"command": product_command})

    result = router.create({"command": "Створи агента для перевірки каталогу"})

    assert result.message == "agent-builder"
    assert router.active_project_id == project_id
    assert ordinary.calls == []
    assert len(agent_builder.calls) == 1
