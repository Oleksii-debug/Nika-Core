from __future__ import annotations

from dataclasses import dataclass, replace

import pytest

from nika_core.product_factory_build_admission import (
    ReviewedBuildAdmissionError,
    ReviewedBuildExecutionPolicy,
    reviewed_candidate_fingerprint,
    reviewed_component_build_spec,
)
from nika_core.product_factory_build_execution import (
    ApprovedBuildCommand,
    BuildExecutionCoordinator,
    ProjectExecutionAuthority,
)
from nika_core.product_factory_coordinator import (
    ProductFactoryCoordinator,
    ReviewDecision,
    WorkRecord,
    WorkState,
    WorkerResultEnvelope,
)
from nika_core.product_factory_deployment import (
    ExecutionNodeRegistry,
    Platform,
    ResourceEnvelope,
)
from nika_core.product_factory_multi_repository import RepositoryGraphAuthority
from nika_core.product_factory_orchestration import (
    ProductComponent,
    ProductRepositoryGraph,
    RepositoryRef,
)
from nika_core.toolsmith.contracts import CodingResult, TestEvidence

BASE_SHA = "a" * 40
RESULT_SHA = "b" * 40
DIFF_DIGEST = "c" * 64
GRAPH_DIGEST = "d" * 64
PROJECT_ID = "product-reviewed-build"
COMPONENT_ID = "desktop"
REPOSITORY_ID = "repo-desktop"
PRODUCER = "worker:builder"
REVIEWER = "worker:independent-qa"
TEST_COMMAND = ("python", "-m", "pytest", "tests")


class AllowReviewAuthority:
    def verify(self, subject, evidence_refs) -> bool:
        return (
            subject.project_id == PROJECT_ID
            and subject.component_id == COMPONENT_ID
            and subject.producer_actor_id == PRODUCER
            and subject.reviewer_id == REVIEWER
            and subject.accepted is True
            and evidence_refs
            == ("review://independent/accepted",)
        )


class AlwaysAvailable:
    def is_available(self, node_id: str) -> bool:
        return True


@dataclass
class ExactPF5Authority:
    authority: ProjectExecutionAuthority

    def resolve(
        self,
        *,
        project_id: str,
        repository_id: str,
        work_id: str,
    ) -> ProjectExecutionAuthority:
        if (
            project_id != self.authority.project_id
            or repository_id != self.authority.repository_id
            or work_id != self.authority.work_id
        ):
            raise AssertionError("unexpected PF5 trusted-authority identity")
        return self.authority


@dataclass
class CapturingPolicies:
    source_override: str | None = None
    reviewer_fingerprint_override: str | None = None

    def resolve(
        self,
        *,
        project_id: str,
        repository_id: str,
        component_id: str,
        candidate_work_id: str,
        source_sha: str,
        review_fingerprint: str,
        spec_version: int,
        row_version: int,
        graph_digest: str,
    ) -> ReviewedBuildExecutionPolicy:
        return ReviewedBuildExecutionPolicy(
            project_id=project_id,
            repository_id=repository_id,
            component_id=component_id,
            candidate_work_id=candidate_work_id,
            source_sha=self.source_override or source_sha,
            review_fingerprint=(
                self.reviewer_fingerprint_override or review_fingerprint
            ),
            spec_version=spec_version,
            row_version=row_version,
            graph_digest=graph_digest,
            platform=Platform.WINDOWS,
            requested_node_ids=("windows-builder-1",),
            workspace_relpath="products/desktop",
            required_features=frozenset({"build"}),
            required_toolchains=frozenset({"python"}),
            resources=ResourceEnvelope(2, 4096, 8192),
            network_scopes=("pypi.org:443",),
            credential_refs=("credref:package-index",),
            command_id="build",
            lease_seconds=300,
        )


