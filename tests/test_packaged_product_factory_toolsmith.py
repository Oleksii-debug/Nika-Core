from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.checkpoint import CheckpointService
from nika_core.kernel.task_queue import TaskQueue
from nika_core.product_factory_checkpoint_host import ProductFactoryCheckpointHost
from nika_core.product_factory_coding_worker_adapter import CodingWorkerComponentAdapter
from nika_core.product_factory_coordinator import WorkerResultEnvelope
from nika_core.product_factory_multi_repository import (
    MultiRepositoryExecutionError,
    MultiRepositoryProductFactoryHost,
    RepairLineageError,
)
from nika_core.product_factory_orchestration import (
    ProductComponent,
    ProductRepositoryGraph,
    RepositoryRef,
)
from nika_core.product_factory_packaged_preparation import (
    PackagedProductFactoryExecutionPlan,
    PackagedProductFactoryPreparationService,
)
from nika_core.product_factory_packaged_toolsmith import (
    PackagedProductFactoryCapabilityGapPlan,
    PackagedProductFactoryToolsmithError,
    PackagedProductFactoryToolsmithService,
)
from nika_core.product_factory_toolsmith_integration import (
    ProductFactoryToolsmithBridge,
    ProductFactoryToolsmithError,
)
from nika_core.product_factory_toolsmith_state import (
    ProductFactoryToolsmithBindingError,
    ProductFactoryToolsmithBindingRepository,
)
from nika_core.product_project import ProductProjectRepository, ProductProjectSpec
from nika_core.toolsmith.contracts import (
    CandidateState,
    CapabilityManifestV1,
    CodingResult,
    RecoveryState,
    ReuseCandidate,
    WorkerFailure,
    WorkerFailureKind,
)
from nika_core.toolsmith.repository import ToolsmithRepository
from nika_core.toolsmith.service import CapabilityEscalationService

SHA_A = "a" * 40
SHA_B = "b" * 40
DIFF_DIGEST = "d" * 64
CAPABILITY_DIGEST = "f" * 64
PERMISSIONS = frozenset({"read_source", "write_source", "run_tests"})


class NeverDispatchWorker:
    async def dispatch(self, request):
        raise AssertionError(f"unexpected dispatch: {request.work_id}")

    async def inspect(self, work_id: str) -> RecoveryState | None:
        raise AssertionError(f"unexpected inspect: {work_id}")

    async def recover(self, request, state):
        raise AssertionError(f"unexpected recover: {request.work_id}:{state}")


class FailingProgramWorker:
    async def dispatch(self, request):
        return WorkerResultEnvelope(
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
                    "canonical bounded worker lacks exact TOML editing capability",
                    retryable=True,
                ),
            ),
        )

    async def inspect(self, _work_id: str) -> RecoveryState | None:
        return None

    async def recover(self, request, state):
        raise AssertionError(f"unexpected recover: {request.work_id}:{state}")


class UnusedToolsmithWorker:
    async def execute(self, _job):
        raise AssertionError("Toolsmith worker execution is not expected")

    async def cancel(self, _job_id):
        raise AssertionError("Toolsmith worker cancellation is not expected")

    async def inspect(self, _job_id):
        return None

    async def recover(self, _job, _state):
        raise AssertionError("Toolsmith worker recovery is not expected")


class RecordingEscalation:
    def __init__(self) -> None:
        self.begun = []
        self.resumed: list[tuple[str, str]] = []

    def begin(self, gap):
        self.begun.append(gap)
        return 0, CandidateState.PROPOSED

    def reconcile_resume(self, *, task_id: str, capability_id: str):
        self.resumed.append((task_id, capability_id))
        return None


