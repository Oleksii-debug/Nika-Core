from __future__ import annotations

import asyncio
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.packaging.release import (
    build_release_manifest,
    verify_release_manifest,
    write_release_manifest,
)
from nika_core.product_factory_checkpoint_host import (
    ProductFactoryCheckpointHost,
    ProductFactoryRecoveryDisposition,
)
from nika_core.product_factory_coordinator import ReviewDecision, WorkerResultEnvelope, WorkState
from nika_core.product_factory_orchestration import (
    ProductComponent,
    ProductRepositoryGraph,
    RepositoryRef,
)
from nika_core.product_factory_program_host import (
    ProductFactoryProgramError,
    ProductFactoryProgramHost,
)
from nika_core.product_factory_project_binding import ProductProjectCoordinatorBinding
from nika_core.product_project import (
    ProductProjectRepository,
    ProductProjectSpec,
    ProductRequirement,
)
from nika_core.toolsmith.contracts import CodingResult, TestEvidence

PROJECT_ID = "w075-project"
REPOSITORY_ID = "repo-core"
COMPONENT_ID = "component-core"
LOCATOR = "org/w075-core"
HOST_STAGE = "product_factory.coordinator.v1"
PERMISSIONS = frozenset({"read_source", "write_source", "run_tests"})
BASE_SHA = "1" * 40


def _digest(value: int) -> str:
    return f"{value:064x}"[-64:]


def _result_sha(value: int) -> str:
    return f"{value:040x}"[-40:]


def _graph() -> ProductRepositoryGraph:
    return ProductRepositoryGraph(
        project_id=PROJECT_ID,
        repositories=(
            RepositoryRef(
                repository_id=REPOSITORY_ID,
                provider="github",
                locator=LOCATOR,
                default_branch="main",
            ),
        ),
        components=(
            ProductComponent(
                component_id=COMPONENT_ID,
                repository_id=REPOSITORY_ID,
                paths=("src/w075",),
                dependencies=(),
                test_commands=(("python", "-m", "pytest", "tests/w075"),),
            ),
        ),
    )


def _spec(goal: str) -> ProductProjectSpec:
    return ProductProjectSpec(
        goal=goal,
        desired_outcome="A reviewed repair result that can become a release candidate",
        requirements=(
            ProductRequirement(
                "req-authority",
                "Only the current ProductProject specification may authorize repair or release",
                ("Old generation repair authority is stale after a specification change",),
            ),
        ),
        repository_refs=(LOCATOR,),
        team_refs=("team:implementation", "team:independent-review"),
        release_refs=("release-slot:w075-candidate",),
    )


def _record(coordinator):
    return next(
        record
        for record in coordinator.snapshot().records
        if record.request.component_id == COMPONENT_ID
    )


class DeterministicWorker:
    def __init__(self) -> None:
        self.dispatch_calls = []

    async def dispatch(self, request):
        self.dispatch_calls.append(request)
        ordinal = len(self.dispatch_calls)
        return WorkerResultEnvelope(
            work_id=request.work_id,
            component_id=request.component_id,
            repository_id=request.repository_id,
            base_sha=request.base_sha,
            result_sha=_result_sha(10_000 + ordinal),
            diff_digest=_digest(20_000 + ordinal),
            coding_result=CodingResult(
                job_id=request.work_id,
                test_evidence=(
                    TestEvidence(
                        request.acceptance_commands[0],
                        0,
                        _digest(30_000 + ordinal),
                    ),
                ),
            ),
        )

    async def inspect(self, work_id):
        raise AssertionError(f"unexpected recovery inspection for {work_id}")

    async def recover(self, request, state):
        raise AssertionError(f"unexpected recovery for {request.work_id}: {state}")


