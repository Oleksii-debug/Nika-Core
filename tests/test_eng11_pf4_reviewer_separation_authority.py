from __future__ import annotations

from dataclasses import fields

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.product_factory_coordinator import (
    CoordinatorError,
    ProductFactoryCoordinator,
    ReviewDecision,
    WorkerResultEnvelope,
    WorkState,
)
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

SHA_A = "a" * 40
SHA_B = "b" * 40
DIGEST = "d" * 64
PROJECT_ID = "qa-pf4-reviewer-separation"
SAME_ACTOR_ID = "worker-and-reviewer-same-actor"
PRODUCER_ID = "trusted-producer-actor"
UNASSIGNED_REVIEWER_ID = "invented-unassigned-reviewer"
ACTOR_FIELD_CANDIDATES = (
    "worker_id",
    "worker_actor_id",
    "producer_id",
    "producer_actor_id",
    "author_id",
    "actor_id",
)


def _graph() -> ProductRepositoryGraph:
    return ProductRepositoryGraph(
        project_id=PROJECT_ID,
        repositories=(
            RepositoryRef("repo-main", "github", "owner/product", "main"),
        ),
        components=(
            ProductComponent(
                component_id="core",
                repository_id="repo-main",
                paths=("src/core",),
                test_commands=(("python", "-m", "pytest", "tests/core"),),
            ),
            ProductComponent(
                component_id="downstream",
                repository_id="repo-main",
                paths=("src/downstream",),
                dependencies=("core",),
                test_commands=(("python", "-m", "pytest", "tests/downstream"),),
            ),
        ),
    )


def _planned_coordinator() -> ProductFactoryCoordinator:
    coordinator = ProductFactoryCoordinator(_graph())
    coordinator.plan(
        base_shas={"repo-main": SHA_A},
        goals={
            "core": "Implement the acceptance-scoped core component",
            "downstream": "Implement the dependent component",
        },
        permission_ceiling=frozenset(
            {"read_source", "write_source", "run_tests"}
        ),
    )
    return coordinator


def _record(coordinator: ProductFactoryCoordinator, component_id: str):
    return next(
        record
        for record in coordinator.snapshot().records
        if record.request.component_id == component_id
    )


def _producer_identity_binding() -> tuple[str, str] | None:
    envelope_fields = {item.name for item in fields(WorkerResultEnvelope)}
    coding_result_fields = {item.name for item in fields(CodingResult)}
    for name in ACTOR_FIELD_CANDIDATES:
        if name in envelope_fields:
            return "envelope", name
        if name in coding_result_fields:
            return "coding_result", name
    return None


def _candidate_result(
    request,
    *,
    producer_id: str,
    require_producer_identity: bool,
) -> WorkerResultEnvelope:
    binding = _producer_identity_binding()
    if require_producer_identity and binding is None:
        pytest.fail(
            "PF4 cannot prove independent review: successful candidate evidence "
            "carries no producer/worker actor identity that ProductFactoryCoordinator "
            "can compare with ReviewDecision.reviewer_id"
        )

    coding_kwargs: dict[str, object] = {
        "job_id": request.work_id,
        "test_evidence": (
            TestEvidence(
                request.acceptance_commands[0],
                0,
                "qa-passing-evidence",
            ),
        ),
    }
    envelope_kwargs: dict[str, object] = {
        "work_id": request.work_id,
        "component_id": request.component_id,
        "repository_id": request.repository_id,
        "base_sha": request.base_sha,
        "result_sha": SHA_B,
        "diff_digest": DIGEST,
    }
    if binding is not None:
        location, actor_field = binding
        if location == "coding_result":
            coding_kwargs[actor_field] = producer_id
        else:
            envelope_kwargs[actor_field] = producer_id

    envelope_kwargs["coding_result"] = CodingResult(**coding_kwargs)
    return WorkerResultEnvelope(**envelope_kwargs)


class _NoopProgramWorker:
    async def dispatch(self, request):  # pragma: no cover - security test forbids dispatch
        raise AssertionError(f"unexpected dispatch: {request.work_id}")

    async def inspect(self, work_id):  # pragma: no cover - security test forbids inspect
        raise AssertionError(f"unexpected inspect: {work_id}")

    async def recover(self, request, state):  # pragma: no cover - security test forbids recover
        raise AssertionError(f"unexpected recover: {request.work_id} {state}")