class RecordingBridge:
    def __init__(self) -> None:
        self.begin_calls = []
        self.resume_calls = []
        self.begin_result = object()
        self.resume_result = object()

    def begin_durable_gap(
        self,
        request,
        *,
        host_task_id: str,
        capability_id: str,
        reason: str,
        attempted_methods: tuple[str, ...] = (),
    ):
        self.begin_calls.append(
            (request, host_task_id, capability_id, reason, attempted_methods)
        )
        return self.begin_result

    def resume_durable_registered_gap(
        self,
        *,
        host_task_id: str,
        binding,
        coordinator,
        component_id: str,
        expected_work_id: str | None = None,
        expected_capability_id: str | None = None,
    ):
        self.resume_calls.append(
            (
                host_task_id,
                binding,
                coordinator,
                component_id,
                expected_work_id,
                expected_capability_id,
            )
        )
        return self.resume_result


def _fixture(
    tmp_path: Path,
    *,
    failed: bool = True,
    canonical_failure: bool = False,
):
    store = SQLiteStore(tmp_path / "packaged factory toolsmith.db")
    store.initialize()
    repository = ProductProjectRepository(store)
    project = repository.create(
        project_id="packaged-toolsmith-product",
        name="Packaged Product Factory Toolsmith",
        spec=ProductProjectSpec(
            goal="Build one exact component",
            desired_outcome="Capability gaps retain durable authority",
            repository_refs=("Oleksii-debug/Nika-Core",),
        ),
        idempotency_key="create:packaged-toolsmith-product",
    )
    graph = ProductRepositoryGraph(
        project_id=project.project_id,
        repositories=(
            RepositoryRef(
                repository_id="repo-core",
                provider="github",
                locator="Oleksii-debug/Nika-Core",
                default_branch="main",
            ),
        ),
        components=(
            ProductComponent(
                component_id="core",
                repository_id="repo-core",
                paths=("src/nika_core",),
                test_commands=(("python", "-m", "pytest", "tests"),),
            ),
        ),
    )
    host = MultiRepositoryProductFactoryHost(
        store,
        FailingProgramWorker() if canonical_failure else NeverDispatchWorker(),
    )
    preparation = PackagedProductFactoryPreparationService(
        repository=repository,
        tasks=TaskQueue(store),
        host=host,
        workspace_id="packaged.product-factory",
    )
    execution_plan = PackagedProductFactoryExecutionPlan(
        project_id=project.project_id,
        expected_spec_version=project.spec_version,
        expected_row_version=project.row_version,
        graph=graph,
        graph_version=1,
        base_shas={"repo-core": SHA_A},
        component_goals={"core": "Implement core"},
        permission_ceiling=PERMISSIONS,
    )
    prepared = preparation.prepare(execution_plan)
    record = prepared.state.coordinator.snapshot().records[0]
    request = record.request
    if failed and canonical_failure:
        outcomes = asyncio.run(
            host.dispatch_ready(
                host_task_id=prepared.host_task_id,
                state=prepared.state,
                max_parallel=1,
                max_count=1,
            )
        )
        assert len(outcomes) == 1
        assert outcomes[0].state.value == "repair_required"
        request = prepared.state.coordinator.snapshot().records[0].request
    elif failed:
        request = prepared.state.coordinator.start("core")
        prepared.state.coordinator.record_result(
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
                        "worker lacks exact TOML editing capability",
                        retryable=True,
                    ),
                ),
            )
        )
        ProductFactoryCheckpointHost(store).save(
            host_task_id=prepared.host_task_id,
            checkpoint=prepared.state.binding.checkpoint(
                prepared.state.coordinator
            ),
        )
    gap_plan = PackagedProductFactoryCapabilityGapPlan(
        project_id=project.project_id,
        expected_spec_version=project.spec_version,
        expected_row_version=project.row_version,
        expected_graph_digest=prepared.graph_digest,
        component_id="core",
        expected_work_id=request.work_id,
        capability_id="toml-editor",
        attempted_methods=("canonical-registry-search",),
    )
    return store, repository, preparation, prepared, request, gap_plan


