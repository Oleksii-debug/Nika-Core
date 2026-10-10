from __future__ import annotations

from pathlib import Path

from nika_core.config import AppConfig
from nika_core.product_factory_packaged_journey import product_project_identity
from scripts.nika_windows import build_windows_bridge


def _dispatch(bridge: object, request_id: str, command: str) -> dict[str, object]:
    return bridge.dispatch(  # type: ignore[attr-defined]
        {
            "request_id": request_id,
            "action_id": "task.create",
            "payload": {"command": command},
        }
    )


def test_real_bridge_refines_and_recovers_same_productproject(tmp_path: Path) -> None:
    database = tmp_path / "bridge refinement journey.db"
    config = AppConfig(database_path=database)
    original_goal = "Create accessible product application for document review"
    refined_goal = "Create accessible Windows product application for document review"
    project_id = product_project_identity(original_goal)

    first_bridge, first_products = build_windows_bridge(config)
    created = _dispatch(first_bridge, "create-product", original_goal)
    current = _dispatch(first_bridge, "current-product", "Show current ProductProject")
    refined = _dispatch(
        first_bridge,
        "refine-product",
        f"Set current ProductProject goal: {refined_goal}",
    )

    assert created["status"] == "completed"
    assert current["status"] == "completed"
    assert f"Поточний ProductProject: {project_id}; spec version 1" in str(current["message"])
    assert refined == {
        "request_id": "refine-product",
        "status": "completed",
        "message": (
            f"ProductProject оновлено: {project_id}; "
            f"spec version 1 -> 2; state active; goal: {refined_goal}."
        ),
        "focus_id": "tasks-heading",
    }
    durable = first_products.inspect_project(project_id)
    assert durable.summary.version == 2
    assert durable.summary.goal == refined_goal
    state = first_bridge.get_state()
    assert state["ok"] is True
    assert state["state"]["product_project"]["project_id"] == project_id  # type: ignore[index]
    assert state["state"]["product_project"]["spec_version"] == 2  # type: ignore[index]
    assert state["state"]["product_project"]["goal"] == refined_goal  # type: ignore[index]

    restarted_bridge, restarted_products = build_windows_bridge(config)
    recovered = restarted_bridge.get_state()
    assert recovered["ok"] is True
    recovered_project = recovered["state"]["product_project"]  # type: ignore[index]
    assert recovered_project["project_id"] == project_id
    assert recovered_project["spec_version"] == 2
    assert recovered_project["goal"] == refined_goal

    current_after_restart = _dispatch(
        restarted_bridge,
        "current-after-restart",
        "Current ProductProject",
    )
    repeated = _dispatch(
        restarted_bridge,
        "repeat-refinement",
        f"Update current ProductProject goal: {refined_goal}",
    )

    assert current_after_restart["status"] == "completed"
    assert f"spec version 2; state active; goal: {refined_goal}." in str(
        current_after_restart["message"]
    )
    assert repeated["status"] == "completed"
    assert repeated["focus_id"] == "tasks-heading"
    assert "вже актуальна" in str(repeated["message"])
    assert restarted_products.inspect_project(project_id).summary.version == 2
