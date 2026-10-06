from pathlib import Path

import pytest

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
from nika_core.product_factory_toolsmith_integration import (
    ProductFactoryToolsmithBridge,
    ProductFactoryToolsmithError,
)
from nika_core.toolsmith.contracts import (
    CandidateState,
    CodingResult,
    IsolationClass,
    NetworkPolicy,
    ProcessPolicy,
    ResourceBudget,
    WorkerFailure,
    WorkerFailureKind,
    WorkspaceLease,
)

SHA_A = "a" * 40
SHA_B = "b" * 40
DIGEST = "d" * 64
PERMISSIONS = frozenset({"read_source", "write_source", "run_tests"})


def _coordinator(*, failure_kind=WorkerFailureKind.PROCESS_FAILED, retryable=True):
    graph = ProductRepositoryGraph(
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
    coordinator = ProductFactoryCoordinator(graph)
    coordinator.plan(
        base_shas={"repo-1": SHA_A},
        goals={"core": "build core"},
        permission_ceiling=PERMISSIONS,
    )
    request = coordinator.start("core")
    result = CodingResult(
        job_id=request.work_id,
        failure=WorkerFailure(failure_kind, "missing parser capability", retryable=retryable),
    )
    coordinator.record_result(
        WorkerResultEnvelope(
            work_id=request.work_id,
            component_id=request.component_id,
            repository_id=request.repository_id,
            base_sha=request.base_sha,
            result_sha=SHA_B,
            diff_digest=DIGEST,
            coding_result=result,
        )
    )
    return coordinator, request


class UnusedWorker:
    async def execute(self, _job):
        raise AssertionError("worker execution is not expected")

    async def cancel(self, _job_id):
        raise AssertionError("worker cancellation is not expected")

    async def inspect(self, _job_id):
        raise AssertionError("worker inspection is not expected")

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
            DIGEST,
        )


class FakeEscalation:
    def __init__(self):
        self.begun = []
        self.begin_result = (1, CandidateState.PROPOSED)
        self.resume = None
        self.resume_calls = []

    def begin(self, gap):
        self.begun.append(gap)
        return self.begin_result

    def reconcile_resume(self, *, task_id, capability_id):
        self.resume_calls.append((task_id, capability_id))
        return self.resume


def _bridge(escalation):
    return ProductFactoryToolsmithBridge(
        escalation,
        CodingWorkerComponentAdapter(UnusedWorker(), UnusedContexts(), UnusedEvidence()),
    )


@pytest.mark.parametrize(
    "begin_result",
    (
        (True, CandidateState.PROPOSED),
        (1, "proposed"),
        [1, CandidateState.PROPOSED],
    ),
)
def test_malformed_toolsmith_begin_identity_fails_closed_before_product_change(
    begin_result: object,
) -> None:
    coordinator, request = _coordinator()
    escalation = FakeEscalation()
    escalation.begin_result = begin_result
    bridge = _bridge(escalation)
    before = coordinator.snapshot()

    with pytest.raises(ProductFactoryToolsmithError, match="invalid escalation identity"):
        bridge.begin_gap(request, capability_id="toml-editor", reason="missing")

    assert coordinator.snapshot() == before


def test_gap_is_bound_to_one_component_task_and_original_permission_ceiling() -> None:
    coordinator, request = _coordinator()
    escalation = FakeEscalation()
    bridge = _bridge(escalation)

    checkpoint = bridge.begin_gap(
        request,
        capability_id="python-toml-editor",
        reason="current bounded worker lacks safe TOML editing",
        attempted_methods=("reuse-search", "existing-tool-registry"),
    )

    assert checkpoint.work_id == request.work_id
    assert checkpoint.component_id == "core"
    assert checkpoint.task_id == "product:project-1:component:core"
    assert checkpoint.gap.task_id == checkpoint.task_id
    assert checkpoint.gap.permission_ceiling == PERMISSIONS
    assert checkpoint.gap.attempted_methods == ("reuse-search", "existing-tool-registry")
    assert coordinator.snapshot().records[0].state is WorkState.REPAIR_REQUIRED


def test_unregistered_gap_does_not_advance_product_factory_attempt() -> None:
    coordinator, request = _coordinator()
    escalation = FakeEscalation()
    bridge = _bridge(escalation)
    checkpoint = bridge.begin_gap(request, capability_id="toml-editor", reason="missing")

    resumed = bridge.resume_registered_gap(coordinator, checkpoint)

    assert resumed is None
    record = coordinator.snapshot().records[0]
    assert record.request.work_id == request.work_id
    assert record.request.attempt == 1
    assert record.state is WorkState.REPAIR_REQUIRED