def _real_toolsmith(store: SQLiteStore):
    worker = UnusedToolsmithWorker()
    service = CapabilityEscalationService(
        repository=ToolsmithRepository(store),
        checkpoints=CheckpointService(store),
        worker=worker,
    )
    adapter = CodingWorkerComponentAdapter(
        worker=worker,
        contexts=cast(Any, object()),
        evidence=cast(Any, object()),
    )
    bridge = ProductFactoryToolsmithBridge(
        service,
        adapter,
        store=store,
    )
    return service, bridge


def _register_reuse(service: CapabilityEscalationService, checkpoint) -> None:
    candidate = ReuseCandidate(
        capability_id=checkpoint.capability_id,
        version="1.4.0",
        source="canonical-local-registry",
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
        verifier_evidence={"tests": "independent-green"},
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
            source="canonical-local-registry",
        ),
    )


def _restart_packaged_service(
    store: SQLiteStore,
):
    restarted = SQLiteStore(store.path)
    restarted.initialize()
    host = MultiRepositoryProductFactoryHost(restarted, NeverDispatchWorker())
    preparation = PackagedProductFactoryPreparationService(
        repository=ProductProjectRepository(restarted),
        tasks=TaskQueue(restarted),
        host=host,
        workspace_id="packaged.product-factory",
    )
    toolsmith, bridge = _real_toolsmith(restarted)
    return (
        restarted,
        host,
        preparation,
        toolsmith,
        PackagedProductFactoryToolsmithService(
            preparation=preparation,
            bridge=bridge,
        ),
    )


def _service(preparation, bridge: RecordingBridge):
    return PackagedProductFactoryToolsmithService(
        preparation=preparation,
        bridge=cast(ProductFactoryToolsmithBridge, bridge),
    )


def test_begin_gap_uses_exact_current_failed_attempt_and_fixed_reason(
    tmp_path: Path,
) -> None:
    _store, _repository, preparation, prepared, request, plan = _fixture(tmp_path)
    bridge = RecordingBridge()

    result = _service(preparation, bridge).begin_gap(plan)

    assert result is bridge.begin_result
    assert len(bridge.begin_calls) == 1
    (
        actual_request,
        host_task_id,
        capability_id,
        reason,
        attempted_methods,
    ) = bridge.begin_calls[0]
    assert actual_request == request
    assert host_task_id == prepared.host_task_id
    assert capability_id == "toml-editor"
    assert reason == "Product Factory worker capability gap"
    assert attempted_methods == ("canonical-registry-search",)
    assert actual_request.permission_ceiling == PERMISSIONS


def test_stale_work_id_fails_before_toolsmith_effect(tmp_path: Path) -> None:
    _store, _repository, preparation, _prepared, _request, plan = _fixture(tmp_path)
    bridge = RecordingBridge()

    with pytest.raises(
        PackagedProductFactoryToolsmithError,
        match="stale for the current component attempt",
    ):
        _service(preparation, bridge).begin_gap(
            replace(plan, expected_work_id="stale-work-id")
        )

    assert bridge.begin_calls == []


def test_stale_graph_authority_fails_before_toolsmith_effect(tmp_path: Path) -> None:
    _store, _repository, preparation, _prepared, _request, plan = _fixture(tmp_path)
    bridge = RecordingBridge()
    wrong_digest = "0" * 64 if plan.expected_graph_digest != "0" * 64 else "1" * 64

    with pytest.raises(
        PackagedProductFactoryToolsmithError,
        match="stale for the current Product Factory authority",
    ):
        _service(preparation, bridge).begin_gap(
            replace(plan, expected_graph_digest=wrong_digest)
        )

    assert bridge.begin_calls == []


