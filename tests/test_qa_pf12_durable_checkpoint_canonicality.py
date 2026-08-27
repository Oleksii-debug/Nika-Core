from __future__ import annotations

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.product_factory_checkpoint_host import (
    ProductFactoryCheckpointHost,
    ProductFactoryCheckpointIntegrityError,
)
from nika_core.product_factory_orchestration import (
    ProductComponent,
    ProductRepositoryGraph,
    RepositoryRef,
)
from nika_core.product_factory_project_binding import ProductProjectCoordinatorBinding
from nika_core.product_project import (
    ProductProjectRepository,
    ProductProjectSpec,
    ProductRequirement,
)

SHA_A = "a" * 40
PERMISSIONS = frozenset({"read_source", "write_source", "run_tests"})


def _setup(tmp_path):
    store = SQLiteStore(tmp_path / "qa-pf12-checkpoint-id.db")
    store.initialize()
    project = ProductProjectRepository(store).create(
        project_id="qa-pf12-project",
        name="PF12 checkpoint identity QA",
        spec=ProductProjectSpec(
            goal="Build a restart-safe product",
            desired_outcome="Bind checkpoint identity to canonical durable state",
            requirements=(
                ProductRequirement(
                    "req-1",
                    "Checkpoint identity is not caller-rebindable",
                    ("Restart rejects a substituted checkpoint identifier",),
                ),
            ),
            repository_refs=("org/repo",),
        ),
        idempotency_key="qa-pf12:create",
    )
    graph = ProductRepositoryGraph(
        project_id=project.project_id,
        repositories=(RepositoryRef("repo-1", "github", "org/repo", "main"),),
        components=(
            ProductComponent(
                component_id="core",
                repository_id="repo-1",
                paths=("src/core",),
                test_commands=(("python", "-m", "pytest", "tests/core"),),
            ),
        ),
    )
    binding = ProductProjectCoordinatorBinding(project, graph)
    coordinator = binding.plan(
        base_shas={"repo-1": SHA_A},
        component_goals={"core": "build core"},
        permission_ceiling=PERMISSIONS,
    )
    task = TaskQueue(store).create(
        workspace_id="qa-pf12-workspace",
        agent_id="product-factory",
        payload={
            "kind": "product_factory",
            "product_project_id": project.project_id,
        },
    )
    saved = ProductFactoryCheckpointHost(store).save(
        host_task_id=task.task_id,
        checkpoint=binding.checkpoint(coordinator),
    )
    return store, task.task_id, saved.checkpoint_id


def _restart(store: SQLiteStore) -> ProductFactoryCheckpointHost:
    restarted = SQLiteStore(store.path)
    restarted.initialize()
    return ProductFactoryCheckpointHost(restarted)


def test_checkpoint_id_rebinding_fails_closed_with_unchanged_canonical_bytes(tmp_path) -> None:
    store, task_id, checkpoint_id = _setup(tmp_path)
    rebound_id = "pf2-" + ("f" * 32)
    assert rebound_id != checkpoint_id

    with store.connection() as conn:
        before = conn.execute(
            """
            SELECT payload_json, checksum_sha256
            FROM checkpoints
            WHERE checkpoint_id = ?
            """,
            (checkpoint_id,),
        ).fetchone()
        assert before is not None
        conn.execute(
            "UPDATE checkpoints SET checkpoint_id = ? WHERE checkpoint_id = ?",
            (rebound_id, checkpoint_id),
        )
        after = conn.execute(
            """
            SELECT payload_json, checksum_sha256
            FROM checkpoints
            WHERE checkpoint_id = ?
            """,
            (rebound_id,),
        ).fetchone()
        assert after is not None

    assert after["payload_json"] == before["payload_json"]
    assert after["checksum_sha256"] == before["checksum_sha256"]

    with pytest.raises(
        ProductFactoryCheckpointIntegrityError,
        match="checkpoint|identity|integrity",
    ):
        _restart(store).load(rebound_id, host_task_id=task_id)