def test_registered_gap_resumes_same_component_from_exact_failed_result_sha() -> None:
    coordinator, request = _coordinator()
    escalation = FakeEscalation()
    bridge = _bridge(escalation)
    checkpoint = bridge.begin_gap(request, capability_id="toml-editor", reason="missing")
    escalation.resume = {
        "task_id": checkpoint.task_id,
        "capability_id": checkpoint.capability_id,
        "version": "1.4.0",
        "digest": "f" * 64,
    }

    resumed = bridge.resume_registered_gap(coordinator, checkpoint)

    assert resumed is not None
    assert resumed.previous_work_id == request.work_id
    assert resumed.next_request.attempt == 2
    assert resumed.next_request.base_sha == SHA_B
    assert resumed.next_request.allowed_paths == request.allowed_paths
    assert resumed.next_request.permission_ceiling == request.permission_ceiling
    assert resumed.capability_version == "1.4.0"
    assert resumed.capability_digest == "f" * 64
    assert "Toolsmith capability registered" in resumed.next_request.goal


def test_stale_component_attempt_cannot_consume_old_gap_registration() -> None:
    coordinator, request = _coordinator()
    escalation = FakeEscalation()
    bridge = _bridge(escalation)
    checkpoint = bridge.begin_gap(request, capability_id="toml-editor", reason="missing")
    bridge.worker_adapter.prepare_safe_repair(coordinator, "core", reason="manual retry")
    escalation.resume = {
        "task_id": checkpoint.task_id,
        "capability_id": checkpoint.capability_id,
        "version": "1.0.0",
        "digest": "f" * 64,
    }

    with pytest.raises(ProductFactoryToolsmithError, match="stale component attempt"):
        bridge.resume_registered_gap(coordinator, checkpoint)


def test_wrong_toolsmith_resume_identity_is_rejected_before_product_state_change() -> None:
    coordinator, request = _coordinator()
    escalation = FakeEscalation()
    bridge = _bridge(escalation)
    checkpoint = bridge.begin_gap(request, capability_id="toml-editor", reason="missing")
    escalation.resume = {
        "task_id": checkpoint.task_id,
        "capability_id": "other-capability",
        "version": "1.0.0",
        "digest": "f" * 64,
    }

    with pytest.raises(ProductFactoryToolsmithError, match="different capability"):
        bridge.resume_registered_gap(coordinator, checkpoint)

    record = coordinator.snapshot().records[0]
    assert record.request.work_id == request.work_id
    assert record.state is WorkState.REPAIR_REQUIRED


def test_non_dict_toolsmith_resume_identity_fails_closed_before_product_change() -> None:
    coordinator, request = _coordinator()
    escalation = FakeEscalation()
    bridge = _bridge(escalation)
    checkpoint = bridge.begin_gap(request, capability_id="toml-editor", reason="missing")
    escalation.resume = ["task_id", "capability_id", "version", "digest"]
    before = coordinator.snapshot()

    with pytest.raises(ProductFactoryToolsmithError, match="exact dictionary"):
        bridge.resume_registered_gap(coordinator, checkpoint)

    assert coordinator.snapshot() == before


@pytest.mark.parametrize(
    ("field", "bad_value"),
    (
        ("version", 1),
        ("digest", None),
    ),
)
def test_malformed_toolsmith_resume_identity_fails_closed_before_product_change(
    field: str,
    bad_value: object,
) -> None:
    coordinator, request = _coordinator()
    escalation = FakeEscalation()
    bridge = _bridge(escalation)
    checkpoint = bridge.begin_gap(request, capability_id="toml-editor", reason="missing")
    resume_identity: dict[str, object] = {
        "task_id": checkpoint.task_id,
        "capability_id": checkpoint.capability_id,
        "version": "1.0.0",
        "digest": "f" * 64,
    }
    resume_identity[field] = bad_value
    escalation.resume = resume_identity
    before = coordinator.snapshot()

    with pytest.raises(ProductFactoryToolsmithError, match="values must be text"):
        bridge.resume_registered_gap(coordinator, checkpoint)

    assert coordinator.snapshot() == before


def test_policy_failure_cannot_be_laundered_into_toolsmith_auto_repair() -> None:
    coordinator, request = _coordinator(
        failure_kind=WorkerFailureKind.POLICY_VIOLATION,
        retryable=True,
    )
    escalation = FakeEscalation()
    bridge = _bridge(escalation)
    checkpoint = bridge.begin_gap(request, capability_id="toml-editor", reason="missing")
    escalation.resume = {
        "task_id": checkpoint.task_id,
        "capability_id": checkpoint.capability_id,
        "version": "1.0.0",
        "digest": "f" * 64,
    }

    with pytest.raises(ProductFactoryToolsmithError, match="not eligible for automatic repair"):
        bridge.resume_registered_gap(coordinator, checkpoint)