def test_nonfailed_component_cannot_be_promoted_to_capability_gap(
    tmp_path: Path,
) -> None:
    _store, _repository, preparation, _prepared, _request, plan = _fixture(
        tmp_path,
        failed=False,
    )
    bridge = RecordingBridge()

    with pytest.raises(
        PackagedProductFactoryToolsmithError,
        match="repair authority is unavailable",
    ):
        _service(preparation, bridge).begin_gap(plan)

    assert bridge.begin_calls == []


def test_resume_forwards_exact_failed_work_and_capability_identity(
    tmp_path: Path,
) -> None:
    _store, _repository, preparation, prepared, _request, plan = _fixture(tmp_path)
    bridge = RecordingBridge()

    result = _service(preparation, bridge).resume_registered_gap(plan)

    assert result is bridge.resume_result
    assert len(bridge.resume_calls) == 1
    (
        host_task_id,
        binding,
        coordinator,
        component_id,
        expected_work_id,
        expected_capability_id,
    ) = bridge.resume_calls[0]
    assert host_task_id == prepared.host_task_id
    assert binding.project.project_id == plan.project_id
    assert coordinator.snapshot().project_id == plan.project_id
    assert component_id == "core"
    assert expected_work_id == plan.expected_work_id
    assert expected_capability_id == "toml-editor"


def test_project_revision_invalidates_packaged_gap_plan_before_resume(
    tmp_path: Path,
) -> None:
    _store, repository, preparation, _prepared, _request, plan = _fixture(tmp_path)
    bridge = RecordingBridge()
    current = repository.get(plan.project_id)
    repository.update_spec(
        current.project_id,
        replace(
            current.spec,
            desired_outcome="A newer ProductProject revision",
        ),
        expected_row_version=current.row_version,
        change_reason="regression: invalidate stale packaged capability-gap plan",
    )

    with pytest.raises(
        PackagedProductFactoryToolsmithError,
        match="current Product Factory authority is unavailable",
    ):
        _service(preparation, bridge).resume_registered_gap(plan)

    assert bridge.resume_calls == []


@pytest.mark.parametrize(
    "kwargs, message",
    [
        (
            {"attempted_methods": tuple(f"method-{index}" for index in range(17))},
            "bounded evidence limit",
        ),
        (
            {"attempted_methods": ("registry", "registry")},
            "must not contain duplicates",
        ),
        (
            {"capability_id": "unsafe\ncapability"},
            "without control characters",
        ),
        (
            {"expected_graph_digest": "not-a-digest"},
            "64-character hexadecimal digest",
        ),
    ],
)
def test_gap_plan_rejects_noncanonical_or_unbounded_authority(
    tmp_path: Path,
    kwargs,
    message: str,
) -> None:
    _store, _repository, _preparation, _prepared, _request, plan = _fixture(tmp_path)

    with pytest.raises(PackagedProductFactoryToolsmithError, match=message):
        replace(plan, **kwargs)



@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("expected_spec_version", True, "positive integer"),
        ("expected_row_version", False, "non-negative integer"),
        ("capability_id", "unsafe\ncapability", "without control characters"),
        ("attempted_methods", ["registry"], "must be a tuple"),
    ],
)
def test_begin_gap_revalidates_tampered_frozen_plan_before_effect(
    tmp_path: Path,
    field: str,
    value,
    message: str,
) -> None:
    _store, _repository, preparation, _prepared, _request, plan = _fixture(tmp_path)
    bridge = RecordingBridge()
    object.__setattr__(plan, field, value)

    with pytest.raises(PackagedProductFactoryToolsmithError, match=message):
        _service(preparation, bridge).begin_gap(plan)

    assert bridge.begin_calls == []


def test_resume_revalidates_tampered_frozen_plan_before_effect(tmp_path: Path) -> None:
    _store, _repository, preparation, _prepared, _request, plan = _fixture(tmp_path)
    bridge = RecordingBridge()
    object.__setattr__(plan, "expected_spec_version", True)

    with pytest.raises(PackagedProductFactoryToolsmithError, match="positive integer"):
        _service(preparation, bridge).resume_registered_gap(plan)

    assert bridge.resume_calls == []


