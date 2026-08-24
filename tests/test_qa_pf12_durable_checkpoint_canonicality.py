from __future__ import annotations

import hashlib
import json

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.product_factory_checkpoint_host import (
    ProductFactoryCheckpointHost,
    ProductFactoryCheckpointIntegrityError,
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
FAILURE_CANARY = "QA_NIKA50_PF12_FAILURE_CANARY_42A7"
RECOVERY_CANARY = "QA_NIKA50_PF12_RECOVERY_CANARY_91D3"


def _canonical(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _setup(tmp_path):
    store = SQLiteStore(tmp_path / "qa-pf12-canonicality.db")
    store.initialize()
    project = ProductProjectRepository(store).create(
        project_id="qa-pf12-project",
        name="PF12 canonicality QA",
        spec=ProductProjectSpec(
            goal="Build a restart-safe product",
            desired_outcome="Reject non-canonical durable checkpoint bytes",
            requirements=(
                ProductRequirement(
                    "req-1",
                    "Durable checkpoints fail closed on tamper",
                    ("Non-canonical durable bytes are rejected",),
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
                    opaque_token="runtime-only-recovery-state",
                ),
                failure=WorkerFailure(
                    kind=WorkerFailureKind.PROCESS_FAILED,
                    message="runtime-only worker diagnostic",
                    retryable=True,
                ),
            ),
        )
    )
    task = TaskQueue(store).create(
        workspace_id="qa-pf12-workspace",
        agent_id="product-factory",
        payload={
            "kind": "product_factory",
            "product_project_id": project.project_id,
        },
    )
    host = ProductFactoryCheckpointHost(store)
    saved = host.save(
        host_task_id=task.task_id,
        checkpoint=binding.checkpoint(coordinator),
    )
    return store, task.task_id, saved.checkpoint_id


def _derived_checkpoint_id(
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


def _restart(store: SQLiteStore) -> ProductFactoryCheckpointHost:
    restarted = SQLiteStore(store.path)
    restarted.initialize()
    return ProductFactoryCheckpointHost(restarted)


def test_noncanonical_secret_bearing_durable_bytes_fail_closed(tmp_path) -> None:
    store, task_id, checkpoint_id = _setup(tmp_path)

    with store.connection() as conn:
        row = conn.execute(
            "SELECT payload_json FROM checkpoints WHERE checkpoint_id = ?",
            (checkpoint_id,),
        ).fetchone()
        assert row is not None
        payload = json.loads(str(row["payload_json"]))
        coding_result = payload["coordinator"]["records"][0]["result"]["coding_result"]
        coding_result["failure"]["message"] = (
            "subprocess failed with api_key=" + FAILURE_CANARY
        )
        coding_result["recovery_state"]["opaque_token"] = (
            "access_token=" + RECOVERY_CANARY
        )
        raw = _canonical(payload)
        checksum = _sha256(raw)
        tampered_id = _derived_checkpoint_id(task_id, payload, checksum)
        conn.execute(
            """
            UPDATE checkpoints
            SET checkpoint_id = ?, payload_json = ?, checksum_sha256 = ?
            WHERE checkpoint_id = ?
            """,
            (tampered_id, raw, checksum, checkpoint_id),
        )

    assert FAILURE_CANARY in raw
    assert RECOVERY_CANARY in raw
    with pytest.raises(
        ProductFactoryCheckpointIntegrityError,
        match="canonical|checkpoint|durable|integrity",
    ):
        _restart(store).load(tampered_id, host_task_id=task_id)


def test_checkpoint_id_rebinding_fails_closed(tmp_path) -> None:
    store, task_id, checkpoint_id = _setup(tmp_path)
    rebound_id = "pf2-" + ("f" * 32)
    assert rebound_id != checkpoint_id

    with store.connection() as conn:
        conn.execute(
            "UPDATE checkpoints SET checkpoint_id = ? WHERE checkpoint_id = ?",
            (rebound_id, checkpoint_id),
        )

    with pytest.raises(
        ProductFactoryCheckpointIntegrityError,
        match="checkpoint|identity|integrity",
    ):
        _restart(store).load(rebound_id, host_task_id=task_id)
