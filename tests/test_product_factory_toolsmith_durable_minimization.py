from __future__ import annotations

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.product_factory_coordinator import ComponentWorkRequest
from nika_core.product_factory_toolsmith_state import (
    ProductFactoryToolsmithBindingRepository,
)

_CANARY = "PF10_DURABLE_SECRET_CANARY_7f31c2"


def _request() -> ComponentWorkRequest:
    return ComponentWorkRequest(
        work_id="work-1",
        project_id="project-1",
        component_id="core",
        repository_id="repo-1",
        goal="repair missing capability",
        base_sha="a" * 40,
        allowed_paths=("src/core",),
        permission_ceiling=frozenset({"read_source", "write_source", "run_tests"}),
        acceptance_commands=(("python", "-m", "pytest", "tests/core"),),
    )


def test_durable_toolsmith_gap_minimizes_free_text_across_sqlite_restart(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    host_task = TaskQueue(store).create(
        workspace_id="ws-product",
        agent_id="product-factory",
        payload={"kind": "product_factory", "product_project_id": "project-1"},
    )
    request = _request()

    repository = ProductFactoryToolsmithBindingRepository(store)
    first = repository.reserve(
        host_task_id=host_task.task_id,
        request=request,
        capability_id="toml-editor",
        reason=f"Authorization: Bearer {_CANARY}",
        attempted_methods=(f"backend token={_CANARY}",),
    )

    assert _CANARY not in repr(first)
    assert first.reason == "Product Factory worker capability gap"
    assert first.attempted_methods == ()
    with store.connection() as conn:
        row = conn.execute(
            "SELECT reason, attempted_methods_json "
            "FROM product_factory_toolsmith_bindings "
            "WHERE host_task_id = ? AND work_id = ?",
            (host_task.task_id, request.work_id),
        ).fetchone()
    assert row is not None
    assert _CANARY not in f"{row['reason']} {row['attempted_methods_json']}"

    restarted = ProductFactoryToolsmithBindingRepository(store)
    replay = restarted.reserve(
        host_task_id=host_task.task_id,
        request=request,
        capability_id="toml-editor",
        reason=f"Authorization: Bearer {_CANARY}",
        attempted_methods=(f"backend token={_CANARY}",),
    )

    assert replay == first
    assert _CANARY not in repr(replay)