def test_effect_uses_detached_plan_snapshot_when_original_is_mutated(
    tmp_path: Path,
) -> None:
    _store, _repository, preparation, _prepared, _request, plan = _fixture(tmp_path)
    bridge = RecordingBridge()

    class MutatingPreparation:
        def restore(self, project_id: str):
            restored = preparation.restore(project_id)
            object.__setattr__(plan, "capability_id", "mutated-after-entry")
            object.__setattr__(plan, "attempted_methods", ("mutated-after-entry",))
            return restored

        def require_repair_request(self, project_id: str, component_id: str):
            return preparation.require_repair_request(project_id, component_id)

    service = PackagedProductFactoryToolsmithService(
        preparation=cast(
            PackagedProductFactoryPreparationService,
            MutatingPreparation(),
        ),
        bridge=cast(ProductFactoryToolsmithBridge, bridge),
    )

    service.begin_gap(plan)

    assert len(bridge.begin_calls) == 1
    assert bridge.begin_calls[0][2] == "toml-editor"
    assert bridge.begin_calls[0][4] == ("canonical-registry-search",)

def test_bridge_exact_work_guard_blocks_before_resume_effect(tmp_path: Path) -> None:
    store, _repository, _preparation, prepared, request, _plan = _fixture(tmp_path)
    escalation = RecordingEscalation()
    bridge = ProductFactoryToolsmithBridge(
        escalation,
        cast(CodingWorkerComponentAdapter, object()),
        store=store,
    )
    bridge.begin_durable_gap(
        request,
        host_task_id=prepared.host_task_id,
        capability_id="toml-editor",
        reason="trusted durable worker capability gap",
    )

    with pytest.raises(ProductFactoryToolsmithError, match="different failed work"):
        bridge.resume_durable_registered_gap(
            host_task_id=prepared.host_task_id,
            binding=prepared.state.binding,
            coordinator=prepared.state.coordinator,
            component_id="core",
            expected_work_id="different-work-id",
            expected_capability_id="toml-editor",
        )

    assert escalation.resumed == []


def test_bridge_exact_capability_guard_blocks_before_resume_effect(
    tmp_path: Path,
) -> None:
    store, _repository, _preparation, prepared, request, _plan = _fixture(tmp_path)
    escalation = RecordingEscalation()
    bridge = ProductFactoryToolsmithBridge(
        escalation,
        cast(CodingWorkerComponentAdapter, object()),
        store=store,
    )
    bridge.begin_durable_gap(
        request,
        host_task_id=prepared.host_task_id,
        capability_id="toml-editor",
        reason="trusted durable worker capability gap",
    )

    with pytest.raises(ProductFactoryToolsmithError, match="different capability"):
        bridge.resume_durable_registered_gap(
            host_task_id=prepared.host_task_id,
            binding=prepared.state.binding,
            coordinator=prepared.state.coordinator,
            component_id="core",
            expected_work_id=request.work_id,
            expected_capability_id="different-capability",
        )

    assert escalation.resumed == []


def test_bridge_exact_guards_preserve_current_none_resume_behavior(
    tmp_path: Path,
) -> None:
    store, _repository, _preparation, prepared, request, _plan = _fixture(tmp_path)
    escalation = RecordingEscalation()
    bridge = ProductFactoryToolsmithBridge(
        escalation,
        cast(CodingWorkerComponentAdapter, object()),
        store=store,
    )
    bridge.begin_durable_gap(
        request,
        host_task_id=prepared.host_task_id,
        capability_id="toml-editor",
        reason="trusted durable worker capability gap",
    )

    result = bridge.resume_durable_registered_gap(
        host_task_id=prepared.host_task_id,
        binding=prepared.state.binding,
        coordinator=prepared.state.coordinator,
        component_id="core",
        expected_work_id=request.work_id,
        expected_capability_id="toml-editor",
    )

    assert result is None
    assert escalation.resumed == [(prepared.host_task_id, "toml-editor")]


