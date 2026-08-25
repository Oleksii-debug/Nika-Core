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


def _setup(tmp_path):
    store = SQLiteStore(tmp_path / "qa53-pf12.db")
    store.initialize()
    project = ProductProjectRepository(store).create(
        project_id="qa53-project",
        name="QA53",
        spec=ProductProjectSpec(
            goal="Build safe product",
            desired_outcome="No durable credential leakage",
            requirements=(
                ProductRequirement("req-1", "safe", ("secret-free checkpoints",)),
            ),
            repository_refs=("org/repo",),
        ),
        idempotency_key="qa53:create",
    )
    graph = ProductRepositoryGraph(
        project_id="qa53-project",
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
        workspace_id="qa53-workspace",
        agent_id="product-factory",
        payload={"kind": "product_factory", "product_project_id": "qa53-project"},
    )
    return store, binding, coordinator, task.task_id


def _record_failed_worker_result(coordinator, *, failure_canary: str, recovery_canary: str):
    request = coordinator.start("core")
    result = CodingResult(
        job_id=request.work_id,
        recovery_state=RecoveryState(
            phase="failed",
            opaque_token="access_token=" + recovery_canary,
        ),
        failure=WorkerFailure(
            kind=WorkerFailureKind.PROCESS_FAILED,
            message="subprocess failed with api_key=" + failure_canary,
            retryable=True,
        ),
    )
    coordinator.record_result(
        WorkerResultEnvelope(
            work_id=request.work_id,
            component_id="core",
            repository_id="repo-1",
            base_sha=SHA_A,
            result_sha=SHA_B,
            diff_digest=DIGEST,
            coding_result=result,
        )
    )


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _saved_checkpoint_with_worker_failure(tmp_path):
    failure_canary = "QA53_CANARY_CHECKPOINT_FAILURE_42A7"
    recovery_canary = "QA53_CANARY_CHECKPOINT_RECOVERY_91D3"
    store, binding, coordinator, task_id = _setup(tmp_path)
    _record_failed_worker_result(
        coordinator,
        failure_canary=failure_canary,
        recovery_canary=recovery_canary,
    )
    host = ProductFactoryCheckpointHost(store)
    saved = host.save(
        host_task_id=task_id,
        checkpoint=binding.checkpoint(coordinator),
    )
    return store, host, saved, task_id, failure_canary, recovery_canary


def _raw_checkpoint(store, checkpoint_id: str) -> tuple[str, str]:
    with store.connection() as conn:
        row = conn.execute(
            "SELECT payload_json, checksum_sha256 FROM checkpoints WHERE checkpoint_id = ?",
            (checkpoint_id,),
        ).fetchone()
    assert row is not None
    return str(row["payload_json"]), str(row["checksum_sha256"])


def _replace_checkpoint_bytes(
    store,
    *,
    checkpoint_id: str,
    payload_json: str,
    checksum_sha256: str,
) -> None:
    with store.connection() as conn:
        conn.execute(
            """
            UPDATE checkpoints
            SET payload_json = ?, checksum_sha256 = ?
            WHERE checkpoint_id = ?
            """,
            (payload_json, checksum_sha256, checkpoint_id),
        )


def test_qa53_worker_failure_and_recovery_tokens_never_reach_durable_checkpoint(
    tmp_path,
) -> None:
    (
        store,
        _,
        saved,
        _,
        failure_canary,
        recovery_canary,
    ) = _saved_checkpoint_with_worker_failure(tmp_path)

    raw, _ = _raw_checkpoint(store, saved.checkpoint_id)

    assert failure_canary not in raw
    assert recovery_canary not in raw


def test_qa53_restart_rejects_secret_bearing_durable_tamper_with_valid_checksum(
    tmp_path,
) -> None:
    (
        store,
        host,
        saved,
        task_id,
        _,
        _,
    ) = _saved_checkpoint_with_worker_failure(tmp_path)
    durable_failure_canary = "QA53_DURABLE_TAMPER_FAILURE_D832"
    durable_recovery_canary = "QA53_DURABLE_TAMPER_RECOVERY_7F1B"

    raw, _ = _raw_checkpoint(store, saved.checkpoint_id)
    payload = json.loads(raw)
    coding_result = payload["coordinator"]["records"][0]["result"]["coding_result"]
    assert coding_result["failure"] is not None
    assert coding_result["recovery_state"] is not None
    coding_result["failure"]["message"] = "api_key=" + durable_failure_canary
    coding_result["recovery_state"]["opaque_token"] = (
        "access_token=" + durable_recovery_canary
    )
    tampered = _canonical(payload)
    tampered_checksum = _sha256(tampered)
    _replace_checkpoint_bytes(
        store,
        checkpoint_id=saved.checkpoint_id,
        payload_json=tampered,
        checksum_sha256=tampered_checksum,
    )

    persisted, persisted_checksum = _raw_checkpoint(store, saved.checkpoint_id)
    assert durable_failure_canary in persisted
    assert durable_recovery_canary in persisted
    assert persisted_checksum == tampered_checksum

    with pytest.raises(ProductFactoryCheckpointIntegrityError):
        host.load(saved.checkpoint_id, host_task_id=task_id)


def test_qa53_restart_rejects_stale_checkpoint_id_after_canonicality_tamper(
    tmp_path,
) -> None:
    store, host, saved, task_id, _, _ = _saved_checkpoint_with_worker_failure(tmp_path)
    raw, _ = _raw_checkpoint(store, saved.checkpoint_id)

    # Change only the durable byte representation, not its decoded semantics. A valid
    # checksum does not authorize non-canonical bytes, and because checkpoint identity
    # binds the checksum, the pre-existing checkpoint_id must no longer be accepted.
    noncanonical = json.dumps(
        json.loads(raw),
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    )
    assert noncanonical != raw
    tampered_checksum = _sha256(noncanonical)
    _replace_checkpoint_bytes(
        store,
        checkpoint_id=saved.checkpoint_id,
        payload_json=noncanonical,
        checksum_sha256=tampered_checksum,
    )

    with pytest.raises(ProductFactoryCheckpointIntegrityError):
        host.load(saved.checkpoint_id, host_task_id=task_id)
