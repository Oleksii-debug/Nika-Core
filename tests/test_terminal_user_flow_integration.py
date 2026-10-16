from __future__ import annotations

from pathlib import Path

from nika_core.config import AppConfig
from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState
from nika_core.product_factory_packaged_journey import product_project_identity
from nika_core.product_project import (
    EvidenceRef,
    ProductDecision,
    ProductDecisionState,
    ProductOption,
    ProductProjectRepository,
    ResearchEvidencePackage,
)
from scripts import nika_windows


def _ready(queue: TaskQueue, index: int) -> str:
    record = queue.create(
        workspace_id="default",
        agent_id="nika.default",
        payload={"command": f"integration-task-{index}"},
    )
    queue.transition(record.task_id, TaskState.READY)
    return record.task_id


def test_product_decision_and_accessible_task_pages_share_one_packaged_state(
    tmp_path: Path,
) -> None:
    database = (tmp_path / "combined user flow.db").resolve()
    bridge, products = nika_windows.build_windows_bridge(
        AppConfig(database_path=database),
        start_startup_recovery=False,
    )
    project_command = "Створи доступний застосунок для щоденних нотаток"
    project_id = product_project_identity(project_command)
    created = bridge.dispatch(
        {
            "request_id": "combined-product-create",
            "action_id": "task.create",
            "payload": {"command": project_command},
        }
    )
    assert created["status"] == "completed"

    store = SQLiteStore(database)
    store.initialize()
    repository = ProductProjectRepository(store)
    repository.record_research_handoff(
        project_id,
        ResearchEvidencePackage(
            "research-combined-flow",
            (
                EvidenceRef(
                    "evidence:combined-flow",
                    "research://combined-flow/claim/1",
                    "Owner choice is required before product continuation",
                ),
            ),
        ),
        (
            ProductOption(
                "option-combined-flow",
                "Accessible Windows path",
                "Keep the packaged keyboard/NVDA path",
                ("research-combined-flow",),
            ),
        ),
    )
    products.record_decision(
        project_id,
        ProductDecision(
            decision_id="decision-combined-flow",
            option_id="option-combined-flow",
            state=ProductDecisionState.PROPOSED,
            rationale="Owner must choose before continuation",
            decided_by_ref="user://owner",
        ),
        expected_row_version=0,
        idempotency_key="decision:combined-flow:pending",
    )

    queue = TaskQueue(store)
    task_ids = {_ready(queue, index) for index in range(55)}

    first = bridge.get_state()
    assert first["ok"] is True
    first_state = first["state"]
    project_state = first_state["product_project"]
    assert project_state["current_decision"]["decision_id"] == "decision-combined-flow"
    assert isinstance(project_state["status_items"], list)
    assert isinstance(project_state["status_items_truncated"], bool)
    assert len(project_state["status_items"]) <= 24
    assert len(project_state["status_items"]) <= project_state["status_count"]
    assert project_state["status_items_truncated"] is (
        len(project_state["status_items"]) < project_state["status_count"]
    )
    assert first_state["task_page"] == {
        "schema": "nika.task-page:v1",
        "page_size": 50,
        "offset": 0,
        "page_number": 1,
        "has_previous": False,
        "has_next": True,
        "unfinished_only": True,
    }
    assert len(first_state["tasks"]) == 50

    moved = bridge.dispatch(
        {
            "request_id": "combined-page-next",
            "action_id": "task.page.next",
            "payload": {},
        }
    )
    assert moved["status"] == "completed"

    second_state = bridge.get_state()["state"]
    assert second_state["task_page"]["offset"] == 50
    assert len(second_state["tasks"]) == 5
    assert second_state["product_project"]["current_decision"] == project_state["current_decision"]
    assert second_state["product_project"]["status_items"] == project_state["status_items"]
    assert second_state["product_project"]["status_items_truncated"] is (
        project_state["status_items_truncated"]
    )

    selected_task_id = second_state["tasks"][0]["task_id"]
    assert selected_task_id in task_ids
    paused = bridge.dispatch(
        {
            "request_id": "combined-selected-pause",
            "action_id": "task.pause",
            "payload": {"task_id": selected_task_id},
        }
    )
    assert paused["status"] == "completed"
    assert queue.get(selected_task_id).state is TaskState.PAUSED

    final_state = bridge.get_state()["state"]
    assert final_state["product_project"]["current_decision"] == project_state["current_decision"]
    assert final_state["product_project"]["status_items"] == project_state["status_items"]
    assert final_state["product_project"]["status_items_truncated"] is (
        project_state["status_items_truncated"]
    )
    assert final_state["product_project"]["decision_count"] == 1