def test_real_registered_capability_resumes_factory_with_durable_repair_lineage(
    tmp_path: Path,
) -> None:
    store, _repository, preparation, prepared, request, plan = _fixture(
        tmp_path,
        canonical_failure=True,
    )
    toolsmith, bridge = _real_toolsmith(store)
    packaged = PackagedProductFactoryToolsmithService(
        preparation=preparation,
        bridge=bridge,
    )
    checkpoint = packaged.begin_gap(plan)
    _register_reuse(toolsmith, checkpoint)

    restarted, _host, _preparation, _toolsmith, packaged = (
        _restart_packaged_service(store)
    )
    resumed = packaged.resume_registered_gap(plan)

    assert resumed is not None
    assert resumed.previous_work_id == request.work_id
    assert resumed.next_request.attempt == 2
    assert resumed.next_request.base_sha == SHA_B
    assert resumed.next_request.permission_ceiling == PERMISSIONS
    assert resumed.capability_version == "1.4.0"
    assert resumed.capability_digest == CAPABILITY_DIGEST

    _final_store, final_host, final_preparation, _toolsmith, _packaged = (
        _restart_packaged_service(restarted)
    )
    final = final_preparation.restore(plan.project_id)
    record = final.state.coordinator.snapshot().records[0]
    lineage = final_host.repair_lineage(
        host_task_id=final.host_task_id,
        state=final.state,
    )
    assert record.state.value == "ready"
    assert record.request == resumed.next_request
    assert len(lineage) == 1
    assert lineage[0].from_work_id == request.work_id
    assert lineage[0].to_work_id == resumed.next_request.work_id
    assert lineage[0].from_result_sha == SHA_B
    assert lineage[0].to_base_sha == SHA_B


def test_toolsmith_resume_recovers_after_failure_before_factory_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _repository, preparation, prepared, request, plan = _fixture(
        tmp_path,
        canonical_failure=True,
    )
    toolsmith, bridge = _real_toolsmith(store)
    packaged = PackagedProductFactoryToolsmithService(
        preparation=preparation,
        bridge=bridge,
    )
    checkpoint = packaged.begin_gap(plan)
    _register_reuse(toolsmith, checkpoint)

    def fail_commit(*_args, **_kwargs):
        raise OSError("simulated Factory checkpoint outage")

    monkeypatch.setattr(preparation, "commit_repair", fail_commit)
    with pytest.raises(
        PackagedProductFactoryToolsmithError,
        match="resume was rejected",
    ):
        packaged.resume_registered_gap(plan)

    monkeypatch.undo()
    restarted, _host, _preparation, _toolsmith, packaged = (
        _restart_packaged_service(store)
    )
    resumed = packaged.resume_registered_gap(plan)
    assert resumed is not None
    assert resumed.previous_work_id == request.work_id
    assert resumed.next_request.attempt == 2
    assert resumed.next_request.base_sha == SHA_B

    _final_store, final_host, final_preparation, _toolsmith, _packaged = (
        _restart_packaged_service(restarted)
    )
    final = final_preparation.restore(plan.project_id)
    lineage = final_host.repair_lineage(
        host_task_id=final.host_task_id,
        state=final.state,
    )
    assert len(lineage) == 1
    assert lineage[0].to_work_id == resumed.next_request.work_id


def test_repair_preview_is_side_effect_free_before_toolsmith_binding_commit(
    tmp_path: Path,
) -> None:
    _store, _repository, preparation, prepared, request, _plan = _fixture(tmp_path)
    before = prepared.state.coordinator.snapshot()

    preview = preparation.preview_repair(
        prepared,
        component_id="core",
        reason="registered capability toml-editor is ready",
    )

    assert preview.attempt == 2
    assert preview.base_sha == SHA_B
    assert prepared.state.coordinator.snapshot() == before
    assert prepared.state.coordinator.snapshot().records[0].request == request