def _graph(*, dangerous_plan_command: bool = False) -> ProductRepositoryGraph:
    build_commands = (
        (("powershell.exe", "-Command", "Invoke-Expression $untrusted"),)
        if dangerous_plan_command
        else (("python", "-m", "build"),)
    )
    return ProductRepositoryGraph(
        project_id=PROJECT_ID,
        repositories=(
            RepositoryRef(
                repository_id=REPOSITORY_ID,
                provider="local-git",
                locator="C:/Nika/Product",
                default_branch="main",
                case_sensitive_paths=False,
            ),
        ),
        components=(
            ProductComponent(
                component_id=COMPONENT_ID,
                repository_id=REPOSITORY_ID,
                paths=("src", "tests"),
                build_commands=build_commands,
                test_commands=(TEST_COMMAND,),
                release_identity="desktop-release",
            ),
        ),
    )


def _authority(graph: ProductRepositoryGraph) -> RepositoryGraphAuthority:
    return RepositoryGraphAuthority(
        checkpoint_id="checkpoint:graph:1",
        project_id=PROJECT_ID,
        spec_version=3,
        row_version=7,
        graph_version=5,
        graph_digest=GRAPH_DIGEST,
        graph=graph,
        dependency_edges=(),
    )


def _coordinator(
    graph: ProductRepositoryGraph,
    *,
    permissions: frozenset[str] = frozenset(
        {"read_source", "write_source", "run_tests", "build_release"}
    ),
    review: bool = True,
) -> ProductFactoryCoordinator:
    coordinator = ProductFactoryCoordinator(
        graph,
        review_authority=AllowReviewAuthority(),
    )
    coordinator.plan(
        base_shas={REPOSITORY_ID: BASE_SHA},
        goals={COMPONENT_ID: "Build accessible Windows desktop product"},
        permission_ceiling=permissions,
    )
    request = coordinator.start(COMPONENT_ID)
    coordinator.record_result(
        WorkerResultEnvelope(
            work_id=request.work_id,
            component_id=COMPONENT_ID,
            repository_id=REPOSITORY_ID,
            base_sha=BASE_SHA,
            result_sha=RESULT_SHA,
            diff_digest=DIFF_DIGEST,
            coding_result=CodingResult(
                job_id=request.work_id,
                test_evidence=(
                    TestEvidence(
                        command=TEST_COMMAND,
                        exit_code=0,
                        output_digest="e" * 64,
                    ),
                ),
            ),
            producer_actor_id=PRODUCER,
        )
    )
    if review:
        coordinator.review(
            COMPONENT_ID,
            ReviewDecision(
                reviewer_id=REVIEWER,
                accepted=True,
                reason="independent acceptance passed",
                evidence_refs=("review://independent/accepted",),
            ),
        )
    return coordinator


def _spec(
    coordinator: ProductFactoryCoordinator,
    graph: ProductRepositoryGraph,
    policies=None,
):
    return reviewed_component_build_spec(
        authority=_authority(graph),
        coordinator=coordinator,
        component_id=COMPONENT_ID,
        policies=policies or CapturingPolicies(),
    )


def test_reviewed_candidate_becomes_bounded_pf5_spec_without_plan_argv_authority() -> None:
    graph = _graph(dangerous_plan_command=True)
    coordinator = _coordinator(graph)

    spec = _spec(coordinator, graph)

    record = coordinator.snapshot().records[0]
    assert spec.source_sha == RESULT_SHA
    assert spec.request.project_id == PROJECT_ID
    assert spec.request.work_id.startswith("pf5-build:")
    assert spec.request.platform is Platform.WINDOWS
    assert spec.scope.repository_id == REPOSITORY_ID
    assert spec.scope.command_id == "build"
    assert spec.scope.requested_node_ids == ("windows-builder-1",)
    assert spec.scope.credential_refs == ("credref:package-index",)
    assert spec.lease_seconds == 300
    assert not hasattr(spec, "argv")
    assert not hasattr(spec.scope, "argv")
    assert graph.components[0].build_commands[0][0] == "powershell.exe"
    assert reviewed_candidate_fingerprint(
        authority=_authority(graph),
        record=record,
        trusted_plan_fingerprint=coordinator.trusted_plan_fingerprint,
    ) in spec.request.work_id


