from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.product_factory_checkpoint_host import ProductFactoryCheckpointHost
from nika_core.product_factory_coordinator import WorkerResultEnvelope
from nika_core.product_factory_multi_repository import (
    MultiRepositoryProductFactoryHost,
    RepairLineageError,
    RepositoryGraphIntegrityError,
)
from nika_core.product_factory_orchestration import (
    ProductComponent,
    ProductRepositoryGraph,
    RepositoryRef,
)
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

_GRAPH_STAGE = "product_factory.repository_graph.v1"
_LINEAGE_STAGE = "product_factory.repair_lineage.v1"
_GRAPH_VERSION = 7
_PERMISSIONS = frozenset({"read_source", "write_source", "run_tests"})


class _NoopProgramWorker:
    async def dispatch(self, request):
        raise AssertionError(f"unexpected dispatch: {request.work_id}")

    async def inspect(self, work_id: str) -> RecoveryState | None:
        del work_id
        return None

    async def recover(self, request, state: RecoveryState):
        raise AssertionError(f"unexpected recovery: {request.work_id}:{state.phase}")


def _canonical(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha(value: str) -> str:
    return _sha256(value)[:40]


def _create_state(tmp_path: Path):
    store = SQLiteStore(tmp_path / "durable-schema.db")
    store.initialize()
    graph = ProductRepositoryGraph(
        project_id="project:durable-schema",
        repositories=(
            RepositoryRef("repo:a", "github", "owner/a", "main"),
            RepositoryRef("repo:b", "github", "owner/b", "main"),
        ),
        components=(
            ProductComponent("a", "repo:a", ("src/a",)),
            ProductComponent("b", "repo:b", ("src/b",), dependencies=("a",)),
        ),
    )
    project = ProductProjectRepository(store).create(
        project_id=graph.project_id,
        name="Durable Schema Product",
        spec=ProductProjectSpec(
            goal="Prove strict multi-repository durable schema validation",
            desired_outcome="Coercive durable type rewrites fail closed",
            requirements=(
                ProductRequirement(
                    requirement_id="strict-schema",
                    text="Repository graph and repair lineage retain exact durable types",
                    acceptance=("Type-coercion tamper fails closed",),
                ),
            ),
            repository_refs=("owner/a", "owner/b"),
        ),
        idempotency_key="create:durable-schema",
    )
    task = TaskQueue(store).create(
        workspace_id="ws:durable-schema",
        agent_id="product-factory",
        payload={
            "kind": "product_factory",
            "product_project_id": project.project_id,
        },
    )
    host = MultiRepositoryProductFactoryHost(store, _NoopProgramWorker())
    state = host.initialize(
        host_task_id=task.task_id,
        project=project,
        graph=graph,
        graph_version=_GRAPH_VERSION,
        base_shas={"repo:a": "a" * 40, "repo:b": "b" * 40},
        component_goals={"a": "Implement A", "b": "Implement B"},
        permission_ceiling=_PERMISSIONS,
    )
    return store, project, task.task_id, host, state


def _rewrite_checkpoint_payload(
    store: SQLiteStore,
    *,
    task_id: str,
    stage: str,
    mutate,
) -> None:
    with store.connection() as conn:
        row = conn.execute(
            """
            SELECT checkpoint_id, payload_json
            FROM checkpoints
            WHERE task_id = ? AND stage = ?
            ORDER BY created_at DESC, checkpoint_id DESC
            LIMIT 1
            """,
            (task_id, stage),
        ).fetchone()
        assert row is not None
        payload = json.loads(row["payload_json"])
        mutate(payload)
        canonical = _canonical(payload)
        conn.execute(
            """
            UPDATE checkpoints
            SET payload_json = ?, checksum_sha256 = ?
            WHERE checkpoint_id = ?
            """,
            (canonical, _sha256(canonical), row["checkpoint_id"]),
        )


def _restart(store: SQLiteStore, project_id: str):
    restarted_store = SQLiteStore(store.path)
    restarted_store.initialize()
    project = ProductProjectRepository(restarted_store).get(project_id)
    return (
        restarted_store,
        project,
        MultiRepositoryProductFactoryHost(restarted_store, _NoopProgramWorker()),
    )


@pytest.mark.parametrize(
    "mutate",
    (
        lambda payload: payload.__setitem__("graph_version", str(_GRAPH_VERSION)),
        lambda payload: payload.__setitem__("spec_version", True),
        lambda payload: payload["dependency_edges"][0].__setitem__(
            "graph_version",
            str(_GRAPH_VERSION),
        ),
    ),
    ids=("graph-version-text", "spec-version-bool", "edge-version-text"),
)
def test_graph_authority_rejects_semantically_equal_type_coercion_tamper(
    tmp_path: Path,
    mutate,
) -> None:
    store, project, task_id, _, _ = _create_state(tmp_path)
    _rewrite_checkpoint_payload(
        store,
        task_id=task_id,
        stage=_GRAPH_STAGE,
        mutate=mutate,
    )

    _, restarted_project, restarted_host = _restart(store, project.project_id)
    with pytest.raises(RepositoryGraphIntegrityError):
        restarted_host.restore(
            host_task_id=task_id,
            project=restarted_project,
        )


def test_repair_lineage_rejects_stringified_attempt_with_recomputed_checksum(
    tmp_path: Path,
) -> None:
    store, project, task_id, host, state = _create_state(tmp_path)
    request = state.coordinator.start("a")
    result_sha = _sha(f"failed:{request.work_id}")
    state.coordinator.record_result(
        WorkerResultEnvelope(
            work_id=request.work_id,
            component_id=request.component_id,
            repository_id=request.repository_id,
            base_sha=request.base_sha,
            result_sha=result_sha,
            diff_digest=_sha256(f"diff:{request.work_id}"),
            coding_result=CodingResult(
                job_id=request.work_id,
                failure=WorkerFailure(
                    WorkerFailureKind.PROCESS_FAILED,
                    "deterministic failure for lineage schema test",
                    retryable=True,
                ),
            ),
        )
    )
    ProductFactoryCheckpointHost(store).save(
        host_task_id=task_id,
        checkpoint=state.binding.checkpoint(state.coordinator),
    )
    repair_request, lineage = host.prepare_repair_and_checkpoint(
        host_task_id=task_id,
        state=state,
        component_id="a",
        reason="advance from exact failed result",
    )
    assert repair_request.attempt == 2
    assert lineage.to_attempt == 2
    assert lineage.to_base_sha == result_sha

    _rewrite_checkpoint_payload(
        store,
        task_id=task_id,
        stage=_LINEAGE_STAGE,
        mutate=lambda payload: payload.__setitem__("to_attempt", "2"),
    )

    _, restarted_project, restarted_host = _restart(store, project.project_id)
    with pytest.raises(RepairLineageError, match="payload is invalid"):
        restarted_host.restore(
            host_task_id=task_id,
            project=restarted_project,
        )