def _forge_checkpoint_generation(
    store: SQLiteStore,
    *,
    host_task_id: str,
    old_checkpoint_id: str,
    spec_version: int,
    row_version: int,
) -> str:
    """Model a candidate that rewrites generation headers and recomputes all row hashes."""

    with store.connection() as conn:
        row = conn.execute(
            "SELECT payload_json FROM checkpoints WHERE checkpoint_id = ? AND task_id = ?",
            (old_checkpoint_id, host_task_id),
        ).fetchone()
        assert row is not None
        payload = json.loads(row["payload_json"])
        payload["spec_version"] = spec_version
        payload["row_version"] = row_version
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        checksum = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        identity = json.dumps(
            {
                "host_task_id": host_task_id,
                "project_id": payload["project_id"],
                "spec_version": spec_version,
                "row_version": row_version,
                "revision": payload["coordinator"]["revision"],
                "checksum": checksum,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        forged_checkpoint_id = (
            "pf2-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
        )
        conn.execute(
            "UPDATE checkpoints SET checkpoint_id = ?, payload_json = ?, checksum_sha256 = ? "
            "WHERE checkpoint_id = ? AND task_id = ?",
            (
                forged_checkpoint_id,
                canonical,
                checksum,
                old_checkpoint_id,
                host_task_id,
            ),
        )
    return forged_checkpoint_id


def test_spec_change_cannot_resurrect_old_repair_worker_result_or_release_candidate(
    tmp_path,
) -> None:
    """W075: old repair evidence remains history, never current execution authority."""

    db_path = tmp_path / "w075 authority.db"
    store = SQLiteStore(db_path)
    store.initialize()
    projects = ProductProjectRepository(store)
    graph = _graph()
    project = projects.create(
        project_id=PROJECT_ID,
        name="W075 long-horizon authority",
        spec=_spec("Generation N product goal"),
        idempotency_key="w075:create",
    )
    binding = ProductProjectCoordinatorBinding(project, graph)
    task = TaskQueue(store).create(
        workspace_id="ws-w075",
        agent_id="product-factory",
        payload={"kind": "product_factory", "product_project_id": PROJECT_ID},
    )
    coordinator = binding.plan(
        base_shas={REPOSITORY_ID: BASE_SHA},
        component_goals={COMPONENT_ID: "Implement generation N component"},
        permission_ceiling=PERMISSIONS,
    )
    checkpoints = ProductFactoryCheckpointHost(store)
    checkpoints.save(
        host_task_id=task.task_id,
        checkpoint=binding.checkpoint(coordinator),
    )

    worker = DeterministicWorker()
    host = ProductFactoryProgramHost(store, worker)
    first_outcomes = asyncio.run(
        host.dispatch_ready(
            host_task_id=task.task_id,
            binding=binding,
            coordinator=coordinator,
            max_count=1,
        )
    )
    assert len(first_outcomes) == 1
    first_record = _record(coordinator)
    assert first_record.state is WorkState.REVIEW_REQUIRED
    assert first_record.result is not None

    host.review_and_checkpoint(
        host_task_id=task.task_id,
        binding=binding,
        coordinator=coordinator,
        component_id=COMPONENT_ID,
        decision=ReviewDecision(
            reviewer_id="qa-independent",
            accepted=False,
            reason="generation N requires one bounded repair",
            evidence_refs=("review:generation-n:reject",),
        ),
    )
    repair = host.prepare_repair_and_checkpoint(
        host_task_id=task.task_id,
        binding=binding,
        coordinator=coordinator,
        component_id=COMPONENT_ID,
        base_sha=first_record.result.result_sha,
        reason="apply generation N independent review",
    )
    assert repair.attempt == 2

    repair_outcomes = asyncio.run(
        host.dispatch_ready(
            host_task_id=task.task_id,
            binding=binding,
            coordinator=coordinator,
            max_count=1,
        )
    )
    assert len(repair_outcomes) == 1
    repaired_record = _record(coordinator)
    assert repaired_record.state is WorkState.REVIEW_REQUIRED
    assert repaired_record.request.work_id == repair.work_id
    assert repaired_record.result is not None
    old_worker_result_sha = repaired_record.result.result_sha

    host.review_and_checkpoint(
        host_task_id=task.task_id,
        binding=binding,
        coordinator=coordinator,
        component_id=COMPONENT_ID,
        decision=ReviewDecision(
            reviewer_id="qa-independent",
            accepted=True,
            reason="generation N repair evidence accepted",
            evidence_refs=("review:generation-n:accept",),
        ),
    )
    assert _record(coordinator).state is WorkState.ACCEPTED

    release_dir = tmp_path / "release candidate N"
    release_dir.mkdir()
    (release_dir / "product.bin").write_bytes(b"w075-generation-n")
    old_release_candidate = build_release_manifest(
        release_dir,
        product="W075 fixture",
        version="N",
        source_sha=old_worker_result_sha,
    )
    write_release_manifest(release_dir, old_release_candidate)
    assert verify_release_manifest(release_dir, old_release_candidate) == ()

    old_checkpoint = checkpoints.latest(
        host_task_id=task.task_id,
        project_id=PROJECT_ID,
    )
    assert old_checkpoint is not None
    assert old_checkpoint.checkpoint.spec_version == 1
    assert old_checkpoint.checkpoint.row_version == 0

    generation_n_plus_1 = projects.update_spec(
        PROJECT_ID,
        _spec("Generation N+1 changed product goal"),
        expected_row_version=project.row_version,
        change_reason="W075 material product goal revision",
    )
    assert generation_n_plus_1.spec_version == 2
    assert generation_n_plus_1.row_version == 1
    assert generation_n_plus_1.spec.repository_refs == project.spec.repository_refs
    assert generation_n_plus_1.spec.team_refs == project.spec.team_refs
    assert generation_n_plus_1.spec.release_refs == project.spec.release_refs

    restarted_store = SQLiteStore(db_path)
    restarted_store.initialize()
    restarted_project = ProductProjectRepository(restarted_store).get(PROJECT_ID)
    restarted_binding = ProductProjectCoordinatorBinding(restarted_project, graph)
    stale = ProductFactoryCheckpointHost(restarted_store).inspect_latest(
        host_task_id=task.task_id,
        binding=restarted_binding,
    )
    assert stale.disposition is ProductFactoryRecoveryDisposition.STALE_PROJECT
    try:
        ProductFactoryProgramHost(restarted_store, DeterministicWorker()).restore_latest(
            host_task_id=task.task_id,
            binding=restarted_binding,
        )
    except ProductFactoryProgramError:
        pass
    else:
        raise AssertionError("ProgramHost resumed an unmodified old-spec checkpoint")

    history = ProductProjectRepository(restarted_store).spec_history(PROJECT_ID)
    assert tuple(item.spec_version for item in history) == (1, 2)
    assert verify_release_manifest(release_dir, old_release_candidate) == ()
    assert old_release_candidate.source_sha == old_worker_result_sha

    forged_checkpoint_id = _forge_checkpoint_generation(
        restarted_store,
        host_task_id=task.task_id,
        old_checkpoint_id=old_checkpoint.checkpoint_id,
        spec_version=restarted_project.spec_version,
        row_version=restarted_project.row_version,
    )
    assert forged_checkpoint_id != old_checkpoint.checkpoint_id

    def inspect_after_restart(_: int) -> ProductFactoryRecoveryDisposition:
        concurrent_store = SQLiteStore(db_path)
        concurrent_project = ProductProjectRepository(concurrent_store).get(PROJECT_ID)
        concurrent_binding = ProductProjectCoordinatorBinding(concurrent_project, _graph())
        return ProductFactoryCheckpointHost(concurrent_store).inspect_latest(
            host_task_id=task.task_id,
            binding=concurrent_binding,
        ).disposition

    with ThreadPoolExecutor(max_workers=8) as executor:
        dispositions = tuple(executor.map(inspect_after_restart, range(32)))

    assert len(dispositions) == 32
    assert all(
        disposition is not ProductFactoryRecoveryDisposition.RESUMABLE
        for disposition in dispositions
    ), (
        "candidate-controlled spec_version/row_version rewrite plus recomputed checkpoint "
        "checksum/id resurrected generation N repair authority after generation N+1 spec change"
    )
