from __future__ import annotations

from pathlib import Path

from nika_core.config import AppConfig
from nika_core.product_factory_packaged_journey import product_project_identity
from scripts.nika_windows import build_windows_bridge


_ENG06_REQUIRED_FIELDS = frozenset(
    {
        "title",
        "project_id",
        "goal",
        "state",
        "spec_version",
        "blocker_count",
        "status_count",
        "decision_count",
    }
)


def _assert_eng06_projection(project: dict, *, detail) -> None:
    assert _ENG06_REQUIRED_FIELDS <= project.keys()
    assert project["title"] == detail.summary.title
    assert project["project_id"] == detail.summary.project_id
    assert project["goal"] == detail.summary.goal
    assert project["state"] == detail.summary.state
    assert project["spec_version"] == detail.summary.version
    assert project["blocker_count"] == detail.summary.blocker_count
    assert project["status_count"] == len(detail.statuses)
    assert project["decision_count"] == len(detail.decisions)

    for field in ("title", "project_id", "goal", "state"):
        assert isinstance(project[field], str)
        assert project[field].strip()
    assert isinstance(project["spec_version"], int)
    assert project["spec_version"] >= 1
    for field in ("blocker_count", "status_count", "decision_count"):
        assert isinstance(project[field], int)
        assert project[field] >= 0


def test_real_windows_bridge_satisfies_eng06_projection_and_restart(tmp_path: Path) -> None:
    database = tmp_path / "eng06 exact parent contract.db"
    config = AppConfig(database_path=database)
    command = "Створи застосунок для доступного керування нотатками"
    project_id = product_project_identity(command)

    bridge, products = build_windows_bridge(config)
    result = bridge.dispatch(
        {
            "request_id": "eng09-contract-create",
            "action_id": "task.create",
            "payload": {"command": command},
        }
    )

    assert result["status"] == "completed"
    detail = products.inspect_project(project_id)
    projection = bridge.get_state()["state"]["product_project"]
    _assert_eng06_projection(projection, detail=detail)

    restarted_bridge, restarted_products = build_windows_bridge(config)
    restarted_detail = restarted_products.inspect_project(project_id)
    recovered = restarted_bridge.get_state()["state"]["product_project"]

    _assert_eng06_projection(recovered, detail=restarted_detail)
    assert recovered == projection