def test_reviewed_spec_submits_to_pf5_using_only_host_approved_argv() -> None:
    graph = _graph(dangerous_plan_command=True)
    coordinator = _coordinator(graph)
    spec = _spec(coordinator, graph)
    authority = ProjectExecutionAuthority(
        project_id=PROJECT_ID,
        repository_id=REPOSITORY_ID,
        work_id=spec.request.work_id,
        permissions=frozenset({"build_release"}),
        allowed_node_ids=("windows-builder-1",),
        allowed_workspace_paths=("products/desktop",),
        network_scopes=("pypi.org:443",),
        credential_refs=("credref:package-index",),
        commands=(
            ApprovedBuildCommand(
                "build",
                ("python", "-m", "build", "--wheel"),
            ),
        ),
        evidence_refs=("authority://pf5/reviewed-build",),
    )
    pf5 = BuildExecutionCoordinator(
        nodes=ExecutionNodeRegistry(),
        node_availability=AlwaysAvailable(),
        trusted_authority=ExactPF5Authority(authority),
    )

    submitted = pf5.submit(spec)

    assert submitted.grant.argv == ("python", "-m", "build", "--wheel")
    assert submitted.grant.argv != graph.components[0].build_commands[0]
    assert submitted.grant.authority_evidence_refs == (
        "authority://pf5/reviewed-build",
    )


def test_restored_pf4_snapshot_preserves_exact_build_admission_identity() -> None:
    graph = _graph()
    original = _coordinator(graph)
    original_spec = _spec(original, graph)
    snapshot = original.snapshot()

    restored = ProductFactoryCoordinator(
        graph,
        review_authority=AllowReviewAuthority(),
    )
    restored.restore(
        snapshot,
        trusted_plan_fingerprint=original.trusted_plan_fingerprint,
    )
    restored_spec = _spec(restored, graph)

    assert restored_spec == original_spec


def test_accepted_repair_attempt_enters_pf5_with_new_exact_source() -> None:
    graph = _graph()
    coordinator = ProductFactoryCoordinator(
        graph,
        review_authority=AllowReviewAuthority(),
    )
    coordinator.plan(
        base_shas={REPOSITORY_ID: BASE_SHA},
        goals={COMPONENT_ID: "Build accessible Windows desktop product"},
        permission_ceiling=frozenset(
            {"read_source", "write_source", "run_tests", "build_release"}
        ),
    )
    first = coordinator.start(COMPONENT_ID)
    coordinator.record_result(
        WorkerResultEnvelope(
            work_id=first.work_id,
            component_id=COMPONENT_ID,
            repository_id=REPOSITORY_ID,
            base_sha=BASE_SHA,
            result_sha=RESULT_SHA,
            diff_digest=DIFF_DIGEST,
            coding_result=CodingResult(
                job_id=first.work_id,
                test_evidence=(
                    TestEvidence(
                        command=TEST_COMMAND,
                        exit_code=0,
                        output_digest="e" * 64,
                    ),
                ),
            ),
            producer_actor_id=PRODUCER,
        )
    )
    coordinator.review(
        COMPONENT_ID,
        ReviewDecision(
            reviewer_id=REVIEWER,
            accepted=False,
            reason="repair required",
            evidence_refs=("review://independent/rejected",),
        ),
    )
    repair = coordinator.prepare_repair(
        COMPONENT_ID,
        base_sha=RESULT_SHA,
        reason="address independent review",
    )
    second = coordinator.start(COMPONENT_ID)
    assert second == repair
    repaired_sha = "f" * 40
    coordinator.record_result(
        WorkerResultEnvelope(
            work_id=second.work_id,
            component_id=COMPONENT_ID,
            repository_id=REPOSITORY_ID,
            base_sha=RESULT_SHA,
            result_sha=repaired_sha,
            diff_digest="9" * 64,
            coding_result=CodingResult(
                job_id=second.work_id,
                test_evidence=(
                    TestEvidence(
                        command=TEST_COMMAND,
                        exit_code=0,
                        output_digest="8" * 64,
                    ),
                ),
            ),
            producer_actor_id=PRODUCER,
        )
    )
    coordinator.review(
        COMPONENT_ID,
        ReviewDecision(
            reviewer_id=REVIEWER,
            accepted=True,
            reason="repair independently accepted",
            evidence_refs=("review://independent/accepted",),
        ),
    )

    spec = _spec(coordinator, graph)

    assert spec.source_sha == repaired_sha
    assert coordinator.snapshot().records[0].request.attempt == 2
    assert spec.request.work_id.startswith("pf5-build:")


