from __future__ import annotations

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.product_factory_checkpoint_host import ProductFactoryCheckpointHost
from nika_core.product_factory_coordinator import (
    CoordinatorError,
    ReviewDecision,
    WorkerResultEnvelope,
    WorkState,
)
from nika_core.product_factory_orchestration import (
    ProductComponent,
    ProductRepositoryGraph,
    RepositoryRef,
    TeamPlan,
    TeamRole,
)
from nika_core.product_factory_program_host import (
    ProductFactoryProgramError,
    ProductFactoryProgramHost,
)
from nika_core.product_factory_project_binding import ProductProjectCoordinatorBinding
from nika_core.product_factory_review_authority import (
    ProductFactoryReviewSubject,
    team_plan_fingerprint_ref,
)
from nika_core.product_project import (
    ProductProjectRepository,
    ProductProjectSpec,
    ProductRequirement,
)
from nika_core.toolsmith.contracts import CodingResult, TestEvidence

PROJECT_ID = "pf4-program-host-trusted-review"
REPOSITORY_LOCATOR = "owner/pf4-program-host-trusted-review"
SHA_A = "a" * 40
SHA_B = "b" * 40
DIFF_DIGEST = "d" * 64
PERMISSIONS = frozenset({"read_source", "write_source", "run_tests"})
PRODUCER = "team-role:builder"
REVIEWER = "team-role:qa"
TRUSTED_EVIDENCE = ("review-evidence:trusted:1",)


class _NoWorker:
    async def dispatch(self, request):  # pragma: no cover - must never be reached here
        raise AssertionError(f"unexpected dispatch for {request.work_id}")

    async def inspect(self, work_id):  # pragma: no cover - must never be reached here
        raise AssertionError(f"unexpected inspect for {work_id}")

    async def recover(self, request, state):  # pragma: no cover - must never be reached here
        raise AssertionError(f"unexpected recovery for {request.work_id}: {state}")


class _ExactEvidenceAuthority:
    def verify(
        self,
        subject: ProductFactoryReviewSubject,
        evidence_refs: tuple[str, ...],
    ) -> bool:
        return (
            subject.project_id == PROJECT_ID
            and subject.producer_actor_id == PRODUCER
            and subject.reviewer_id == REVIEWER
            and subject.accepted is True
            and evidence_refs == TRUSTED_EVIDENCE
        )


class _AllowAllAuthority:
    def verify(self, subject, evidence_refs):
        return True


def _graph() -> ProductRepositoryGraph:
    return ProductRepositoryGraph(
        project_id=PROJECT_ID,
        repositories=(
            RepositoryRef("repo", "github", REPOSITORY_LOCATOR, "main"),
        ),
        components=(
            ProductComponent(
                component_id="core",
                repository_id="repo",
                paths=("src/core",),
                test_commands=(("python", "-m", "pytest", "tests/core"),),
            ),
            ProductComponent(
                component_id="ui",
                repository_id="repo",
                paths=("src/ui",),
                dependencies=("core",),
                test_commands=(("python", "-m", "pytest", "tests/ui"),),
            ),
        ),
    )


def _team_plan() -> TeamPlan:
    return TeamPlan(
        project_id=PROJECT_ID,
        plan_id="team-plan:pf4-program-host",
        roles=(
            TeamRole(
                role_id=PRODUCER,
                capabilities=("implementation",),
                component_ids=("core", "ui"),
                permissions=PERMISSIONS,
                reasons=("canonical implementation owner",),
            ),
            TeamRole(
                role_id=REVIEWER,
                capabilities=("qa",),
                component_ids=("core", "ui"),
                permissions=frozenset({"read_source", "run_tests"}),
                reasons=("canonical independent reviewer",),
                independent_review=True,
            ),
        ),
        permission_ceiling=PERMISSIONS,
        reasons=("trusted PF4 production composition",),
    )


def _project(store: SQLiteStore, plan: TeamPlan):
    repository = ProductProjectRepository(store)
    project = repository.create(
        project_id=PROJECT_ID,
        name="PF4 trusted ProgramHost review",
        spec=ProductProjectSpec(
            goal="Complete a trusted two-stage Product Factory product",
            desired_outcome="Independent review unlocks only the dependent stage",
            requirements=(
                ProductRequirement(
                    "req-review",
                    "Every candidate requires trusted independent review",
                    ("trusted review survives restart",),
                ),
            ),
            repository_refs=(REPOSITORY_LOCATOR,),
            team_refs=(plan.plan_id, team_plan_fingerprint_ref(plan)),
        ),
        idempotency_key="create:pf4-program-host-trusted-review",
    )
    return repository, project


