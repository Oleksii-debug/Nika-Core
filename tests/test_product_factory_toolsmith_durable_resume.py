from __future__ import annotations

from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.checkpoint import CheckpointService
from nika_core.kernel.task_queue import TaskQueue
from nika_core.product_factory_checkpoint_host import ProductFactoryCheckpointHost
from nika_core.product_factory_coding_worker_adapter import (
    CodingWorkerComponentAdapter,
    CodingWorkerDispatchContext,
    CodingWorkerExecutionEvidence,
)
from nika_core.product_factory_coordinator import (
    ProductFactoryCoordinator,
    WorkerResultEnvelope,
    WorkState,
)
from nika_core.product_factory_orchestration import (
    ProductComponent,
    ProductRepositoryGraph,
    RepositoryRef,
)
from nika_core.product_factory_project_binding import ProductProjectCoordinatorBinding
from nika_core.product_factory_toolsmith_integration import (
    ProductFactoryToolsmithBridge,
    ProductFactoryToolsmithError,
)
from nika_core.product_factory_toolsmith_state import (
    ComponentCapabilityBindingState,
    ProductFactoryToolsmithBindingRepository,
)
from nika_core.product_project import (
    ProductProjectRepository,
    ProductProjectSpec,
    ProductRequirement,
)
from nika_core.toolsmith.contracts import (
    CapabilityManifestV1,
    CodingResult,
    IsolationClass,
    NetworkPolicy,
    ProcessPolicy,
    ResourceBudget,
    ReuseCandidate,
    WorkerFailure,
    WorkerFailureKind,
    WorkspaceLease,
)
from nika_core.toolsmith.repository import ToolsmithRepository
from nika_core.toolsmith.service import CapabilityEscalationService

SHA_A = "a" * 40
SHA_B = "b" * 40
DIFF_DIGEST = "d" * 64
CAPABILITY_DIGEST = "f" * 64
PERMISSIONS = frozenset({"read_source", "write_source", "run_tests"})


class UnusedWorker:
    async def execute(self, _job):
        raise AssertionError("worker execution is not expected")

    async def cancel(self, _job_id):
        raise AssertionError("worker cancellation is not expected")

    async def inspect(self, _job_id):
        return None

    async def recover(self, _job, _state):
        raise AssertionError("worker recovery is not expected")


class UnusedContexts:
    async def context_for(self, _request):
        return CodingWorkerDispatchContext(
            repository_tree_digest="tree",
            lease=WorkspaceLease(
                "lease-1",
                Path("worker-root"),
                IsolationClass.PROCESS_CONTAINED,
                "2026-08-24T00:00:00Z",
            ),
            process_policy=ProcessPolicy(("python",)),
            network_policy=NetworkPolicy(),
            resource_budget=ResourceBudget(300, 1000, 10),
        )


class UnusedEvidence:
    async def collect(self, request, _job, _result):
        return CodingWorkerExecutionEvidence(
            request.work_id,
            request.repository_id,
            request.base_sha,
            SHA_B,
            DIFF_DIGEST,
        )


def _graph() -> ProductRepositoryGraph:
    return ProductRepositoryGraph(
        project_id="project-1",
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


def _spec() -> ProductProjectSpec:
    return ProductProjectSpec(
        goal="Build a durable product",
        desired_outcome="Reviewed component",
        requirements=(
            ProductRequirement(
                "req-1",
                "Component capability gaps resume after restart",
                ("registered capability resumes exact failed attempt",),
            ),
        ),
        repository_refs=("org/repo",),
    )


def _setup(tmp_path):
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    project = ProductProjectRepository(store).create(
        project_id="project-1",
        name="Durable Toolsmith Product",
        spec=_spec(),
        idempotency_key="create:project-1",
    )
    graph = _graph()
    binding = ProductProjectCoordinatorBinding(project, graph)
    coordinator = binding.plan(
        base_shas={"repo-1": SHA_A},
        component_goals={"core": "Implement core"},
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
            diff_digest=DIFF_DIGEST,
            coding_result=CodingResult(
                job_id=request.work_id,
                failure=WorkerFailure(
                    WorkerFailureKind.PROCESS_FAILED,
                    "missing safe TOML capability",
                    retryable=True,
                ),
            ),
        )
    )
    host_task = TaskQueue(store).create(
        workspace_id="ws-product",
        agent_id="product-factory",
        payload={"kind": "product_factory", "product_project_id": project.project_id},
    )
    ProductFactoryCheckpointHost(store).save(
        host_task_id=host_task.task_id,
        checkpoint=binding.checkpoint(coordinator),
    )
    return store, binding, coordinator, request, host_task.task_id


def _toolsmith(store: SQLiteStore):
    repository = ToolsmithRepository(store)
    service = CapabilityEscalationService(
        repository=repository,
        checkpoints=CheckpointService(store),
        worker=UnusedWorker(),
    )
    return repository, service


