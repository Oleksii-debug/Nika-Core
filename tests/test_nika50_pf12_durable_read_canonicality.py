from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

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

_SHA_A = "a" * 40
_SHA_B = "b" * 40
_DIFF_DIGEST = "d" * 64
_PERMISSIONS = frozenset({"read_source", "write_source", "run_tests"})
_FAILURE_CANARY = "NIKA50_PF12_RAW_FAILURE_CANARY_42A7"
_RECOVERY_CANARY = "NIKA50_PF12_RAW_RECOVERY_CANARY_91D3"


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _expected_checkpoint_id(
    host_task_id: str,
    payload: dict[str, Any],
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


def _setup(tmp_path: Path):
    store = SQLiteStore(tmp_path / "nika50-pf12.db")
    store.initialize()
    project = ProductProjectRepository(store).create(
        project_id="nika50-pf12-project",
        name="NIKA50 PF12",
        spec=ProductProjectSpec(
            goal="Build durable product",
            desired_outcome="Canonical restart-safe checkpoint state",
            requirements=(
                ProductRequirement(
                    "req-1",
                    "Durable checkpoint integrity",
                    ("tampered durable bytes fail closed on restart",),
                ),
            ),
            repository_refs=("org/repo",),
        ),
        idempotency_key="nika50:pf12:create",
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
        base_shas={"repo-1": _SHA_A},
        component_goals={"core": "build core"},
        permission_ceiling=_PERMISSIONS,
    )
    task = TaskQueue(store).create(
        workspace_id="nika50-pf12-workspace",
        agent_id="product-factory",
        payload={"kind": "product_factory", "product_project_id": project.project_id},
    )
    return store, binding, coordinator, task.task_id


def _save_failure_checkpoint(tmp_path: Path):
    store, binding, coordinator, task_id = _setup(tmp_path)
    request = coordinator.start("core")
    result = CodingResult(
        job_id=request.work_id,
        recovery_state=RecoveryState(
            phase="failed",
            opaque_token="live-only-recovery-diagnostic",
        ),
        failure=WorkerFailure(
            kind=WorkerFailureKind.PROCESS_FAILED,
            message="live-only worker failure detail",
            retryable=True,
        ),
    )
    coordinator.record_result(
        WorkerResultEnvelope(
            work_id=request.work_id,
            component_id="core",
            repository_id="repo-1",
            base_sha=_SHA_A,
            result_sha=_SHA_B,
            diff_digest=_DIFF_DIGEST,
            coding_result=result,
        )
    )
    host = ProductFactoryCheckpointHost(store)
    saved = host.save(
        host_task_id=task_id,
        checkpoint=binding.checkpoint(coordinator),
    )
    return store, binding, task_id, saved


def _inject_raw_worker_canaries(node: object) -> tuple[int, int]:
    failure_hits = 0
    recovery_hits = 0
    if isinstance(node, dict):
        if {"kind", "message", "retryable"}.issubset(node):
            node["message"] = _FAILURE_CANARY
            failure_hits += 1
        if {"phase", "opaque_token"}.issubset(node):
            node["opaque_token"] = _RECOVERY_CANARY
            recovery_hits += 1
        for value in node.values():
            child_failure, child_recovery = _inject_raw_worker_canaries(value)
            failure_hits += child_failure
            recovery_hits += child_recovery
    elif isinstance(node, list):
        for value in node:
            child_failure, child_recovery = _inject_raw_worker_canaries(value)
            failure_hits += child_failure
            recovery_hits += child_recovery
    return failure_hits, recovery_hits


def test_restart_rejects_checksum_and_id_consistent_noncanonical_worker_bytes(
    tmp_path: Path,
) -> None:
    store, binding, task_id, saved = _save_failure_checkpoint(tmp_path)
    with store.connection() as conn:
        row = conn.execute(
            "SELECT payload_json FROM checkpoints WHERE checkpoint_id = ?",
            (saved.checkpoint_id,),
        ).fetchone()
        assert row is not None
        payload = json.loads(str(row["payload_json"]))
        assert isinstance(payload, dict)
        failure_hits, recovery_hits = _inject_raw_worker_canaries(payload)
        assert failure_hits == 1
        assert recovery_hits == 1
        canonical = _canonical(payload)
        checksum = _sha256(canonical)
        rebound_id = _expected_checkpoint_id(task_id, payload, checksum)
        conn.execute(
            """
            UPDATE checkpoints
            SET checkpoint_id = ?, payload_json = ?, checksum_sha256 = ?
            WHERE checkpoint_id = ?
            """,
            (rebound_id, canonical, checksum, saved.checkpoint_id),
        )

    restarted = ProductFactoryCheckpointHost(SQLiteStore(store.path))
    with pytest.raises(ProductFactoryCheckpointIntegrityError):
        restarted.latest(
            host_task_id=task_id,
            project_id=binding.project.project_id,
        )


def test_restart_rejects_checkpoint_id_rebound_away_from_canonical_identity(
    tmp_path: Path,
) -> None:
    store, binding, task_id, saved = _save_failure_checkpoint(tmp_path)
    forged_id = "pf2-" + "f" * 32
    assert forged_id != saved.checkpoint_id
    with store.connection() as conn:
        conn.execute(
            "UPDATE checkpoints SET checkpoint_id = ? WHERE checkpoint_id = ?",
            (forged_id, saved.checkpoint_id),
        )

    restarted = ProductFactoryCheckpointHost(SQLiteStore(store.path))
    with pytest.raises(ProductFactoryCheckpointIntegrityError):
        restarted.latest(
            host_task_id=task_id,
            project_id=binding.project.project_id,
        )


def test_canonical_sanitized_checkpoint_still_roundtrips_after_restart(
    tmp_path: Path,
) -> None:
    store, binding, task_id, saved = _save_failure_checkpoint(tmp_path)
    restarted = ProductFactoryCheckpointHost(SQLiteStore(store.path))
    restored = restarted.latest(
        host_task_id=task_id,
        project_id=binding.project.project_id,
    )

    assert restored is not None
    assert restored.checkpoint_id == saved.checkpoint_id
    assert restored.checksum_sha256 == saved.checksum_sha256
