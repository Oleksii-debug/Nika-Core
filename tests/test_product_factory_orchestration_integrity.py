from __future__ import annotations

from dataclasses import replace

import pytest

from nika_core.product_factory_coordinator import (
    CoordinatorError,
    CoordinatorSnapshot,
    ProductFactoryCoordinator,
    ReviewDecision,
    WorkerResultEnvelope,
    WorkRecord,
    WorkState,
)
from nika_core.product_factory_orchestration import (
    ComponentBrief,
    DynamicTeamComposer,
    IntegrationDecision,
    IntegrationDecisionKind,
    OwnershipLease,
    ProductComponent,
    ProductRepositoryGraph,
    ProjectScale,
    RepositoryGraphError,
    RepositoryRef,
    TeamCompositionError,
    TeamCompositionRequest,
)
from nika_core.toolsmith.contracts import CodingResult
from nika_core.toolsmith.contracts import TestEvidence as WorkerTestEvidence

SHA_A = "a" * 40
SHA_B = "b" * 40
DIGEST = "d" * 64
PERMISSIONS = frozenset({"read_source", "write_source", "run_tests"})


def _graph() -> ProductRepositoryGraph:
    return ProductRepositoryGraph(
        project_id="project-1",
        repositories=(RepositoryRef("repo-1", "github", "org/repo", "main"),),
        components=(
            ProductComponent(
                "core",
                "repo-1",
                ("src/core",),
                test_commands=(
                    ("python", "-m", "pytest", "tests/core"),
                    ("python", "-m", "pytest", "tests/integration"),
                ),
            ),
            ProductComponent(
                "ui",
                "repo-1",
                ("src/ui",),
                dependencies=("core",),
                test_commands=(("python", "-m", "pytest", "tests/ui"),),
            ),
        ),
    )


def _planned() -> ProductFactoryCoordinator:
    coordinator = ProductFactoryCoordinator(_graph())
    coordinator.plan(
        base_shas={"repo-1": SHA_A},
        goals={"core": "build core", "ui": "build ui"},
        permission_ceiling=PERMISSIONS,
    )
    return coordinator


def _success(request, commands=None) -> WorkerResultEnvelope:
    evidence_commands = request.acceptance_commands if commands is None else commands
    return WorkerResultEnvelope(
        work_id=request.work_id,
        component_id=request.component_id,
        repository_id=request.repository_id,
        base_sha=request.base_sha,
        result_sha=SHA_B,
        diff_digest=DIGEST,
        coding_result=CodingResult(
            job_id=request.work_id,
            test_evidence=tuple(
                WorkerTestEvidence(command, 0, f"ok-{index}")
                for index, command in enumerate(evidence_commands, start=1)
            ),
        ),
    )


def _records(snapshot: CoordinatorSnapshot) -> dict[str, WorkRecord]:
    return {record.request.component_id: record for record in snapshot.records}


def test_success_requires_every_declared_acceptance_command() -> None:
    coordinator = _planned()
    request = coordinator.start("core")

    with pytest.raises(CoordinatorError, match="missing declared acceptance evidence"):
        coordinator.record_result(_success(request, (request.acceptance_commands[0],)))


def test_unrelated_passing_command_cannot_replace_acceptance_matrix() -> None:
    coordinator = _planned()
    request = coordinator.start("core")

    with pytest.raises(CoordinatorError, match="missing declared acceptance evidence"):
        coordinator.record_result(_success(request, (("python", "-c", "print('green')"),)))


def test_restore_rejects_forged_accepted_record_without_result_or_review() -> None:
    coordinator = _planned()
    snapshot = coordinator.snapshot()
    records = _records(snapshot)
    forged = CoordinatorSnapshot(
        snapshot.project_id,
        snapshot.revision + 1,
        (WorkRecord(records["core"].request, WorkState.ACCEPTED), records["ui"]),
    )

    with pytest.raises(CoordinatorError, match="accepted snapshot record"):
        ProductFactoryCoordinator(_graph()).restore(forged)


def test_restore_rejects_nested_project_and_repository_identity_drift() -> None:
    coordinator = _planned()
    snapshot = coordinator.snapshot()
    records = _records(snapshot)
    core = records["core"]

    for mutated_request in (
        replace(core.request, project_id="other-project"),
        replace(core.request, repository_id="other-repo"),
    ):
        forged = CoordinatorSnapshot(
            snapshot.project_id,
            snapshot.revision + 1,
            (WorkRecord(mutated_request, core.state), records["ui"]),
        )
        with pytest.raises(CoordinatorError):
            ProductFactoryCoordinator(_graph()).restore(forged)


