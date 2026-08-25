from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.product_factory_checkpoint_host import (
    ProductFactoryCheckpointHost,
    ProductFactoryCheckpointIntegrityError,
    ProductFactoryTrustedPlanAuthorityError,
)
from nika_core.product_factory_coordinator import (
    ComponentWorkRequest,
    WorkerResultEnvelope,
    WorkState,
)
from nika_core.product_factory_orchestration import (
    ProductComponent,
    ProductRepositoryGraph,
    RepositoryRef,
)
from nika_core.product_factory_project_binding import (
    ProductProjectCoordinatorBinding,
    ProductProjectCoordinatorCheckpoint,
)
from nika_core.product_project import (
    ProductProjectRepository,
    ProductProjectSpec,
    ProductRequirement,
)
from nika_core.toolsmith.contracts import CodingResult, WorkerFailure, WorkerFailureKind

PROJECT_ID = "pf12-durable-head-project"
LOCATOR = "org/pf12-head"
SHA_A = "a" * 40
SHA_B = "b" * 40
DIFF_DIGEST = "d" * 64
PERMISSIONS = frozenset({"read_source", "write_source", "run_tests"})
CHECKPOINT_STAGE = "product_factory.coordinator.v1"
TRUSTED_PLAN_KEY = "trusted_plan_fingerprint"
CHECKPOINT_HEAD_KEY = "product_factory_checkpoint_head"


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _graph() -> ProductRepositoryGraph:
    return ProductRepositoryGraph(
        project_id=PROJECT_ID,
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


def _setup(tmp_path):
    store = SQLiteStore(tmp_path / "pf12-durable-head.db")
    store.initialize()
    project = ProductProjectRepository(store).create(
        project_id=PROJECT_ID,
        name="PF12 durable checkpoint head",
        spec=ProductProjectSpec(
            goal="Keep long-horizon checkpoint authority restart safe",
            desired_outcome="Only host-admitted checkpoints can become restart authority",
            requirements=(
                ProductRequirement(
                    "req-head",
                    "Checkpoint authority survives reset, clock rollback and restart",
                    ("candidate-created durable rows never become canonical authority",),
                ),
            ),
            repository_refs=(LOCATOR,),
        ),
        idempotency_key="pf12:durable-head:create",
    )
    binding = ProductProjectCoordinatorBinding(project, _graph())
    coordinator = binding.plan(
        base_shas={"repo-1": SHA_A},
        component_goals={"core": "build core"},
        permission_ceiling=PERMISSIONS,
    )
    task = TaskQueue(store).create(
        workspace_id="pf12-head-workspace",
        agent_id="product-factory",
        payload={
            "kind": "product_factory",
            "product_project_id": PROJECT_ID,
            "unrelated_owner_metadata": "preserve-me",
        },
    )
    return store, binding, coordinator, task.task_id


def _task_payload(store: SQLiteStore, task_id: str) -> dict[str, object]:
    with store.connection() as conn:
        row = conn.execute(
            "SELECT payload_json FROM tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
    assert row is not None
    payload = json.loads(row["payload_json"])
    assert isinstance(payload, dict)
    return payload


def _request_payload(request: ComponentWorkRequest) -> dict[str, object]:
    return {
        "work_id": request.work_id,
        "project_id": request.project_id,
        "component_id": request.component_id,
        "repository_id": request.repository_id,
        "goal": request.goal,
        "base_sha": request.base_sha,
        "allowed_paths": list(request.allowed_paths),
        "permission_ceiling": sorted(request.permission_ceiling),
        "acceptance_commands": [list(command) for command in request.acceptance_commands],
        "attempt": request.attempt,
    }


def _checkpoint_id_from_payload(
    *,
    host_task_id: str,
    payload: dict[str, object],
    checksum: str,
) -> str:
    coordinator = payload["coordinator"]
    assert isinstance(coordinator, dict)
    identity = _canonical(
        {
            "host_task_id": host_task_id,
            "project_id": payload["project_id"],
            "spec_version": payload["spec_version"],
            "row_version": payload["row_version"],
            "revision": coordinator["revision"],
            "checksum": checksum,
        }
    )
    return f"pf2-{_sha256(identity)[:32]}"


def _persist_repair_required(store, binding, coordinator, task_id):
    host = ProductFactoryCheckpointHost(store)
    host.save(host_task_id=task_id, checkpoint=binding.checkpoint(coordinator))
    request = coordinator.start("core")
    coordinator.record_result(
        WorkerResultEnvelope(
            work_id=request.work_id,
            component_id=request.component_id,
            repository_id=request.repository_id,
            base_sha=request.base_sha,
            result_sha=SHA_B,
            diff_digest=DIFF_DIGEST,
            coding_result=CodingResult(
                job_id=request.work_id,
                failure=WorkerFailure(
                    WorkerFailureKind.PROCESS_FAILED,
                    "deterministic repair required",
                    retryable=True,
                ),
            ),
        )
    )
    saved = host.save(
        host_task_id=task_id,
        checkpoint=binding.checkpoint(coordinator),
    )
    record = saved.checkpoint.coordinator.records[0]
    assert record.state is WorkState.REPAIR_REQUIRED
    return host, saved


def _restart(store: SQLiteStore):
    restarted_store = SQLiteStore(store.path)
    restarted_store.initialize()
    project = ProductProjectRepository(restarted_store).get(PROJECT_ID)
    binding = ProductProjectCoordinatorBinding(project, _graph())
    return restarted_store, binding


def test_committed_head_not_created_at_selects_restart_authority(tmp_path) -> None:
    store, binding, coordinator, task_id = _setup(tmp_path)
    host = ProductFactoryCheckpointHost(store)
    first = host.save(host_task_id=task_id, checkpoint=binding.checkpoint(coordinator))
    coordinator.start("core")
    second = host.save(host_task_id=task_id, checkpoint=binding.checkpoint(coordinator))
    assert second.checkpoint.coordinator.revision > first.checkpoint.coordinator.revision

    with store.connection() as conn:
        conn.execute(
            "UPDATE checkpoints SET created_at = ? WHERE checkpoint_id = ?",
            ("2099-01-01T00:00:00+00:00", first.checkpoint_id),
        )
        conn.execute(
            "UPDATE checkpoints SET created_at = ? WHERE checkpoint_id = ?",
            ("2001-01-01T00:00:00+00:00", second.checkpoint_id),
        )

    restarted_store, _ = _restart(store)
    restarted = ProductFactoryCheckpointHost(restarted_store).latest(
        host_task_id=task_id,
        project_id=PROJECT_ID,
    )
    assert restarted is not None
    assert restarted.checkpoint_id == second.checkpoint_id
    assert restarted.checkpoint.coordinator.revision == second.checkpoint.coordinator.revision


def test_clear_revokes_plan_and_head_then_requires_fresh_live_anchor(tmp_path) -> None:
    store, binding, coordinator, task_id = _setup(tmp_path)
    host = ProductFactoryCheckpointHost(store)
    host.save(host_task_id=task_id, checkpoint=binding.checkpoint(coordinator))
    with store.connection() as conn:
        conn.execute(
            """
            INSERT INTO checkpoints(
                checkpoint_id, task_id, stage, payload_json, checksum_sha256, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "foreign-checkpoint",
                task_id,
                "other.stage",
                "{}",
                DIFF_DIGEST,
                "2026-01-01T00:00:00+00:00",
            ),
        )

    before = _task_payload(store, task_id)
    assert TRUSTED_PLAN_KEY in before
    assert CHECKPOINT_HEAD_KEY in before
    assert host.clear(host_task_id=task_id, project_id=PROJECT_ID) == 1

    after = _task_payload(store, task_id)
    assert TRUSTED_PLAN_KEY not in after
    assert CHECKPOINT_HEAD_KEY not in after
    assert after["unrelated_owner_metadata"] == "preserve-me"
    assert host.latest(host_task_id=task_id, project_id=PROJECT_ID) is None
    with store.connection() as conn:
        foreign = conn.execute(
            "SELECT stage FROM checkpoints WHERE checkpoint_id = ?",
            ("foreign-checkpoint",),
        ).fetchone()
    assert foreign is not None
    assert foreign["stage"] == "other.stage"

    unproved = ProductProjectCoordinatorCheckpoint(
        project_id=binding.project.project_id,
        spec_version=binding.project.spec_version,
        row_version=binding.project.row_version,
        coordinator=coordinator.snapshot(),
    )
    with pytest.raises(
        ProductFactoryTrustedPlanAuthorityError,
        match="first Product Factory checkpoint requires live trusted plan authority proof",
    ):
        host.save(host_task_id=task_id, checkpoint=unproved)

    reanchored = host.save(
        host_task_id=task_id,
        checkpoint=binding.checkpoint(coordinator),
    )
    assert reanchored.checkpoint_id.startswith("pf2-")
    assert host.clear(host_task_id=task_id, project_id=PROJECT_ID) == 1
    assert host.clear(host_task_id=task_id, project_id=PROJECT_ID) == 0


def test_clear_rolls_back_checkpoint_delete_if_anchor_revoke_fails(tmp_path) -> None:
    store, binding, coordinator, task_id = _setup(tmp_path)
    host = ProductFactoryCheckpointHost(store)
    saved = host.save(host_task_id=task_id, checkpoint=binding.checkpoint(coordinator))
    with store.connection() as conn:
        conn.execute(
            """
            CREATE TRIGGER pf12_block_task_update
            BEFORE UPDATE ON tasks
            BEGIN
                SELECT RAISE(ABORT, 'blocked pf12 task update');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="blocked pf12 task update"):
        host.clear(host_task_id=task_id, project_id=PROJECT_ID)

    with store.connection() as conn:
        row = conn.execute(
            "SELECT checkpoint_id FROM checkpoints WHERE checkpoint_id = ?",
            (saved.checkpoint_id,),
        ).fetchone()
    assert row is not None
    payload = _task_payload(store, task_id)
    assert TRUSTED_PLAN_KEY in payload
    assert CHECKPOINT_HEAD_KEY in payload


def test_canonical_raw_n_plus_one_rewrite_cannot_replace_admitted_head(tmp_path) -> None:
    store, binding, coordinator, task_id = _setup(tmp_path)
    _, durable_n = _persist_repair_required(store, binding, coordinator, task_id)
    head_before = _task_payload(store, task_id)[CHECKPOINT_HEAD_KEY]

    repair = coordinator.prepare_repair(
        "core",
        base_sha=SHA_B,
        reason="retry on independently selected newer base",
    )
    snapshot = coordinator.snapshot()
    assert snapshot.records[0].request.attempt == 2
    assert snapshot.records[0].state is WorkState.READY

    with store.connection() as conn:
        row = conn.execute(
            "SELECT payload_json FROM checkpoints WHERE checkpoint_id = ?",
            (durable_n.checkpoint_id,),
        ).fetchone()
        assert row is not None
        payload = json.loads(row["payload_json"])
        coordinator_payload = payload["coordinator"]
        record_payload = coordinator_payload["records"][0]
        coordinator_payload["revision"] = snapshot.revision
        record_payload["request"] = _request_payload(repair)
        record_payload["state"] = WorkState.READY.value
        record_payload["result"] = None
        record_payload["review"] = None
        record_payload["blocker"] = None
        forged_json = _canonical(payload)
        forged_checksum = _sha256(forged_json)
        forged_id = _checkpoint_id_from_payload(
            host_task_id=task_id,
            payload=payload,
            checksum=forged_checksum,
        )
        conn.execute(
            """
            UPDATE checkpoints
            SET checkpoint_id = ?, payload_json = ?, checksum_sha256 = ?, created_at = ?
            WHERE checkpoint_id = ?
            """,
            (
                forged_id,
                forged_json,
                forged_checksum,
                "2099-01-01T00:00:00+00:00",
                durable_n.checkpoint_id,
            ),
        )

    assert _task_payload(store, task_id)[CHECKPOINT_HEAD_KEY] == head_before
    restarted_store, restarted_binding = _restart(store)
    with pytest.raises(
        ProductFactoryCheckpointIntegrityError,
        match="uncommitted successor|head row is missing|head points to a missing",
    ):
        ProductFactoryCheckpointHost(restarted_store).restore_latest(
            host_task_id=task_id,
            binding=restarted_binding,
        )


def test_existing_checkpoint_rows_without_host_head_fail_closed(tmp_path) -> None:
    store, binding, coordinator, task_id = _setup(tmp_path)
    host = ProductFactoryCheckpointHost(store)
    host.save(host_task_id=task_id, checkpoint=binding.checkpoint(coordinator))
    with store.connection() as conn:
        row = conn.execute(
            "SELECT payload_json FROM tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        payload = json.loads(row["payload_json"])
        payload.pop(CHECKPOINT_HEAD_KEY)
        conn.execute(
            "UPDATE tasks SET payload_json = ? WHERE task_id = ?",
            (_canonical(payload), task_id),
        )

    restarted_store, _ = _restart(store)
    restarted_host = ProductFactoryCheckpointHost(restarted_store)
    with pytest.raises(
        ProductFactoryCheckpointIntegrityError,
        match="no canonical host-task head",
    ):
        restarted_host.latest(host_task_id=task_id, project_id=PROJECT_ID)


def test_tampered_host_head_checksum_fails_closed(tmp_path) -> None:
    store, binding, coordinator, task_id = _setup(tmp_path)
    host = ProductFactoryCheckpointHost(store)
    host.save(host_task_id=task_id, checkpoint=binding.checkpoint(coordinator))
    with store.connection() as conn:
        row = conn.execute(
            "SELECT payload_json FROM tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        payload = json.loads(row["payload_json"])
        payload[CHECKPOINT_HEAD_KEY]["checksum_sha256"] = "0" * 64
        conn.execute(
            "UPDATE tasks SET payload_json = ? WHERE task_id = ?",
            (_canonical(payload), task_id),
        )

    with pytest.raises(
        ProductFactoryCheckpointIntegrityError,
        match="head checksum does not match durable row",
    ):
        host.latest(host_task_id=task_id, project_id=PROJECT_ID)
