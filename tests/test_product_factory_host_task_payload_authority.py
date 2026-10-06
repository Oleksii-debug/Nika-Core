from __future__ import annotations

from contextlib import contextmanager

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.checkpoint import CheckpointService
from nika_core.kernel.task_queue import TaskPayloadCorruptionError, TaskQueue
from nika_core.product_factory_deployment import DeploymentFabricSnapshot
from nika_core.product_factory_checkpoint_host import (
    ProductFactoryCheckpointError,
    ProductFactoryCheckpointHost,
)
from nika_core.product_factory_deployment_checkpoint import (
    ProductFactoryDeploymentCheckpointError,
    ProductFactoryDeploymentCheckpointHost,
)


def _tampered_host_task(tmp_path):
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    task = TaskQueue(store).create(
        workspace_id="ws-product",
        agent_id="product-factory",
        payload={"kind": "product_factory", "product_project_id": "p1"},
    )
    duplicate_project_payload = (
        '{"kind":"product_factory","product_project_id":"foreign",'
        '"product_project_id":"p1"}'
    )
    with store.connection() as conn:
        conn.execute(
            "UPDATE tasks SET payload_json = ? WHERE task_id = ?",
            (duplicate_project_payload, task.task_id),
        )
    with pytest.raises(TaskPayloadCorruptionError):
        TaskQueue(store).get(task.task_id)
    return store, task.task_id


def test_pf2_checkpoint_host_rejects_task_payload_rejected_by_task_queue(
    tmp_path,
) -> None:
    store, task_id = _tampered_host_task(tmp_path)
    host = ProductFactoryCheckpointHost(store)

    with pytest.raises(ProductFactoryCheckpointError, match="payload is invalid"):
        host.clear(host_task_id=task_id, project_id="p1")

    with store.connection() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM audit_events WHERE event_type = ?",
            ("product_factory.checkpoints_cleared",),
        ).fetchone()[0]
    assert count == 0


def test_pf6_deployment_host_rejects_task_payload_rejected_by_task_queue(
    tmp_path,
) -> None:
    store, task_id = _tampered_host_task(tmp_path)
    host = ProductFactoryDeploymentCheckpointHost(store)

    with pytest.raises(
        ProductFactoryDeploymentCheckpointError,
        match="payload is invalid",
    ):
        host.latest_snapshot(host_task_id=task_id, project_id="p1")


class _CountingSQLiteStore(SQLiteStore):
    def __init__(self, path) -> None:
        super().__init__(path)
        self.connection_count = 0

    @contextmanager
    def connection(self):
        self.connection_count += 1
        with super().connection() as conn:
            yield conn


def _empty_deployment_snapshot() -> DeploymentFabricSnapshot:
    return DeploymentFabricSnapshot((), (), ())


def test_connection_bound_checkpoint_save_obeys_caller_rollback(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    task = TaskQueue(store).create(
        workspace_id="ws-product",
        agent_id="product-factory",
        payload={"kind": "product_factory", "product_project_id": "p1"},
    )
    service = CheckpointService(store)

    with pytest.raises(RuntimeError, match="rollback probe"):
        with store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            service.save_with_connection(
                conn,
                task_id=task.task_id,
                stage="test.atomic",
                payload={"schema": "test"},
            )
            raise RuntimeError("rollback probe")

    with store.connection() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM checkpoints WHERE task_id = ?",
            (task.task_id,),
        ).fetchone()[0]
    assert count == 0


def test_pf6_save_and_read_use_one_sqlite_authority_interval(tmp_path) -> None:
    store = _CountingSQLiteStore(tmp_path / "nika.db")
    store.initialize()
    task = TaskQueue(store).create(
        workspace_id="ws-product",
        agent_id="product-factory",
        payload={"kind": "product_factory", "product_project_id": "p1"},
    )
    host = ProductFactoryDeploymentCheckpointHost(store)
    snapshot = _empty_deployment_snapshot()

    store.connection_count = 0
    checkpoint_id = host.save(
        host_task_id=task.task_id,
        project_id="p1",
        snapshot=snapshot,
    )
    assert checkpoint_id
    assert store.connection_count == 1

    store.connection_count = 0
    restored = host.latest_snapshot(
        host_task_id=task.task_id,
        project_id="p1",
    )
    assert restored == snapshot
    assert store.connection_count == 1