def _bridge(store: SQLiteStore, service: CapabilityEscalationService):
    return ProductFactoryToolsmithBridge(
        service,
        CodingWorkerComponentAdapter(UnusedWorker(), UnusedContexts(), UnusedEvidence()),
        store=store,
    )


def _register(service, checkpoint) -> None:
    candidate = ReuseCandidate(
        capability_id=checkpoint.capability_id,
        version="1.4.0",
        source="existing-registry",
        digest=CAPABILITY_DIGEST,
        permissions=PERMISSIONS,
    )
    version, selected = service.choose_reuse(
        gap=checkpoint.gap,
        candidates=(candidate,),
        expected_version=checkpoint.row_version,
    )
    assert selected == candidate
    version = service.start_verification(
        gap=checkpoint.gap,
        expected_version=version,
    )
    version = service.accept_verification(
        gap=checkpoint.gap,
        expected_version=version,
        candidate_digest=CAPABILITY_DIGEST,
        verifier_evidence={"tests": "green"},
    )
    service.register(
        gap=checkpoint.gap,
        expected_version=version,
        manifest=CapabilityManifestV1(
            capability_id=checkpoint.capability_id,
            version="1.4.0",
            digest=CAPABILITY_DIGEST,
            entrypoint="nika_ext.toml:tool",
            permissions=PERMISSIONS,
            source="existing-registry",
        ),
    )


def _restart(store: SQLiteStore, host_task_id: str):
    restarted = SQLiteStore(store.path)
    restarted.initialize()
    project = ProductProjectRepository(restarted).get("project-1")
    binding = ProductProjectCoordinatorBinding(project, _graph())
    coordinator = ProductFactoryCheckpointHost(restarted).restore_latest(
        host_task_id=host_task_id,
        binding=binding,
    )
    _, service = _toolsmith(restarted)
    return restarted, binding, coordinator, service


def _record(coordinator: ProductFactoryCoordinator):
    return coordinator.snapshot().records[0]


def test_real_toolsmith_begin_uses_canonical_host_task_and_accepts_zero_row_version(
    tmp_path,
) -> None:
    store, _, coordinator, request, host_task_id = _setup(tmp_path)
    repository, service = _toolsmith(store)
    bridge = _bridge(store, service)

    checkpoint = bridge.begin_durable_gap(
        request,
        host_task_id=host_task_id,
        capability_id="toml-editor",
        reason="bounded worker lacks safe TOML editing",
        attempted_methods=("registry-search",),
    )

    assert checkpoint.task_id == host_task_id
    assert checkpoint.work_id == request.work_id
    assert checkpoint.row_version == 0
    row = repository.get_escalation(
        task_id=host_task_id,
        capability_id="toml-editor",
    )
    assert row is not None
    assert row["task_id"] == host_task_id
    assert _record(coordinator).state is WorkState.REPAIR_REQUIRED


def test_registered_gap_survives_process_restart_and_checkpoints_exact_repair(
    tmp_path,
) -> None:
    store, _, _, request, host_task_id = _setup(tmp_path)
    _, service = _toolsmith(store)
    bridge = _bridge(store, service)
    checkpoint = bridge.begin_durable_gap(
        request,
        host_task_id=host_task_id,
        capability_id="toml-editor",
        reason="missing capability",
    )
    _register(service, checkpoint)

    restarted, binding, coordinator, service = _restart(store, host_task_id)
    bridge = _bridge(restarted, service)
    resumed = bridge.resume_durable_registered_gap(
        host_task_id=host_task_id,
        binding=binding,
        coordinator=coordinator,
        component_id="core",
    )

    assert resumed is not None
    assert resumed.previous_work_id == request.work_id
    assert resumed.next_request.attempt == 2
    assert resumed.next_request.base_sha == SHA_B
    assert resumed.next_request.permission_ceiling == PERMISSIONS
    assert resumed.capability_version == "1.4.0"
    assert resumed.capability_digest == CAPABILITY_DIGEST

    restarted_again, _, coordinator_again, _ = _restart(restarted, host_task_id)
    durable = _record(coordinator_again)
    assert durable.state is WorkState.READY
    assert durable.request.work_id == resumed.next_request.work_id
    assert durable.request.attempt == 2
    binding_row = ProductFactoryToolsmithBindingRepository(restarted_again).require(
        host_task_id=host_task_id,
        work_id=request.work_id,
    )
    assert binding_row.state is ComponentCapabilityBindingState.CONSUMED
    assert binding_row.next_work_id == resumed.next_request.work_id