def test_restore_rejects_owned_path_or_permission_expansion() -> None:
    coordinator = _planned()
    snapshot = coordinator.snapshot()
    records = _records(snapshot)
    core = records["core"]
    expanded = replace(
        core.request,
        allowed_paths=("src",),
        permission_ceiling=core.request.permission_ceiling | {"admin_project"},
    )
    forged = CoordinatorSnapshot(
        snapshot.project_id,
        snapshot.revision + 1,
        (WorkRecord(expanded, core.state), records["ui"]),
    )

    with pytest.raises(CoordinatorError):
        ProductFactoryCoordinator(_graph()).restore(forged)


def test_legitimate_reviewed_snapshot_restores_and_unblocks_dependency() -> None:
    coordinator = _planned()
    request = coordinator.start("core")
    coordinator.record_result(_success(request))
    coordinator.review("core", ReviewDecision("qa", True, "accepted", ("ci:green",)))
    snapshot = coordinator.snapshot()

    restored = ProductFactoryCoordinator(_graph())
    restored.restore(snapshot)

    assert [request.component_id for request in restored.ready_requests()] == ["ui"]


def test_same_physical_repository_cannot_use_two_logical_ids() -> None:
    for second_locator in ("org/repo", "org/repo/"):
        with pytest.raises(RepositoryGraphError, match="aliased by multiple repository ids"):
            ProductRepositoryGraph(
                project_id="project-1",
                repositories=(
                    RepositoryRef("repo-a", "github", "org/repo", "main"),
                    RepositoryRef("repo-b", "github", second_locator, "main"),
                ),
                components=(
                    ProductComponent("api", "repo-a", ("src/api",)),
                    ProductComponent("worker", "repo-b", ("src/worker",)),
                ),
            )


def test_integration_decision_must_name_actual_conflicting_active_lease() -> None:
    graph = ProductRepositoryGraph(
        project_id="project-1",
        repositories=(RepositoryRef("repo", "github", "org/repo", "main"),),
        components=(ProductComponent("api", "repo", ("src/api",)),),
    )
    active = OwnershipLease("lease-active", "worker-a", ("api",), ("src/api",))
    candidate = OwnershipLease(
        "lease-candidate", "worker-b", ("api",), ("src/api/routes",)
    )
    decision = IntegrationDecision(
        "decision-1",
        IntegrationDecisionKind.RECONCILE,
        ("lease-candidate", "lease-unrelated"),
        "reconcile shared contract",
        ("compare:evidence",),
    )

    with pytest.raises(RepositoryGraphError, match="every conflicting active lease"):
        graph.assess_lease(candidate, (active,), decision=decision)


def test_dynamic_specialist_cannot_claim_unknown_component() -> None:
    composer = DynamicTeamComposer()
    plan = composer.compose(
        TeamCompositionRequest(
            project_id="project-1",
            components=(ComponentBrief("core", "backend"),),
            acceptance_criteria=("tested",),
            permission_ceiling=frozenset({"read_source", "run_tests"}),
            scale=ProjectScale.SMALL,
        )
    )

    with pytest.raises(TeamCompositionError, match="unknown component"):
        composer.add_specialist(
            plan,
            specialization="security",
            component_ids=("phantom",),
            requested_permissions=("read_source",),
            reason="security review",
        )


def test_repository_credential_ref_requires_nonempty_opaque_identity() -> None:
    for credential_ref in ("credref:", " credref:secret", "credref:   "):
        with pytest.raises(RepositoryGraphError):
            RepositoryRef(
                "repo",
                "github",
                "org/repo",
                "main",
                credential_ref=credential_ref,
            )


def test_component_identity_cannot_be_empty() -> None:
    with pytest.raises(RepositoryGraphError, match="component identity"):
        ProductComponent("", "repo", ("src/core",))


def test_duplicate_active_lease_identity_is_rejected() -> None:
    graph = ProductRepositoryGraph(
        project_id="project-1",
        repositories=(RepositoryRef("repo", "github", "org/repo", "main"),),
        components=(ProductComponent("api", "repo", ("src/api",)),),
    )
    candidate = OwnershipLease("candidate", "worker", ("api",), ("src/api/routes",))
    active_a = OwnershipLease("active", "worker-a", ("api",), ("src/api/a",))
    active_b = OwnershipLease("active", "worker-b", ("api",), ("src/api/b",))

    with pytest.raises(RepositoryGraphError, match="active lease ids must be unique"):
        graph.assess_lease(candidate, (active_a, active_b))
