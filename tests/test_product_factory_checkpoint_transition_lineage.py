from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.product_factory_checkpoint_host import (
    ProductFactoryCheckpointError,
    ProductFactoryCheckpointHost,
    ProductFactoryCheckpointIntegrityError,
)
from nika_core.product_factory_coordinator import (
    ReviewDecision,
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
from nika_core.toolsmith.contracts import (
    CodingResult,
    TestEvidence,
    WorkerFailure,
    WorkerFailureKind,
)

SHA_A = "a" * 40
SHA_B = "b" * 40
DIGEST_D = "d" * 64
LOCATOR = "org/repo"
PERMISSIONS = frozenset({"read_source", "write_source", "run_tests"})


def _spec() -> ProductProjectSpec:
    return ProductProjectSpec(
        goal="Build durable product",
        desired_outcome="A reviewed durable product",
        requirements=(
            ProductRequirement(
                "req-1",
                "Restart-safe orchestration",
                ("Repair attempts survive restart without duplicate execution",),
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
    coordinator = binding.plan(
        base_shas={"repo-1": SHA_A},
        component_goals={"core": "build core"},
        permission_ceiling=PERMISSIONS,
    )
    task = TaskQueue(store).create(
        workspace_id="ws-product",
        agent_id="product-factory",
        payload={"kind": "product_factory", "product_project_id": "p1"},
    )
    return store, binding, coordinator, task.task_id


def _fail_running(coordinator) -> None:
    request = next(
        record.request
        for record in coordinator.snapshot().records
        if record.state is WorkState.RUNNING
    )
    coordinator.record_result(
        WorkerResultEnvelope(
            work_id=request.work_id,
            component_id=request.component_id,
            repository_id=request.repository_id,
            base_sha=request.base_sha,
            result_sha=SHA_B,
            diff_digest=DIGEST_D,
            coding_result=CodingResult(
                job_id=request.work_id,
                failure=WorkerFailure(
                    WorkerFailureKind.PROCESS_FAILED,
                    "worker failed deterministically",
                    retryable=True,
                ),
            ),
        )
    )


def _accept_ready(coordinator) -> None:
    request = coordinator.start("core")
    coordinator.record_result(
        WorkerResultEnvelope(
            work_id=request.work_id,
            component_id=request.component_id,
            repository_id=request.repository_id,
            base_sha=request.base_sha,
            result_sha=SHA_B,
            diff_digest=DIGEST_D,
            coding_result=CodingResult(
                job_id=request.work_id,
                test_evidence=(
                    TestEvidence(
                        command=request.acceptance_commands[0],
                        exit_code=0,
                        output_digest=DIGEST_D,
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
            reason="deterministic acceptance passed",
            evidence_refs=("evidence:core",),
        ),
    )


def _restart(store: SQLiteStore, task_id: str):
    restarted_store = SQLiteStore(store.path)
    restarted_store.initialize()
    restarted_project = ProductProjectRepository(restarted_store).get("p1")
    restarted_binding = ProductProjectCoordinatorBinding(restarted_project, _graph())
    restarted_host = ProductFactoryCheckpointHost(restarted_store)
    restored = restarted_host.restore_latest(
        host_task_id=task_id,
        binding=restarted_binding,
    )
    return restarted_store, restarted_binding, restarted_host, restored


def test_repair_requires_prior_durable_repair_required_checkpoint(tmp_path) -> None:
    store, binding, coordinator, task_id = _setup(tmp_path)
    host = ProductFactoryCheckpointHost(store)
    host.save(host_task_id=task_id, checkpoint=binding.checkpoint(coordinator))

    coordinator.start("core")
    _fail_running(coordinator)
    coordinator.prepare_repair("core", base_sha=SHA_B, reason="fix failure")

    with pytest.raises(
        ProductFactoryCheckpointIntegrityError,
        match="prior durable repair_required",
    ):
        host.save(host_task_id=task_id, checkpoint=binding.checkpoint(coordinator))


def test_attempt_cannot_skip_multiple_durable_repair_generations(tmp_path) -> None:
    store, binding, coordinator, task_id = _setup(tmp_path)
    host = ProductFactoryCheckpointHost(store)
    host.save(host_task_id=task_id, checkpoint=binding.checkpoint(coordinator))
    coordinator.start("core")
    host.save(host_task_id=task_id, checkpoint=binding.checkpoint(coordinator))
    _fail_running(coordinator)
    host.save(host_task_id=task_id, checkpoint=binding.checkpoint(coordinator))

    coordinator.prepare_repair("core", base_sha=SHA_B, reason="repair one")
    coordinator.start("core")
    _fail_running(coordinator)
    coordinator.prepare_repair("core", base_sha=SHA_A, reason="repair two")

    with pytest.raises(
        ProductFactoryCheckpointIntegrityError,
        match="attempt skipped or regressed",
    ):
        host.save(host_task_id=task_id, checkpoint=binding.checkpoint(coordinator))


def test_same_attempt_state_cannot_roll_back_with_recomputed_candidate_snapshot(tmp_path) -> None:
    store, binding, coordinator, task_id = _setup(tmp_path)
    host = ProductFactoryCheckpointHost(store)
    host.save(host_task_id=task_id, checkpoint=binding.checkpoint(coordinator))
    coordinator.start("core")
    running_checkpoint = binding.checkpoint(coordinator)
    host.save(host_task_id=task_id, checkpoint=running_checkpoint)

    running_snapshot = running_checkpoint.coordinator
    running_record = running_snapshot.records[0]
    forged_snapshot = replace(
        running_snapshot,
        revision=running_snapshot.revision + 1,
        records=(replace(running_record, state=WorkState.READY),),
    )
    forged_checkpoint = ProductProjectCoordinatorCheckpoint(
        project_id=binding.project.project_id,
        spec_version=binding.project.spec_version,
        row_version=binding.project.row_version,
        coordinator=forged_snapshot,
    )

    with pytest.raises(
        ProductFactoryCheckpointIntegrityError,
        match="state regressed or is not legally forward-reachable",
    ):
        host.save(host_task_id=task_id, checkpoint=forged_checkpoint)


def test_sparse_checkpoint_allows_legal_ready_to_accepted_progress(tmp_path) -> None:
    store, binding, coordinator, task_id = _setup(tmp_path)
    host = ProductFactoryCheckpointHost(store)
    first = host.save(host_task_id=task_id, checkpoint=binding.checkpoint(coordinator))

    assert first.checkpoint.coordinator.records[0].state is WorkState.READY
    _accept_ready(coordinator)
    accepted = host.save(host_task_id=task_id, checkpoint=binding.checkpoint(coordinator))

    assert accepted.checkpoint.coordinator.records[0].state is WorkState.ACCEPTED
    restored = host.restore_latest(host_task_id=task_id, binding=binding)
    assert restored.snapshot().records[0].state is WorkState.ACCEPTED


def test_legitimate_repair_lineage_remains_restart_resumable(tmp_path) -> None:
    store, binding, coordinator, task_id = _setup(tmp_path)
    host = ProductFactoryCheckpointHost(store)
    host.save(host_task_id=task_id, checkpoint=binding.checkpoint(coordinator))

    coordinator.start("core")
    host.save(host_task_id=task_id, checkpoint=binding.checkpoint(coordinator))
    _fail_running(coordinator)
    host.save(host_task_id=task_id, checkpoint=binding.checkpoint(coordinator))
    repair = coordinator.prepare_repair("core", base_sha=SHA_B, reason="fix failure")
    saved = host.save(host_task_id=task_id, checkpoint=binding.checkpoint(coordinator))

    restarted_store = SQLiteStore(store.path)
    restarted_store.initialize()
    restarted_project = ProductProjectRepository(restarted_store).get("p1")
    restarted_binding = ProductProjectCoordinatorBinding(restarted_project, _graph())
    restored = ProductFactoryCheckpointHost(restarted_store).restore_latest(
        host_task_id=task_id,
        binding=restarted_binding,
    )
    record = restored.snapshot().records[0]

    assert saved.checkpoint.coordinator.records[0].request.work_id == repair.work_id
    assert record.request.work_id == repair.work_id
    assert record.request.attempt == 2
    assert record.state is WorkState.READY


def test_conflicting_multi_connection_writers_serialize_one_durable_revision(tmp_path) -> None:
    store, binding, coordinator, task_id = _setup(tmp_path)
    host = ProductFactoryCheckpointHost(store)
    initial = host.save(host_task_id=task_id, checkpoint=binding.checkpoint(coordinator))
    authority = coordinator.trusted_plan_fingerprint

    blocked = binding.restore(
        initial.checkpoint,
        trusted_plan_fingerprint=authority,
    )
    failed = binding.restore(
        initial.checkpoint,
        trusted_plan_fingerprint=authority,
    )
    blocked.start("core")
    blocked.block("core", "independent blocker")
    failed.start("core")
    _fail_running(failed)
    blocked_checkpoint = binding.checkpoint(blocked)
    failed_checkpoint = binding.checkpoint(failed)

    left_store = SQLiteStore(store.path)
    right_store = SQLiteStore(store.path)
    left_store.initialize()
    right_store.initialize()
    barrier = threading.Barrier(2)

    def save_candidate(candidate_store, checkpoint):
        barrier.wait()
        return ProductFactoryCheckpointHost(candidate_store).save(
            host_task_id=task_id,
            checkpoint=checkpoint,
        )

    outcomes: list[object] = []
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = (
            executor.submit(save_candidate, left_store, blocked_checkpoint),
            executor.submit(save_candidate, right_store, failed_checkpoint),
        )
        for future in futures:
            try:
                outcomes.append(future.result())
            except ProductFactoryCheckpointError as exc:
                outcomes.append(exc)

    successes = [item for item in outcomes if not isinstance(item, Exception)]
    failures = [item for item in outcomes if isinstance(item, ProductFactoryCheckpointError)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert "same coordinator revision has different durable state" in str(failures[0])
    with store.connection() as conn:
        checkpoint_count = conn.execute(
            "SELECT COUNT(*) FROM checkpoints WHERE task_id = ? AND stage = ?",
            (task_id, "product_factory.coordinator.v1"),
        ).fetchone()[0]
    assert checkpoint_count == 2


def test_twenty_five_repair_generations_survive_repeated_process_style_restarts(
    tmp_path,
) -> None:
    store, binding, coordinator, task_id = _setup(tmp_path)
    host = ProductFactoryCheckpointHost(store)
    host.save(host_task_id=task_id, checkpoint=binding.checkpoint(coordinator))
    authority = coordinator.trusted_plan_fingerprint

    for attempt in range(1, 26):
        coordinator.start("core")
        host.save(host_task_id=task_id, checkpoint=binding.checkpoint(coordinator))
        _fail_running(coordinator)
        host.save(host_task_id=task_id, checkpoint=binding.checkpoint(coordinator))
        store, binding, host, coordinator = _restart(store, task_id)
        record = coordinator.snapshot().records[0]
        assert record.request.attempt == attempt
        assert record.state is WorkState.REPAIR_REQUIRED
        assert coordinator.trusted_plan_fingerprint == authority
        if attempt == 25:
            break

        next_base = SHA_B if attempt % 2 else SHA_A
        coordinator.prepare_repair(
            "core",
            base_sha=next_base,
            reason=f"repair generation {attempt + 1}",
        )
        host.save(host_task_id=task_id, checkpoint=binding.checkpoint(coordinator))
        store, binding, host, coordinator = _restart(store, task_id)
        assert coordinator.snapshot().records[0].request.attempt == attempt + 1
        assert coordinator.snapshot().records[0].state is WorkState.READY

    assert coordinator.snapshot().records[0].request.attempt == 25
    assert coordinator.snapshot().records[0].state is WorkState.REPAIR_REQUIRED
