from __future__ import annotations

from pathlib import Path

import pytest

from nika_core.config import AppConfig
from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.action_registry import Keymap
from nika_core.kernel.agent_registry import AgentRegistry
from nika_core.kernel.audit import AuditLog
from nika_core.kernel.default_actions import build_default_action_registry
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.workspace_registry import WorkspaceRegistry
from nika_core.ui.bridge import UIActionBridge
from nika_core.ui.desktop_backend import DesktopBackend
from scripts.nika_windows import build_windows_bridge


def _backend(tmp_path: Path) -> tuple[DesktopBackend, SQLiteStore]:
    store = SQLiteStore(tmp_path / "desktop projection.db")
    store.initialize()
    return (
        DesktopBackend(
            queue=TaskQueue(store),
            agents=AgentRegistry(store),
            workspaces=WorkspaceRegistry(store),
            audit=AuditLog(store),
        ),
        store,
    )


def _projection(**overrides):
    value = {
        "project_id": "product-" + "a" * 64,
        "spec_version": 1,
        "title": "Accessible product",
        "goal": "Build an accessible product",
        "state": "active",
        "blocker_count": 0,
        "status_count": 2,
        "decision_count": 1,
        "status_counts": {"requirement": 2},
        "decision_state_counts": {"pending": 1},
        "credential_refs": ("credential://must-not-cross",),
        "protected_store_handle": "secret-handle",
    }
    value.update(overrides)
    return value


def test_desktop_snapshot_exposes_only_bounded_product_project_allowlist(
    tmp_path: Path,
) -> None:
    backend, _store = _backend(tmp_path)
    backend.bind_product_project_state_provider(_projection)

    product = backend.snapshot()["product_project"]

    assert product == {
        "project_id": "product-" + "a" * 64,
        "title": "Accessible product",
        "goal": "Build an accessible product",
        "state": "active",
        "spec_version": 1,
        "blocker_count": 0,
        "status_count": 2,
        "decision_count": 1,
    }
    assert set(product).isdisjoint(
        {
            "evidence",
            "evidence_refs",
            "credential_refs",
            "authorization_ref",
            "provider_session",
            "protected_store_handle",
            "status_counts",
            "decision_state_counts",
        }
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("project_id", ""),
        ("title", "x" * 241),
        ("goal", "x" * 4001),
        ("state", "x" * 81),
        ("spec_version", 0),
        ("spec_version", True),
        ("blocker_count", -1),
        ("status_count", True),
        ("decision_count", -1),
    ],
)
def test_desktop_product_project_projection_fails_closed_on_malformed_state(
    tmp_path: Path,
    field: str,
    value,
) -> None:
    backend, store = _backend(tmp_path)
    backend.bind_product_project_state_provider(
        lambda: _projection(**{field: value})
    )
    actions = build_default_action_registry()
    bridge = UIActionBridge(
        actions,
        Keymap(store, actions),
        state_provider=backend.snapshot,
    )

    response = bridge.get_state()

    assert response["ok"] is False
    assert "ProductProject desktop state" in response["message"]


def test_desktop_product_project_provider_binding_is_one_time(tmp_path: Path) -> None:
    backend, _store = _backend(tmp_path)
    backend.bind_product_project_state_provider(lambda: None)

    with pytest.raises(RuntimeError, match="already bound"):
        backend.bind_product_project_state_provider(_projection)


def test_packaged_windows_bridge_uses_desktop_product_project_projection(
    tmp_path: Path,
) -> None:
    config = AppConfig(database_path=tmp_path / "Nika Core product projection.db")
    bridge, _products = build_windows_bridge(config)

    initial = bridge.get_state()
    assert initial["ok"] is True
    assert initial["state"]["product_project"] is None

    command = "Створи застосунок для доступного керування документами"
    result = bridge.dispatch(
        {
            "request_id": "desktop-product-projection",
            "action_id": "task.create",
            "payload": {"command": command},
        }
    )
    assert result["status"] == "completed"

    response = bridge.get_state()
    assert response["ok"] is True
    product = response["state"]["product_project"]
    assert product["title"] == command
    assert product["goal"] == command
    assert product["state"] == "active"
    assert product["spec_version"] == 1
    assert set(product) == {
        "project_id",
        "title",
        "goal",
        "state",
        "spec_version",
        "blocker_count",
        "status_count",
        "decision_count",
    }