def test_pf4_same_actor_cannot_accept_its_own_candidate() -> None:
    coordinator = _planned_coordinator()
    request = coordinator.start("core")
    coordinator.record_result(
        _candidate_result(
            request,
            producer_id=SAME_ACTOR_ID,
            require_producer_identity=True,
        )
    )

    with pytest.raises(
        CoordinatorError,
        match=r"(?i)(independent|reviewer|worker|producer|self)",
    ):
        coordinator.review(
            "core",
            ReviewDecision(
                reviewer_id=SAME_ACTOR_ID,
                accepted=True,
                reason="same actor attempts to approve its own candidate",
                evidence_refs=("qa:eng11:self-review",),
            ),
        )

    record = _record(coordinator, "core")
    assert record.state is WorkState.REVIEW_REQUIRED
    assert record.review is None


def test_pf4_untrusted_reviewer_string_cannot_promote_candidate() -> None:
    coordinator = _planned_coordinator()
    request = coordinator.start("core")
    coordinator.record_result(
        _candidate_result(
            request,
            producer_id=PRODUCER_ID,
            require_producer_identity=False,
        )
    )

    with pytest.raises(
        CoordinatorError,
        match=r"(?i)(trusted|independent|reviewer|evidence|authority)",
    ):
        coordinator.review(
            "core",
            ReviewDecision(
                reviewer_id=UNASSIGNED_REVIEWER_ID,
                accepted=True,
                reason="invented reviewer attempts candidate promotion",
                evidence_refs=("invented:untrusted-review-evidence",),
            ),
        )

    record = _record(coordinator, "core")
    assert record.state is WorkState.REVIEW_REQUIRED
    assert record.review is None


def test_pf4_forged_review_cannot_become_durable_restart_authority(tmp_path) -> None:
    graph = _graph()
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    projects = ProductProjectRepository(store)
    project = projects.create(
        project_id=PROJECT_ID,
        name="ENG11 PF4 reviewer authority",
        spec=ProductProjectSpec(
            goal="Prove reviewer-separated Product Factory promotion",
            desired_outcome="Only trusted independent review can promote candidates",
            requirements=(
                ProductRequirement(
                    "req-review-authority",
                    "Candidate promotion requires trusted independent review",
                    ("Forged reviewer evidence cannot become durable authority",),
                ),
            ),
            repository_refs=("owner/product",),
        ),
        idempotency_key="eng11:create-project",
    )
    binding = ProductProjectCoordinatorBinding(project, graph)
    task = TaskQueue(store).create(
        workspace_id="eng11-workspace",
        agent_id="product-factory",
        payload={"kind": "product_factory", "product_project_id": PROJECT_ID},
    )
    coordinator = binding.plan(
        base_shas={"repo-main": SHA_A},
        component_goals={
            "core": "Implement the acceptance-scoped core component",
            "downstream": "Implement the dependent component",
        },
        permission_ceiling=frozenset(
            {"read_source", "write_source", "run_tests"}
        ),
    )
    request = coordinator.start("core")
    coordinator.record_result(
        _candidate_result(
            request,
            producer_id=PRODUCER_ID,
            require_producer_identity=False,
        )
    )

    host = ProductFactoryProgramHost(store, _NoopProgramWorker())
    try:
        host.review_and_checkpoint(
            host_task_id=task.task_id,
            binding=binding,
            coordinator=coordinator,
            component_id="core",
            decision=ReviewDecision(
                reviewer_id=UNASSIGNED_REVIEWER_ID,
                accepted=True,
                reason="forged reviewer attempts durable promotion",
                evidence_refs=("invented:durable-review-evidence",),
            ),
        )
    except (CoordinatorError, ProductFactoryProgramError, PermissionError):
        record = _record(coordinator, "core")
        assert record.state is WorkState.REVIEW_REQUIRED
        return

    restarted_store = SQLiteStore(store.path)
    restarted_store.initialize()
    restarted_project = ProductProjectRepository(restarted_store).get(PROJECT_ID)
    restarted_binding = ProductProjectCoordinatorBinding(restarted_project, graph)
    restored = ProductFactoryProgramHost(
        restarted_store,
        _NoopProgramWorker(),
    ).restore_latest(
        host_task_id=task.task_id,
        binding=restarted_binding,
    )

    assert _record(restored, "core").state is not WorkState.ACCEPTED
    assert "downstream" not in {
        request.component_id for request in restored.ready_requests()
    }
