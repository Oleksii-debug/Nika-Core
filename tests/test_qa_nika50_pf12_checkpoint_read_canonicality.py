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
from nika_core.product_factory_coordinator import ReviewDecision, WorkerResultEnvelope
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
    ChangedFile,
    CodingResult,
    RecoveryState,
    TestEvidence,
    WorkerFailure,
    WorkerFailureKind,
)

SHA_A = "a" * 40
SHA_B = "b" * 40
DIFF_DIGEST = "d" * 64
TEST_DIGEST = "e" * 64
PERMISSIONS = frozenset({"read_source", "write_source", "run_tests"})
STAGE = "product_factory.coordinator.v1"


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _independent_checkpoint_id(
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


def _graph() -> ProductRepositoryGraph:
    return ProductRepositoryGraph(
        project_id="qa-pf12-project",
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


def _setup(tmp_path):
    store = SQLiteStore(tmp_path / "qa-pf12-read-canonicality.db")
    store.initialize()
    project = ProductProjectRepository(store).create(
        project_id="qa-pf12-project",
        name="PF12 read canonicality",
        spec=ProductProjectSpec(
            goal="Build a durable product",
            desired_outcome="Canonical restart authority",
            requirements=(
                ProductRequirement(
                    "req-1",
                    "Reject non-canonical checkpoint bytes",
                    ("restart fails closed",),
                ),
            ),
            repository_refs=("org/repo",),
        ),
        idempotency_key="qa-pf12:create",
    )
    binding = ProductProjectCoordinatorBinding(project, _graph())
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
    return store, binding, coordinator, task.task_id


def _restart(store: SQLiteStore) -> SQLiteStore:
    restarted = SQLiteStore(store.path)
    restarted.initialize()
    return restarted


def test_secret_bearing_noncanonical_bytes_fail_closed_even_with_matching_id(tmp_path) -> None:
    failure_canary = "QA_NIKA50_FAILURE_CANARY_61A2"
    recovery_canary = "QA_NIKA50_RECOVERY_CANARY_93D4"
    store, binding, coordinator, task_id = _setup(tmp_path)
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
                recovery_state=RecoveryState("failed", "runtime-token"),
                failure=WorkerFailure(
                    WorkerFailureKind.PROCESS_FAILED,
                    "runtime diagnostic",
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

    with store.connection() as conn:
        row = conn.execute(
            "SELECT payload_json FROM checkpoints WHERE checkpoint_id = ?",
            (saved.checkpoint_id,),
        ).fetchone()
        payload = json.loads(row["payload_json"])
        coding_result = payload["coordinator"]["records"][0]["result"]["coding_result"]
        coding_result["failure"]["message"] = "api_key=" + failure_canary
        coding_result["recovery_state"]["opaque_token"] = (
            "access_token=" + recovery_canary
        )
        canonical = _canonical(payload)
        checksum = _sha256(canonical)
        forged_id = _independent_checkpoint_id(task_id, payload, checksum)
        assert forged_id != saved.checkpoint_id
        conn.execute(
            """
            UPDATE checkpoints
            SET checkpoint_id = ?, payload_json = ?, checksum_sha256 = ?
            WHERE checkpoint_id = ?
            """,
            (forged_id, canonical, checksum, saved.checkpoint_id),
        )

    with store.connection() as conn:
        raw = conn.execute(
            "SELECT payload_json FROM checkpoints WHERE checkpoint_id = ?",
            (forged_id,),
        ).fetchone()["payload_json"]
    assert failure_canary in raw
    assert recovery_canary in raw

    restarted = _restart(store)
    with pytest.raises(ProductFactoryCheckpointIntegrityError):
        ProductFactoryCheckpointHost(restarted).latest(
            host_task_id=task_id,
            project_id="qa-pf12-project",
        )


def test_canonical_payload_change_cannot_reuse_stale_checkpoint_id(tmp_path) -> None:
    store, binding, coordinator, task_id = _setup(tmp_path)
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
                changed_files=(ChangedFile("src/core/main.py", DIFF_DIGEST, 123),),
                test_evidence=(
                    TestEvidence(
                        command=("python", "-m", "pytest", "tests/core"),
                        exit_code=0,
                        output_digest=TEST_DIGEST,
                    ),
                ),
            ),
        )
    )
    coordinator.review(
        "core",
        ReviewDecision(
            reviewer_id="independent-qa",
            accepted=True,
            reason="original review reason",
            evidence_refs=("qa:review-1",),
        ),
    )
    host = ProductFactoryCheckpointHost(store)
    saved = host.save(
        host_task_id=task_id,
        checkpoint=binding.checkpoint(coordinator),
    )

    with store.connection() as conn:
        row = conn.execute(
            "SELECT payload_json FROM checkpoints WHERE checkpoint_id = ?",
            (saved.checkpoint_id,),
        ).fetchone()
        payload = json.loads(row["payload_json"])
        payload["coordinator"]["records"][0]["review"]["reason"] = (
            "tampered but structurally valid review reason"
        )
        canonical = _canonical(payload)
        checksum = _sha256(canonical)
        replacement_id = _independent_checkpoint_id(task_id, payload, checksum)
        assert replacement_id != saved.checkpoint_id
        conn.execute(
            """
            UPDATE checkpoints
            SET payload_json = ?, checksum_sha256 = ?
            WHERE checkpoint_id = ? AND stage = ?
            """,
            (canonical, checksum, saved.checkpoint_id, STAGE),
        )

    restarted = _restart(store)
    with pytest.raises(ProductFactoryCheckpointIntegrityError):
        ProductFactoryCheckpointHost(restarted).load(
            saved.checkpoint_id,
            host_task_id=task_id,
        )
