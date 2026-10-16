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
DIGEST_D = "d" * 64
PERMISSIONS = frozenset({"read_source", "write_source", "run_tests"})
LOCATOR = "org/repo"


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


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
        ),
    )


def _setup(tmp_path):
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    projects = ProductProjectRepository(store)
    project = projects.create(
        project_id="p1",
        name="Product",
        spec=ProductProjectSpec(
            goal="Build accessible product",
            desired_outcome="A reviewed product",
            requirements=(
                ProductRequirement(
                    "req-1",
                    "Keyboard operation",
                    ("All primary actions keyboard reachable",),
                ),
            ),
            repository_refs=(LOCATOR,),
        ),
        idempotency_key="create:p1",
    )
    binding = ProductProjectCoordinatorBinding(project, _graph())
    task = TaskQueue(store).create(
        workspace_id="ws-product",
        agent_id="product-factory",
        payload={"kind": "product_factory", "product_project_id": "p1"},
    )
    coordinator = binding.plan(
        base_shas={"repo-1": SHA_A},
        component_goals={"core": "build core"},
        permission_ceiling=PERMISSIONS,
    )
    return store, binding, task.task_id, coordinator


def _restarted_binding(store: SQLiteStore) -> ProductProjectCoordinatorBinding:
    project = ProductProjectRepository(store).get("p1")
    return ProductProjectCoordinatorBinding(project, _graph())


def test_restart_rejects_secret_bearing_noncanonical_checkpoint_bytes(tmp_path) -> None:
    store, binding, task_id, coordinator = _setup(tmp_path)
    request = coordinator.start("core")
    coordinator.record_result(
        WorkerResultEnvelope(
            work_id=request.work_id,
            component_id="core",
            repository_id="repo-1",
            base_sha=SHA_A,
            result_sha=SHA_B,
            diff_digest=DIGEST_D,
            coding_result=CodingResult(
                job_id=request.work_id,
                recovery_state=RecoveryState("failed", "runtime-only-token"),
                failure=WorkerFailure(
                    WorkerFailureKind.PROCESS_FAILED,
                    "runtime-only failure detail",
                    retryable=True,
                ),
            ),
        )
    )
    host = ProductFactoryCheckpointHost(store)
    saved = host.save(host_task_id=task_id, checkpoint=binding.checkpoint(coordinator))
    failure_canary = "api_key=NIKA_DURABLE_READ_FAILURE_CANARY"
    token_canary = "access_token=NIKA_DURABLE_READ_TOKEN_CANARY"

    with store.connection() as conn:
        row = conn.execute(
            "SELECT payload_json FROM checkpoints WHERE checkpoint_id=?",
            (saved.checkpoint_id,),
        ).fetchone()
        payload = json.loads(row["payload_json"])
        record = next(
            item
            for item in payload["coordinator"]["records"]
            if item["request"]["component_id"] == "core"
        )
        record["blocker"] = failure_canary
        record["result"]["coding_result"]["failure"]["message"] = failure_canary
        record["result"]["coding_result"]["recovery_state"]["opaque_token"] = token_canary
        raw = _canonical(payload)
        conn.execute(
            "UPDATE checkpoints SET payload_json=?, checksum_sha256=? "
            "WHERE checkpoint_id=?",
            (raw, _sha256(raw), saved.checkpoint_id),
        )

    restarted_store = SQLiteStore(store.path)
    restarted_store.initialize()
    restarted_host = ProductFactoryCheckpointHost(restarted_store)
    with pytest.raises(ProductFactoryCheckpointIntegrityError, match="not canonical"):
        restarted_host.load(saved.checkpoint_id)

    candidate = restarted_host.inspect_latest(
        host_task_id=task_id,
        binding=_restarted_binding(restarted_store),
    )
    assert candidate.disposition is ProductFactoryRecoveryDisposition.CORRUPT
    with restarted_store.connection() as conn:
        raw_after = conn.execute(
            "SELECT payload_json FROM checkpoints WHERE checkpoint_id=?",
            (saved.checkpoint_id,),
        ).fetchone()["payload_json"]
    assert failure_canary in raw_after
    assert token_canary in raw_after


def test_restart_rejects_canonical_payload_with_stale_checkpoint_identity(tmp_path) -> None:
    store, binding, task_id, coordinator = _setup(tmp_path)
    host = ProductFactoryCheckpointHost(store)
    saved = host.save(host_task_id=task_id, checkpoint=binding.checkpoint(coordinator))

    with store.connection() as conn:
        row = conn.execute(
            "SELECT payload_json FROM checkpoints WHERE checkpoint_id=?",
            (saved.checkpoint_id,),
        ).fetchone()
        payload = json.loads(row["payload_json"])
        payload["coordinator"]["revision"] += 1
        raw = _canonical(payload)
        conn.execute(
            "UPDATE checkpoints SET payload_json=?, checksum_sha256=? "
            "WHERE checkpoint_id=?",
            (raw, _sha256(raw), saved.checkpoint_id),
        )

    restarted_store = SQLiteStore(store.path)
    restarted_store.initialize()
    restarted_host = ProductFactoryCheckpointHost(restarted_store)
    with pytest.raises(ProductFactoryCheckpointIntegrityError, match="identity"):
        restarted_host.load(saved.checkpoint_id)

    candidate = restarted_host.inspect_latest(
        host_task_id=task_id,
        binding=_restarted_binding(restarted_store),
    )
    assert candidate.disposition is ProductFactoryRecoveryDisposition.CORRUPT
