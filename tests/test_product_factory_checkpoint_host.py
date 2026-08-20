from __future__ import annotations

import json

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.product_factory_checkpoint_host import (
    ProductFactoryCheckpointError,
    ProductFactoryCheckpointHost,
    ProductFactoryCheckpointIntegrityError,
    ProductFactoryRecoveryDisposition,
)
from nika_core.product_factory_coordinator import WorkerResultEnvelope, WorkState
from nika_core.product_factory_orchestration import (
    ProductComponent,
    ProductRepositoryGraph,
    RepositoryRef,
)
from nika_core.product_factory_project_binding import (
    ProductProjectCoordinatorBinding,
    StaleProductProjectBindingError,
)
from nika_core.product_project import (
    ProductProjectRepository,
    ProductProjectSpec,
    ProductRequirement,
)
from nika_core.toolsmith.contracts import (
    ArtifactEvidence,
    ChangedFile,
    CodingResult,
    RecoveryState,
)
from nika_core.toolsmith.contracts import TestEvidence as WorkerTestEvidence

SHA_A = "a" * 40
SHA_B = "b" * 40
DIGEST_D = "d" * 64
DIGEST_E = "e" * 64
PERMISSIONS = frozenset({"read_source", "write_source", "run_tests"})
LOCATOR = "org/repo"


def _spec(goal: str = "Build accessible product") -> ProductProjectSpec:
    return ProductProjectSpec(
        goal=goal,
        desired_outcome="A reviewed product",
        requirements=(
            ProductRequirement(
                "req-1",
                "Keyboard operation",
                ("All primary actions keyboard reachable",),
            ),
        ),
        repository_refs=(LOCATOR,),
    )


def _graph() -> ProductRepositoryGraph:
    return ProductRepositoryGraph(
        project_id="p1",
        repositories=(RepositoryRef("repo-1", "github", LOCATOR, "main"),),
        components=(
            ProductComponent(
                component_id="core",
                repository_id="repo-1",
                paths=("src/core",),
                test_commands=(("python", "-m", "pytest", "tests/core"),),
            ),
            ProductComponent(
                component_id="docs",
                repository_id="repo-1",
                paths=("docs/product",),
                test_commands=(("python", "-m", "pytest", "tests/docs"),),
            ),
        ),
    )


def _setup(tmp_path):
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    projects = ProductProjectRepository(store)
    project = projects.create(
        project_id="p1",
        name="Product",
        spec=_spec(),
        idempotency_key="create:p1",
    )
    binding = ProductProjectCoordinatorBinding(project, _graph())
    task = TaskQueue(store).create(
        workspace_id="ws-product",
        agent_id="product-factory",
        payload={"kind": "product_factory", "product_project_id": "p1"},
    )
    return store, projects, binding, task.task_id


def _planned(binding: ProductProjectCoordinatorBinding):
    return binding.plan(
        base_shas={"repo-1": SHA_A},
        component_goals={"core": "build core", "docs": "write docs"},
        permission_ceiling=PERMISSIONS,
    )


def test_checkpoint_host_round_trip_survives_process_restart(tmp_path) -> None:
    store, projects, binding, task_id = _setup(tmp_path)
    coordinator = _planned(binding)
    started = coordinator.start("core")
    host = ProductFactoryCheckpointHost(store)
    saved = host.save(host_task_id=task_id, checkpoint=binding.checkpoint(coordinator))

    restarted_store = SQLiteStore(store.path)
    restarted_store.initialize()
    restarted_project = ProductProjectRepository(restarted_store).get("p1")
    restarted_binding = ProductProjectCoordinatorBinding(restarted_project, _graph())
    restored = ProductFactoryCheckpointHost(restarted_store).restore_latest(
        host_task_id=task_id,
        binding=restarted_binding,
    )

    snapshot = restored.snapshot()
    core = next(item for item in snapshot.records if item.request.component_id == "core")
    assert saved.checkpoint_id.startswith("pf2-")
    assert core.request.work_id == started.work_id
    assert core.state is WorkState.RUNNING
    assert {item.component_id for item in restored.ready_requests()} == {"docs"}
    assert projects.get("p1").row_version == restarted_project.row_version


def test_worker_evidence_round_trips_without_self_promotion(tmp_path) -> None:
    store, _, binding, task_id = _setup(tmp_path)
    coordinator = _planned(binding)
    request = coordinator.start("core")
    result = CodingResult(
        job_id=request.work_id,
        changed_files=(ChangedFile("src/core/main.py", DIGEST_D, 123),),
        test_evidence=(
            WorkerTestEvidence(("python", "-m", "pytest", "tests/core"), 0, DIGEST_E),
        ),
        artifacts=(ArtifactEvidence("report", DIGEST_D, "application/json"),),
        recovery_state=RecoveryState("completed", "opaque-worker-token"),
    )
    coordinator.record_result(
        WorkerResultEnvelope(
            work_id=request.work_id,
            component_id="core",
            repository_id="repo-1",
            base_sha=SHA_A,
            result_sha=SHA_B,
            diff_digest=DIGEST_D,
            coding_result=result,
        )
    )
    host = ProductFactoryCheckpointHost(store)
    host.save(host_task_id=task_id, checkpoint=binding.checkpoint(coordinator))

    restored = host.restore_latest(host_task_id=task_id, binding=binding)
    core = next(
        item for item in restored.snapshot().records if item.request.component_id == "core"
    )

    assert core.state is WorkState.REVIEW_REQUIRED
    assert core.result is not None
    assert core.result.result_sha == SHA_B
    assert core.result.coding_result.test_evidence[0].exit_code == 0
    assert core.result.coding_result.recovery_state == RecoveryState(
        "completed",
        "opaque-worker-token",
    )