def test_checkpoint_failure_leaves_prepared_binding_and_restart_retries_same_attempt(
    tmp_path,
    monkeypatch,
) -> None:
    store, binding, coordinator, request, host_task_id = _setup(tmp_path)
    _, service = _toolsmith(store)
    bridge = _bridge(store, service)
    checkpoint = bridge.begin_durable_gap(
        request,
        host_task_id=host_task_id,
        capability_id="toml-editor",
        reason="missing capability",
    )
    _register(service, checkpoint)

    def fail_save(*_args, **_kwargs):
        raise OSError("simulated checkpoint outage")

    monkeypatch.setattr(bridge._checkpoints, "save", fail_save)
    with pytest.raises(OSError, match="simulated checkpoint outage"):
        bridge.resume_durable_registered_gap(
            host_task_id=host_task_id,
            binding=binding,
            coordinator=coordinator,
            component_id="core",
        )

    assert _record(coordinator).request.work_id == request.work_id
    assert _record(coordinator).state is WorkState.REPAIR_REQUIRED
    prepared = ProductFactoryToolsmithBindingRepository(store).require(
        host_task_id=host_task_id,
        work_id=request.work_id,
    )
    assert prepared.state is ComponentCapabilityBindingState.RESUME_PREPARED
    prepared_next = prepared.next_work_id

    monkeypatch.undo()
    restarted, restarted_binding, restarted_coordinator, restarted_service = _restart(
        store,
        host_task_id,
    )
    resumed = _bridge(restarted, restarted_service).resume_durable_registered_gap(
        host_task_id=host_task_id,
        binding=restarted_binding,
        coordinator=restarted_coordinator,
        component_id="core",
    )

    assert resumed is not None
    assert resumed.next_request.work_id == prepared_next
    assert resumed.next_request.attempt == 2


def test_checkpoint_committed_before_binding_finalization_reconciles_without_attempt_three(
    tmp_path,
    monkeypatch,
) -> None:
    store, binding, coordinator, request, host_task_id = _setup(tmp_path)
    _, service = _toolsmith(store)
    bridge = _bridge(store, service)
    checkpoint = bridge.begin_durable_gap(
        request,
        host_task_id=host_task_id,
        capability_id="toml-editor",
        reason="missing capability",
    )
    _register(service, checkpoint)

    original = ProductFactoryToolsmithBindingRepository.mark_consumed

    def fail_consume(self, _binding):
        raise RuntimeError("simulated binding finalization outage")

    monkeypatch.setattr(
        ProductFactoryToolsmithBindingRepository,
        "mark_consumed",
        fail_consume,
    )
    with pytest.raises(ProductFactoryToolsmithError, match="finalization requires"):
        bridge.resume_durable_registered_gap(
            host_task_id=host_task_id,
            binding=binding,
            coordinator=coordinator,
            component_id="core",
        )

    assert _record(coordinator).state is WorkState.READY
    assert _record(coordinator).request.attempt == 2
    next_work_id = _record(coordinator).request.work_id

    monkeypatch.setattr(
        ProductFactoryToolsmithBindingRepository,
        "mark_consumed",
        original,
    )
    restarted, restarted_binding, restarted_coordinator, restarted_service = _restart(
        store,
        host_task_id,
    )
    resumed = _bridge(restarted, restarted_service).resume_durable_registered_gap(
        host_task_id=host_task_id,
        binding=restarted_binding,
        coordinator=restarted_coordinator,
        component_id="core",
    )

    assert resumed is not None
    assert resumed.next_request.work_id == next_work_id
    assert resumed.next_request.attempt == 2


def test_foreign_product_factory_task_is_rejected_before_toolsmith_escalation(tmp_path) -> None:
    store, _, _, request, _ = _setup(tmp_path)
    foreign = TaskQueue(store).create(
        workspace_id="ws-product",
        agent_id="product-factory",
        payload={"kind": "product_factory", "product_project_id": "other-project"},
    )
    repository, service = _toolsmith(store)
    bridge = _bridge(store, service)

    with pytest.raises(ProductFactoryToolsmithError, match="not authoritative"):
        bridge.begin_durable_gap(
            request,
            host_task_id=foreign.task_id,
            capability_id="toml-editor",
            reason="missing capability",
        )

    assert repository.get_escalation(
        task_id=foreign.task_id,
        capability_id="toml-editor",
    ) is None


def test_old_gap_cannot_be_consumed_after_component_advances_to_another_attempt(
    tmp_path,
) -> None:
    store, _, coordinator, request, host_task_id = _setup(tmp_path)
    _, service = _toolsmith(store)
    bridge = _bridge(store, service)
    checkpoint = bridge.begin_durable_gap(
        request,
        host_task_id=host_task_id,
        capability_id="toml-editor",
        reason="missing capability",
    )
    _register(service, checkpoint)
    bridge.worker_adapter.prepare_safe_repair(
        coordinator,
        "core",
        reason="independent manual repair",
    )

    with pytest.raises(ProductFactoryToolsmithError, match="stale component attempt"):
        bridge.resume_durable_registered_gap(
            host_task_id=host_task_id,
            binding=ProductProjectCoordinatorBinding(
                ProductProjectRepository(store).get("project-1"),
                _graph(),
            ),
            coordinator=coordinator,
            component_id="core",
        )