def test_review_required_candidate_cannot_enter_pf5() -> None:
    graph = _graph()
    coordinator = _coordinator(graph, review=False)

    with pytest.raises(ReviewedBuildAdmissionError, match="ACCEPTED"):
        _spec(coordinator, graph)


def test_accepted_candidate_without_build_release_permission_cannot_enter_pf5() -> None:
    graph = _graph()
    coordinator = _coordinator(
        graph,
        permissions=frozenset({"read_source", "write_source", "run_tests"}),
    )

    with pytest.raises(ReviewedBuildAdmissionError, match="build_release"):
        _spec(coordinator, graph)


def test_build_release_cannot_be_added_after_independent_review() -> None:
    graph = _graph()
    coordinator = _coordinator(
        graph,
        permissions=frozenset({"read_source", "write_source", "run_tests"}),
    )
    record = coordinator.snapshot().records[0]
    coordinator._records[COMPONENT_ID] = WorkRecord(
        request=replace(
            record.request,
            permission_ceiling=frozenset(
                {"read_source", "write_source", "run_tests", "build_release"}
            ),
        ),
        state=record.state,
        result=record.result,
        review=record.review,
        blocker=record.blocker,
    )

    with pytest.raises(ReviewedBuildAdmissionError, match="trusted plan"):
        _spec(coordinator, graph)


def test_host_policy_must_match_exact_reviewed_candidate_source() -> None:
    graph = _graph()
    coordinator = _coordinator(graph)

    with pytest.raises(ReviewedBuildAdmissionError, match="exact accepted candidate"):
        _spec(
            coordinator,
            graph,
            CapturingPolicies(source_override="f" * 40),
        )


def test_host_policy_must_match_exact_review_fingerprint() -> None:
    graph = _graph()
    coordinator = _coordinator(graph)

    with pytest.raises(ReviewedBuildAdmissionError, match="exact accepted candidate"):
        _spec(
            coordinator,
            graph,
            CapturingPolicies(reviewer_fingerprint_override="f" * 64),
        )


def test_self_review_corruption_fails_closed_before_pf5_policy_resolution() -> None:
    graph = _graph()
    coordinator = _coordinator(graph)
    record = coordinator.snapshot().records[0]
    assert record.result is not None
    assert record.review is not None
    coordinator._records[COMPONENT_ID] = WorkRecord(
        request=record.request,
        state=WorkState.ACCEPTED,
        result=record.result,
        review=replace(record.review, reviewer_id=PRODUCER),
    )

    with pytest.raises(ReviewedBuildAdmissionError, match="must differ"):
        _spec(coordinator, graph)


def test_result_identity_corruption_fails_closed_before_pf5() -> None:
    graph = _graph()
    coordinator = _coordinator(graph)
    record = coordinator.snapshot().records[0]
    assert record.result is not None
    coordinator._records[COMPONENT_ID] = WorkRecord(
        request=record.request,
        state=WorkState.ACCEPTED,
        result=replace(record.result, repository_id="repo-other"),
        review=record.review,
    )

    with pytest.raises(ReviewedBuildAdmissionError, match="result identity"):
        _spec(coordinator, graph)


def test_durable_graph_authority_project_mismatch_fails_closed() -> None:
    graph = _graph()
    coordinator = _coordinator(graph)
    bad = replace(_authority(graph), project_id="product-other")

    with pytest.raises(ReviewedBuildAdmissionError, match="authority project"):
        reviewed_component_build_spec(
            authority=bad,
            coordinator=coordinator,
            component_id=COMPONENT_ID,
            policies=CapturingPolicies(),
        )