def test_repeated_save_is_idempotent_and_revision_regression_fails_closed(tmp_path) -> None:
    store, _, binding, task_id = _setup(tmp_path)
    coordinator = _planned(binding)
    checkpoint_v1 = binding.checkpoint(coordinator)
    coordinator.start("core")
    checkpoint_v2 = binding.checkpoint(coordinator)
    host = ProductFactoryCheckpointHost(store)

    first = host.save(host_task_id=task_id, checkpoint=checkpoint_v1)
    again = host.save(host_task_id=task_id, checkpoint=checkpoint_v1)
    second = host.save(host_task_id=task_id, checkpoint=checkpoint_v2)

    assert first.checkpoint_id == again.checkpoint_id
    assert second.checkpoint.coordinator.revision > first.checkpoint.coordinator.revision
    with pytest.raises(ProductFactoryCheckpointError, match="checkpoint revision regressed"):
        host.save(host_task_id=task_id, checkpoint=checkpoint_v1)
    with store.connection() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM checkpoints WHERE task_id=? AND stage=?",
            (task_id, "product_factory.coordinator.v1"),
        ).fetchone()[0]
    assert count == 2


def test_stale_product_project_is_classified_for_explicit_reconciliation(tmp_path) -> None:
    store, projects, binding, task_id = _setup(tmp_path)
    coordinator = _planned(binding)
    host = ProductFactoryCheckpointHost(store)
    saved = host.save(host_task_id=task_id, checkpoint=binding.checkpoint(coordinator))
    updated = projects.update_spec(
        "p1",
        _spec("Build accessible product v2"),
        expected_row_version=binding.project.row_version,
    )
    refreshed = ProductProjectCoordinatorBinding(updated, _graph())

    candidate = host.inspect_latest(host_task_id=task_id, binding=refreshed)

    assert candidate.checkpoint_id == saved.checkpoint_id
    assert candidate.disposition is ProductFactoryRecoveryDisposition.STALE_PROJECT
    with pytest.raises(StaleProductProjectBindingError, match="explicit reconciliation"):
        host.restore_latest(host_task_id=task_id, binding=refreshed)


def test_tampered_checkpoint_is_never_deserialized_or_resumed(tmp_path) -> None:
    store, _, binding, task_id = _setup(tmp_path)
    coordinator = _planned(binding)
    host = ProductFactoryCheckpointHost(store)
    saved = host.save(host_task_id=task_id, checkpoint=binding.checkpoint(coordinator))

    with store.connection() as conn:
        row = conn.execute(
            "SELECT payload_json FROM checkpoints WHERE checkpoint_id=?",
            (saved.checkpoint_id,),
        ).fetchone()
        payload = json.loads(row["payload_json"])
        payload["coordinator"]["revision"] += 100
        conn.execute(
            "UPDATE checkpoints SET payload_json=? WHERE checkpoint_id=?",
            (json.dumps(payload, sort_keys=True), saved.checkpoint_id),
        )

    with pytest.raises(ProductFactoryCheckpointIntegrityError, match="checksum"):
        host.load(saved.checkpoint_id)
    candidate = host.inspect_latest(host_task_id=task_id, binding=binding)
    assert candidate.disposition is ProductFactoryRecoveryDisposition.CORRUPT


def test_host_task_project_identity_is_mandatory(tmp_path) -> None:
    store, _, binding, _ = _setup(tmp_path)
    foreign_task = TaskQueue(store).create(
        workspace_id="ws-product",
        agent_id="product-factory",
        payload={"kind": "product_factory", "product_project_id": "p2"},
    )
    coordinator = _planned(binding)
    host = ProductFactoryCheckpointHost(store)

    with pytest.raises(ProductFactoryCheckpointError, match="identity"):
        host.save(
            host_task_id=foreign_task.task_id,
            checkpoint=binding.checkpoint(coordinator),
        )

    candidate = host.inspect_latest(host_task_id=foreign_task.task_id, binding=binding)
    assert candidate.disposition is ProductFactoryRecoveryDisposition.INVALID_HOST_TASK


def test_clear_removes_only_product_factory_stage_for_host_task(tmp_path) -> None:
    store, _, binding, task_id = _setup(tmp_path)
    coordinator = _planned(binding)
    host = ProductFactoryCheckpointHost(store)
    host.save(host_task_id=task_id, checkpoint=binding.checkpoint(coordinator))
    with store.connection() as conn:
        conn.execute(
            """
            INSERT INTO checkpoints(
                checkpoint_id, task_id, stage, payload_json, checksum_sha256, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("foreign-checkpoint", task_id, "other.stage", "{}", DIGEST_D, "2026-01-01T00:00:00Z"),
        )

    deleted = host.clear(host_task_id=task_id, project_id="p1")

    assert deleted == 1
    assert host.latest(host_task_id=task_id, project_id="p1") is None
    with store.connection() as conn:
        foreign = conn.execute(
            "SELECT stage FROM checkpoints WHERE checkpoint_id='foreign-checkpoint'"
        ).fetchone()
    assert foreign["stage"] == "other.stage"
