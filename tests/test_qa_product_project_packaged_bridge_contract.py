from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from nika_core.config import AppConfig
from scripts.nika_windows import build_windows_bridge


_FRONTEND_REQUIRED_FIELDS = {
    "project_id",
    "title",
    "goal",
    "state",
    "spec_version",
    "blocker_count",
    "status_count",
    "decision_count",
}
_FORBIDDEN_AUTHORITY_FIELDS = {
    "evidence",
    "evidence_refs",
    "credential_refs",
    "authorization_ref",
    "provider_session",
    "protected_store_handle",
}


def _state(bridge: Any) -> Mapping[str, Any]:
    response = bridge.get_state()
    assert response["ok"] is True
    state = response["state"]
    assert isinstance(state, Mapping)
    return state


def _assert_frontend_compatible_product_state(
    product_state: object,
    *,
    goal: str,
) -> Mapping[str, Any]:
    assert isinstance(product_state, Mapping)
    assert _FRONTEND_REQUIRED_FIELDS.issubset(product_state)
    assert _FORBIDDEN_AUTHORITY_FIELDS.isdisjoint(product_state)

    for field in ("project_id", "title", "goal", "state"):
        value = product_state[field]
        assert isinstance(value, str)
        assert value.strip()

    assert product_state["title"] == goal
    assert product_state["goal"] == goal
    assert product_state["state"] == "active"

    spec_version = product_state["spec_version"]
    assert isinstance(spec_version, int) and not isinstance(spec_version, bool)
    assert spec_version >= 1
    for field in ("blocker_count", "status_count", "decision_count"):
        value = product_state[field]
        assert isinstance(value, int) and not isinstance(value, bool)
        assert value >= 0

    return product_state


def test_exact_packaged_windows_bridge_satisfies_product_project_frontend_contract_after_restart(
    tmp_path: Path,
) -> None:
    database = tmp_path / "Nika QA ProductProject bridge перезапуск.db"
    config = AppConfig(database_path=database)
    goal = "Створи застосунок для доступного обліку рахунків"

    bridge, products = build_windows_bridge(config)
    assert _state(bridge)["product_project"] is None

    result = bridge.dispatch(
        {
            "request_id": "qa-product-project-create",
            "action_id": "task.create",
            "payload": {"command": goal},
        }
    )
    assert result["status"] == "completed"

    first = _assert_frontend_compatible_product_state(
        _state(bridge)["product_project"],
        goal=goal,
    )
    project_id = first["project_id"]
    assert products.inspect_project(project_id).summary.project_id == project_id

    restarted_bridge, restarted_products = build_windows_bridge(config)
    restarted = _assert_frontend_compatible_product_state(
        _state(restarted_bridge)["product_project"],
        goal=goal,
    )

    assert restarted["project_id"] == project_id
    assert restarted["spec_version"] == first["spec_version"]
    assert restarted["blocker_count"] == first["blocker_count"]
    assert restarted["status_count"] == first["status_count"]
    assert restarted["decision_count"] == first["decision_count"]
    assert restarted_products.inspect_project(project_id).summary.project_id == project_id