def test_commit_repair_rejects_changed_preview_identity_without_state_advance(
    tmp_path: Path,
) -> None:
    _store, _repository, preparation, prepared, request, _plan = _fixture(tmp_path)
    before = prepared.state.coordinator.snapshot()

    with pytest.raises(
        RepairLineageError,
        match="current repair preview differs from the expected next work identity",
    ):
        preparation.commit_repair(
            prepared,
            component_id="core",
            reason="registered capability toml-editor is ready",
            expected_next_work_id="stale-next-work-id",
        )

    assert prepared.state.coordinator.snapshot() == before
    assert prepared.state.coordinator.snapshot().records[0].request == request


def test_project_revision_between_repair_preview_and_commit_fails_closed(
    tmp_path: Path,
) -> None:
    _store, repository, preparation, prepared, request, _plan = _fixture(
        tmp_path,
        canonical_failure=True,
    )
    preview = preparation.preview_repair(
        prepared,
        component_id="core",
        reason="registered capability toml-editor is ready",
    )
    before = prepared.state.coordinator.snapshot()
    current = repository.get(request.project_id)
    repository.update_spec(
        current.project_id,
        replace(
            current.spec,
            desired_outcome="Newer owner-approved ProductProject revision",
        ),
        expected_row_version=current.row_version,
        change_reason="regression: revise after repair preview",
    )

    with pytest.raises(
        MultiRepositoryExecutionError,
        match="ProductProject changed before durable Product Factory authority publication",
    ):
        preparation.commit_repair(
            prepared,
            component_id="core",
            reason="registered capability toml-editor is ready",
            expected_next_work_id=preview.work_id,
        )

    assert prepared.state.coordinator.snapshot() == before
    assert prepared.state.coordinator.snapshot().records[0].request == request


def test_toolsmith_resume_recovers_after_factory_checkpoint_before_binding_finalize(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _repository, preparation, prepared, request, plan = _fixture(
        tmp_path,
        canonical_failure=True,
    )
    toolsmith, bridge = _real_toolsmith(store)
    packaged = PackagedProductFactoryToolsmithService(
        preparation=preparation,
        bridge=bridge,
    )
    checkpoint = packaged.begin_gap(plan)
    _register_reuse(toolsmith, checkpoint)

    def fail_consumed(_self, _binding):
        raise ProductFactoryToolsmithBindingError(
            "simulated binding finalization outage"
        )

    monkeypatch.setattr(
        ProductFactoryToolsmithBindingRepository,
        "mark_consumed",
        fail_consumed,
    )
    with pytest.raises(
        PackagedProductFactoryToolsmithError,
        match="resume was rejected",
    ):
        packaged.resume_registered_gap(plan)

    # Factory lineage/checkpoint is already authoritative even though the Toolsmith
    # two-phase binding deliberately remains RESUME_PREPARED.
    assert prepared.state.coordinator.snapshot().records[0].state.value == "ready"

    monkeypatch.undo()
    restarted, final_host, final_preparation, _toolsmith, packaged = (
        _restart_packaged_service(store)
    )
    resumed = packaged.resume_registered_gap(plan)
    assert resumed is not None
    assert resumed.previous_work_id == request.work_id
    assert resumed.next_request.attempt == 2
    assert resumed.next_request.base_sha == SHA_B

    final = final_preparation.restore(plan.project_id)
    lineage = final_host.repair_lineage(
        host_task_id=final.host_task_id,
        state=final.state,
    )
    assert len(lineage) == 1
    assert lineage[0].to_work_id == resumed.next_request.work_id

    binding = ProductFactoryToolsmithBindingRepository(restarted).require(
        host_task_id=final.host_task_id,
        work_id=request.work_id,
    )
    assert binding.state.value == "consumed"
