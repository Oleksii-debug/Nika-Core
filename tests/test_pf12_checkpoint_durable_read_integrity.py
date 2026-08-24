from __future__ import annotations

import hashlib
import json

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.product_factory_checkpoint_host import (
    ProductFactoryCheckpointHost,
    ProductFactoryCheckpointIntegrityError,
    ProductFactoryRecoveryDisposition,
)
from nika_core.product_factory_coordinator import WorkerResultEnvelope
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
from nika_core.toolsmith.contracts import (
    CodingResult,
    RecoveryState,
    WorkerFailure,
    WorkerFailureKind,
)

SHA_A = "a" * 40
SHA_B = "b" * 40
DIGEST = "d" * 64
PERMISSIONS = frozenset({"read_source", "write_source", "run_tests"})
OMITTED_DIAGNOSTIC = "worker diagnostic omitted from durable checkpoint"


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _setup(tmp_path):
    store = SQLiteStore(tmp_path / "pf12-read-integrity.db")
    store.initialize()
    project = ProductProjectRepository(store).create(
        project_id="pf12-read-integrity",
        name="PF12 read integrity",
        spec=ProductProjectSpec(
            goal="Build restart-safe product",
            desired_outcome="Checkpoint reads fail closed on forged durable bytes",
            requirements=(
                ProductRequirement(
                    "req-1",
                    "durable integrity",
                    ("canonical checkpoint bytes",),
                ),
            ),
            repository_refs=("org/repo",),
        ),
        idempotency_key="pf12-read-integrity:create",
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
        workspace_id="pf12-read-integrity-workspace",
        agent_id="product-factory",
        payload={"kind": "product_factory", "product_project_id": project.project_id},
    )
    return store, binding, coordinator, task.task_id


def _save_failed_checkpoint(tmp_path):
    store, binding, coordinator, task_id = _setup(tmp_path)
    request = coordinator.start("core")
    coordinator.record_result(
        WorkerResultEnvelope(
            work_id=request.work_id,
            component_id=request.component_id,
            repository_id=request.repository_id,
            base_sha=request.base_sha,
            result_sha=SHA_B,
            diff_digest=DIGEST,
            coding_result=CodingResult(
                job_id=request.work_id,
                recovery_state=RecoveryState(
                    phase="failed",
                    opaque_token="runtime-only-token",
                ),
                failure=WorkerFailure(
                    kind=WorkerFailureKind.PROCESS_FAILED,
                    message="runtime-only failure detail",
                    retryable=True,
                ),
            ),
        )
    )
    host = ProductFactoryCheckpointHost(store)
    saved = host.save(
        host_task_id=task_id,
        checkpoint=binding.checkpoint(coordinator),
    )
    return store, binding, host, task_id, saved


def test_restart_rejects_rechecksummed_noncanonical_worker_diagnostics(tmp_path) -> None:
    failure_canary = "PF12_READ_CANARY_FAILURE_42A7"
    recovery_canary = "PF12_READ_CANARY_RECOVERY_91D3"
    store, binding, host, task_id, saved = _save_failed_checkpoint(tmp_path)

    with store.connection() as conn:
        row = conn.execute(
            """
            SELECT payload_json
            FROM checkpoints
            WHERE checkpoint_id = ?
            """,
            (saved.checkpoint_id,),
        ).fetchone()
        assert row is not None
        payload = json.loads(str(row["payload_json"]))
        coding_result = payload["coordinator"]["records"][0]["result"]["coding_result"]
        assert coding_result["failure"]["message"] == OMITTED_DIAGNOSTIC
        assert coding_result["recovery_state"]["opaque_token"] is None

        coding_result["failure"]["message"] = "api_key=" + failure_canary
        coding_result["recovery_state"]["opaque_token"] = "access_token=" + recovery_canary
        forged_payload = _canonical(payload)
        conn.execute(
            """
            UPDATE checkpoints
            SET payload_json = ?, checksum_sha256 = ?
            WHERE checkpoint_id = ?
            """,
            (forged_payload, _sha256(forged_payload), saved.checkpoint_id),
        )

    with store.connection() as conn:
        raw = str(
            conn.execute(
                "SELECT payload_json FROM checkpoints WHERE checkpoint_id = ?",
                (saved.checkpoint_id,),
            ).fetchone()[0]
        )
    assert failure_canary in raw
    assert recovery_canary in raw

    candidate = host.inspect_latest(host_task_id=task_id, binding=binding)

    assert candidate.disposition is ProductFactoryRecoveryDisposition.CORRUPT
    assert candidate.checkpoint_id is None


def test_load_rejects_checkpoint_id_not_bound_to_payload_and_checksum(tmp_path) -> None:
    store, _binding, host, task_id, saved = _save_failed_checkpoint(tmp_path)
    forged_checkpoint_id = "pf2-" + ("0" * 32)
    assert forged_checkpoint_id != saved.checkpoint_id

    with store.connection() as conn:
        conn.execute(
            """
            UPDATE checkpoints
            SET checkpoint_id = ?
            WHERE checkpoint_id = ?
            """,
            (forged_checkpoint_id, saved.checkpoint_id),
        )

    with pytest.raises(ProductFactoryCheckpointIntegrityError):
        host.load(forged_checkpoint_id, host_task_id=task_id)