def _binding(project, plan, evidence_authority):
    return ProductProjectCoordinatorBinding(
        project,
        _graph(),
        team_plan=plan,
        review_evidence_authority=evidence_authority,
    )


def _plan(binding: ProductProjectCoordinatorBinding):
    return binding.plan(
        base_shas={"repo": SHA_A},
        component_goals={"core": "Implement core", "ui": "Implement UI after core"},
        permission_ceiling=PERMISSIONS,
    )


def _record_candidate(coordinator) -> None:
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
                test_evidence=(
                    TestEvidence(request.acceptance_commands[0], 0, "tests-pass"),
                ),
            ),
            producer_actor_id=PRODUCER,
        )
    )


def _state(coordinator, component_id: str) -> WorkState:
    return next(
        record.state
        for record in coordinator.snapshot().records
        if record.request.component_id == component_id
    )


def test_program_host_owns_review_authority_and_legitimate_review_survives_restart(
    tmp_path,
) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    plan = _team_plan()
    projects, project = _project(store, plan)
    task = TaskQueue(store).create(
        workspace_id="ws-pf4",
        agent_id="product-factory",
        payload={"kind": "product_factory", "product_project_id": PROJECT_ID},
    )
    authority = _ExactEvidenceAuthority()
    binding = _binding(project, plan, authority)
    coordinator = _plan(binding)
    checkpoints = ProductFactoryCheckpointHost(store)
    checkpoints.save(
        host_task_id=task.task_id,
        checkpoint=binding.checkpoint(coordinator),
    )
    _record_candidate(coordinator)
    checkpoints.save(
        host_task_id=task.task_id,
        checkpoint=binding.checkpoint(coordinator),
    )
    host = ProductFactoryProgramHost(
        store,
        _NoWorker(),
        review_evidence_authority=authority,
    )

    rogue_binding = _binding(project, plan, _AllowAllAuthority())
    rogue_coordinator = _plan(rogue_binding)
    _record_candidate(rogue_coordinator)
    with pytest.raises(ProductFactoryProgramError, match="ProgramHost-owned"):
        host.review_and_checkpoint(
            host_task_id=task.task_id,
            binding=rogue_binding,
            coordinator=rogue_coordinator,
            component_id="core",
            decision=ReviewDecision(
                reviewer_id="invented-reviewer",
                accepted=True,
                reason="caller injected allow-all authority",
                evidence_refs=("forged",),
            ),
        )

    denied = (
        ReviewDecision(
            reviewer_id=REVIEWER,
            accepted=True,
            reason="forged evidence",
            evidence_refs=("forged",),
        ),
        ReviewDecision(
            reviewer_id="invented-reviewer",
            accepted=True,
            reason="unassigned reviewer",
            evidence_refs=TRUSTED_EVIDENCE,
        ),
        ReviewDecision(
            reviewer_id=PRODUCER,
            accepted=True,
            reason="producer cannot self review",
            evidence_refs=TRUSTED_EVIDENCE,
        ),
    )
    for decision in denied:
        with pytest.raises(CoordinatorError):
            host.review_and_checkpoint(
                host_task_id=task.task_id,
                binding=binding,
                coordinator=coordinator,
                component_id="core",
                decision=decision,
            )
        assert _state(coordinator, "core") is WorkState.REVIEW_REQUIRED
        assert _state(coordinator, "ui") is WorkState.PLANNED

    host.review_and_checkpoint(
        host_task_id=task.task_id,
        binding=binding,
        coordinator=coordinator,
        component_id="core",
        decision=ReviewDecision(
            reviewer_id=REVIEWER,
            accepted=True,
            reason="exact candidate independently accepted",
            evidence_refs=TRUSTED_EVIDENCE,
        ),
    )
    assert _state(coordinator, "core") is WorkState.ACCEPTED
    assert _state(coordinator, "ui") is WorkState.READY

    restarted_store = SQLiteStore(store.path)
    restarted_store.initialize()
    restarted_project = ProductProjectRepository(restarted_store).get(PROJECT_ID)
    restarted_authority = _ExactEvidenceAuthority()
    restarted_host = ProductFactoryProgramHost(
        restarted_store,
        _NoWorker(),
        review_evidence_authority=restarted_authority,
    )
    restarted_binding = _binding(restarted_project, plan, restarted_authority)
    restored = restarted_host.restore_latest(
        host_task_id=task.task_id,
        binding=restarted_binding,
    )

    assert _state(restored, "core") is WorkState.ACCEPTED
    assert _state(restored, "ui") is WorkState.READY
    assert {request.component_id for request in restored.ready_requests()} == {"ui"}
    assert projects.get(PROJECT_ID).project_id == PROJECT_ID
