from __future__ import annotations

import json

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.product_factory_checkpoint_host import (
    ProductFactoryCheckpointError,
    ProductFactoryCheckpointHost,
    ProductFactoryRecoveryDisposition,
    ProductFactoryTrustedPlanAuthorityError,
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
LOCATOR = "org/repo"
PERMISSIONS = frozenset({"read_source", "write_source", "run_tests"})


def _setup(tmp_path):
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    project = ProductProjectRepository(store).create(
        project_id="p1",
        name="Product",
        spec=ProductProjectSpec(
            goal="Build durable product",
            desired_outcome="A restart-safe product",
            requirements=(
                ProductRequirement(
                    "req-1",
                    "Checkpoint authority is atomic",
                    ("First authority and checkpoint commit together",),
                ),
            ),
            repository_refs=(LOCATOR,),
        ),
        idempotency_key="create:p1",
    )
    graph = ProductRepositoryGraph(
        project_id="p1",
        repositories=(RepositoryRef("repo-1", "github", LOCATOR, "main"),),
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
        workspace_id="ws-product",
        agent_id="product-factory",
        payload={"kind": "product_factory", "product_project_id": "p1"},
    )
    return store, binding, coordinator, task.task_id


def _task_payload(store: SQLiteStore, task_id: str) -> dict[str, object]:
    with store.connection() as conn:
        row = conn.execute(
            "SELECT payload_json FROM tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
    assert row is not None
    return json.loads(row["payload_json"])


def test_first_anchor_rolls_back_if_checkpoint_insert_fails(tmp_path) -> None:
    store, binding, coordinator, task_id = _setup(tmp_path)
    checkpoint = binding.checkpoint(coordinator)
    host = ProductFactoryCheckpointHost(store)

    with store.connection() as conn:
        conn.execute(
            """
            CREATE TRIGGER force_first_checkpoint_failure
            BEFORE INSERT ON checkpoints
            BEGIN
                SELECT RAISE(ABORT, 'forced checkpoint failure');
            END
            """
        )

    with pytest.raises(ProductFactoryCheckpointError, match="checkpoint identity"):
        host.save(host_task_id=task_id, checkpoint=checkpoint)

    assert "trusted_plan_fingerprint" not in _task_payload(store, task_id)
    with store.connection() as conn:
        checkpoint_count = conn.execute(
            "SELECT COUNT(*) FROM checkpoints WHERE task_id = ?",
            (task_id,),
        ).fetchone()[0]
        authority_audit_count = conn.execute(
            """
            SELECT COUNT(*) FROM audit_events
            WHERE event_type = 'product_factory.trusted_plan_bound' AND entity_id = ?
            """,
            ("p1",),
        ).fetchone()[0]
        conn.execute("DROP TRIGGER force_first_checkpoint_failure")

    assert checkpoint_count == 0
    assert authority_audit_count == 0

    saved = host.save(host_task_id=task_id, checkpoint=binding.checkpoint(coordinator))
    payload = _task_payload(store, task_id)
    assert payload["trusted_plan_fingerprint"] == coordinator.trusted_plan_fingerprint
    assert saved.checkpoint.coordinator == coordinator.snapshot()


def test_checkpoint_without_durable_host_authority_fails_closed_as_legacy_state(tmp_path) -> None:
    store, binding, coordinator, task_id = _setup(tmp_path)
    host = ProductFactoryCheckpointHost(store)
    host.save(host_task_id=task_id, checkpoint=binding.checkpoint(coordinator))

    with store.connection() as conn:
        row = conn.execute(
            "SELECT payload_json FROM tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        assert row is not None
        payload = json.loads(row["payload_json"])
        payload.pop("trusted_plan_fingerprint")
        conn.execute(
            "UPDATE tasks SET payload_json = ? WHERE task_id = ?",
            (
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                task_id,
            ),
        )

    candidate = host.inspect_latest(host_task_id=task_id, binding=binding)
    assert candidate.disposition is ProductFactoryRecoveryDisposition.MISSING_TRUSTED_PLAN

    with pytest.raises(
        ProductFactoryTrustedPlanAuthorityError,
        match="no trusted plan authority",
    ):
        host.restore_latest(host_task_id=task_id, binding=binding)
