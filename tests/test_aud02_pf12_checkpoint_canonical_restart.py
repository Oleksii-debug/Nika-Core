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

_SHA_A = "a" * 40
_SHA_B = "b" * 40
_DIGEST = "d" * 64
_LOCATOR = "org/repo"
_PERMISSIONS = frozenset({"read_source", "write_source", "run_tests"})
_FAILURE_CANARY = "api_key=NIKA_QA_PF12_RAW_FAILURE_CANARY"
_TOKEN_CANARY = "access_token=NIKA_QA_PF12_RAW_TOKEN_CANARY"


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _checksum(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _spec() -> ProductProjectSpec:
    return ProductProjectSpec(
        goal="Build accessible product",
        desired_outcome="A reviewed product",
        requirements=(
            ProductRequirement(
                "req-1",
                "Keyboard operation",
                ("All primary actions keyboard reachable",),
            ),
        ),
        repository_refs=(_LOCATOR,),
    )


def _graph() -> ProductRepositoryGraph:
    return ProductRepositoryGraph(
        project_id="p1",
        repositories=(RepositoryRef("repo-1", "github", _LOCATOR, "main"),),
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
    project = ProductProjectRepository(store).create(
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
    return store, binding, task.task_id


def _planned(binding: ProductProjectCoordinatorBinding):
    return binding.plan(
        base_shas={"repo-1": _SHA_A},
        component_goals={"core": "build core"},
        permission_ceiling=_PERMISSIONS,
    )


def _rewrite_checkpoint_row(
    store: SQLiteStore,
    checkpoint_id: str,
    mutate,
) -> str:
    with store.connection() as conn:
        row = conn.execute(
            "SELECT payload_json FROM checkpoints WHERE checkpoint_id = ?",
            (checkpoint_id,),
        ).fetchone()
        payload = json.loads(row["payload_json"])
        mutate(payload)
        canonical = _canonical(payload)
        conn.execute(
            """
            UPDATE checkpoints
            SET payload_json = ?, checksum_sha256 = ?
            WHERE checkpoint_id = ?
            """,
            (canonical, _checksum(canonical), checkpoint_id),
        )
    return canonical


def test_restart_rejects_rechecksummed_noncanonical_secret_bearing_payload(tmp_path) -> None:
    store, binding, task_id = _setup(tmp_path)
    coordinator = _planned(binding)
    request = coordinator.start("core")
    coordinator.record_result(
        WorkerResultEnvelope(
            work_id=request.work_id,
            component_id="core",
            repository_id="repo-1",
            base_sha=_SHA_A,
            result_sha=_SHA_B,
            diff_digest=_DIGEST,
            coding_result=CodingResult(
                job_id=request.work_id,
                recovery_state=RecoveryState("failed", _TOKEN_CANARY),
                failure=WorkerFailure(
                    WorkerFailureKind.PROCESS_FAILED,
                    _FAILURE_CANARY,
                    retryable=True,
                ),
            ),
        )
    )
    host = ProductFactoryCheckpointHost(store)
    saved = host.save(host_task_id=task_id, checkpoint=binding.checkpoint(coordinator))

    def reintroduce_raw_diagnostics(payload) -> None:
        record = payload["coordinator"]["records"][0]
        coding_result = record["result"]["coding_result"]
        coding_result["failure"]["message"] = _FAILURE_CANARY
        coding_result["recovery_state"]["opaque_token"] = _TOKEN_CANARY
        record["blocker"] = _FAILURE_CANARY

    raw_payload = _rewrite_checkpoint_row(
        store,
        saved.checkpoint_id,
        reintroduce_raw_diagnostics,
    )
    assert _FAILURE_CANARY in raw_payload
    assert _TOKEN_CANARY in raw_payload

    restarted_store = SQLiteStore(store.path)
    restarted_store.initialize()
    restarted = ProductFactoryCheckpointHost(restarted_store)
    with pytest.raises(ProductFactoryCheckpointIntegrityError):
        restarted.load(saved.checkpoint_id, host_task_id=task_id)


def test_restart_rejects_stale_checkpoint_id_after_payload_and_checksum_rebind(tmp_path) -> None:
    store, binding, task_id = _setup(tmp_path)
    coordinator = _planned(binding)
    host = ProductFactoryCheckpointHost(store)
    saved = host.save(host_task_id=task_id, checkpoint=binding.checkpoint(coordinator))

    def advance_revision(payload) -> None:
        payload["coordinator"]["revision"] += 1

    _rewrite_checkpoint_row(store, saved.checkpoint_id, advance_revision)

    restarted_store = SQLiteStore(store.path)
    restarted_store.initialize()
    restarted = ProductFactoryCheckpointHost(restarted_store)
    with pytest.raises(ProductFactoryCheckpointIntegrityError):
        restarted.load(saved.checkpoint_id, host_task_id=task_id)