def test_forged_review_evidence_is_reverified_and_rejected_before_pf5() -> None:
    graph = _graph()
    coordinator = _coordinator(graph)
    record = coordinator.snapshot().records[0]
    assert record.review is not None
    coordinator._records[COMPONENT_ID] = WorkRecord(
        request=record.request,
        state=WorkState.ACCEPTED,
        result=record.result,
        review=replace(
            record.review,
            evidence_refs=("review://forged",),
        ),
    )

    with pytest.raises(ReviewedBuildAdmissionError, match="rejected build admission"):
        _spec(coordinator, graph)


def test_coordinator_graph_drift_is_rejected_before_pf5_policy_resolution() -> None:
    graph = _graph()
    coordinator = _coordinator(graph)
    drifted_graph = ProductRepositoryGraph(
        project_id=PROJECT_ID,
        repositories=graph.repositories,
        components=(
            replace(
                graph.components[0],
                release_identity="different-release",
            ),
        ),
    )
    coordinator.graph = drifted_graph

    with pytest.raises(ReviewedBuildAdmissionError, match="coordinator graph"):
        _spec(coordinator, graph)


def test_build_work_identity_is_bound_to_independent_review_evidence() -> None:
    graph = _graph()
    first = _coordinator(graph)
    first_record = first.snapshot().records[0]
    assert first_record.review is not None

    second = _coordinator(graph)
    second_record = second.snapshot().records[0]
    assert second_record.review is not None
    second._records[COMPONENT_ID] = WorkRecord(
        request=second_record.request,
        state=WorkState.ACCEPTED,
        result=second_record.result,
        review=replace(
            second_record.review,
            evidence_refs=("review://independent/accepted", "review://extra-proof"),
        ),
    )

    first_fingerprint = reviewed_candidate_fingerprint(
        authority=_authority(graph),
        record=first_record,
        trusted_plan_fingerprint=first.trusted_plan_fingerprint,
    )
    second_fingerprint = reviewed_candidate_fingerprint(
        authority=_authority(graph),
        record=second.snapshot().records[0],
        trusted_plan_fingerprint=second.trusted_plan_fingerprint,
    )

    assert first_fingerprint != second_fingerprint


@pytest.mark.parametrize(
    "field,value,message",
    [
        ("requested_node_ids", ["windows-builder-1"], "requested node ids"),
        ("required_features", {"build"}, "required features"),
        ("require_gpu", 1, "require_gpu"),
        ("lease_seconds", True, "lease duration"),
    ],
)
def test_reviewed_build_policy_rejects_noncanonical_scope_carriers(
    field: str,
    value: object,
    message: str,
) -> None:
    graph = _graph()
    coordinator = _coordinator(graph)
    record = coordinator.snapshot().records[0]
    fingerprint = reviewed_candidate_fingerprint(
        authority=_authority(graph),
        record=record,
        trusted_plan_fingerprint=coordinator.trusted_plan_fingerprint,
    )
    kwargs = {
        "project_id": PROJECT_ID,
        "repository_id": REPOSITORY_ID,
        "component_id": COMPONENT_ID,
        "candidate_work_id": record.request.work_id,
        "source_sha": RESULT_SHA,
        "review_fingerprint": fingerprint,
        "spec_version": 3,
        "row_version": 7,
        "graph_digest": GRAPH_DIGEST,
        "platform": Platform.WINDOWS,
        "requested_node_ids": ("windows-builder-1",),
        "workspace_relpath": "products/desktop",
        "required_features": frozenset({"build"}),
        "required_toolchains": frozenset({"python"}),
        "resources": ResourceEnvelope(2, 4096, 8192),
        "require_gpu": False,
        "lease_seconds": 300,
    }
    kwargs[field] = value

    with pytest.raises(ReviewedBuildAdmissionError, match=message):
        ReviewedBuildExecutionPolicy(**kwargs)
