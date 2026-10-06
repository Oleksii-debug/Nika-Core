from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.product_factory_build_admission import (
    ReviewedBuildExecutionPolicy,
    reviewed_component_build_spec,
)
from nika_core.product_factory_build_execution import (
    ApprovedBuildCommand,
    ProjectExecutionAuthority,
)
from nika_core.product_factory_build_execution_host import BuildOutputPolicy
from nika_core.product_factory_coding_worker_adapter import RepositoryPathIdentity
from nika_core.product_factory_coordinator import (
    ProductFactoryCoordinator,
    ReviewDecision,
    WorkerResultEnvelope,
)
from nika_core.product_factory_deployment import (
    ExecutionNode,
    NodeCapabilities,
    NodeIdentity,
    Platform,
    ResourceEnvelope,
)
from nika_core.product_factory_multi_repository import RepositoryGraphAuthority
from nika_core.product_factory_orchestration import (
    ProductComponent,
    ProductRepositoryGraph,
    RepositoryRef,
)
from nika_core.product_factory_packaged_build_host import (
    build_packaged_local_durable_build_host,
)
from nika_core.product_factory_local_coding import ContainedLocalCodingPolicy
from nika_core.product_factory_packaged_local_startup import (
    PackagedLocalProductFactoryStartup,
)
from nika_core.toolsmith.contracts import (
    AllowedPathPolicy,
    CodingResult,
    ResourceBudget,
    TestEvidence,
)

PROJECT_ID = "product-build-loop-integration"
REPOSITORY_ID = "repo-main"
COMPONENT_ID = "desktop"
BASE_SHA = "a" * 40
RESULT_SHA = "b" * 40
DIFF_DIGEST = "c" * 64
GRAPH_DIGEST = "d" * 64
NODE_ID = "packaged-local-1"
TEST_COMMAND = ("python", "-m", "pytest", "tests")
PRODUCER = "worker:builder"
REVIEWER = "worker:independent-reviewer"


class ReviewAuthority:
    def verify(self, subject, evidence_refs) -> bool:
        return (
            subject.project_id == PROJECT_ID
            and subject.component_id == COMPONENT_ID
            and subject.producer_actor_id == PRODUCER
            and subject.reviewer_id == REVIEWER
            and subject.accepted is True
            and evidence_refs == ("review://independent/accepted",)
        )


@dataclass
class BuildAdmissionPolicies:
    platform: Platform

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
            source_sha=source_sha,
            review_fingerprint=review_fingerprint,
            spec_version=spec_version,
            row_version=row_version,
            graph_digest=graph_digest,
            platform=self.platform,
            requested_node_ids=(NODE_ID,),
            workspace_relpath="products/build",
            required_features=frozenset({"build"}),
            required_toolchains=frozenset({"python"}),
            resources=ResourceEnvelope(1, 1024, 2048),
            network_scopes=(),
            credential_refs=(),
            command_id="build",
            lease_seconds=120,
        )


@dataclass
class ExecutionAuthority:
    value: ProjectExecutionAuthority

    def resolve(self, *, project_id: str, repository_id: str, work_id: str):
        if (
            project_id != self.value.project_id
            or repository_id != self.value.repository_id
            or work_id != self.value.work_id
        ):
            raise AssertionError("unexpected PF5 execution-authority identity")
        return self.value


@dataclass
class OutputPolicies:
    value: BuildOutputPolicy

    def resolve(self, *, project_id: str, repository_id: str, work_id: str):
        if (
            project_id != self.value.project_id
            or repository_id != self.value.repository_id
            or work_id != self.value.work_id
        ):
            raise AssertionError("unexpected PF5 output-policy identity")
        return self.value


def _platform() -> Platform:
    return Platform.WINDOWS if os.name == "nt" else Platform.LINUX


def _path_identity() -> RepositoryPathIdentity:
    return (
        RepositoryPathIdentity.CASE_INSENSITIVE
        if os.name == "nt"
        else RepositoryPathIdentity.CASE_SENSITIVE
    )


def _graph() -> ProductRepositoryGraph:
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
                build_commands=(
                    (
                        "powershell.exe",
                        "-Command",
                        "Invoke-Expression $candidate_controlled_text",
                    ),
                ),
                test_commands=(TEST_COMMAND,),
                release_identity="desktop-release",
            ),
        ),
    )


def _accepted_coordinator(
    graph: ProductRepositoryGraph,
) -> ProductFactoryCoordinator:
    coordinator = ProductFactoryCoordinator(
        graph,
        review_authority=ReviewAuthority(),
    )
    coordinator.plan(
        base_shas={REPOSITORY_ID: BASE_SHA},
        goals={COMPONENT_ID: "Build the accessible Windows desktop product"},
        permission_ceiling=frozenset(
            {"read_source", "write_source", "run_tests", "build_release"}
        ),
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


def test_reviewed_candidate_reaches_durable_packaged_pf5_without_plan_argv(
    tmp_path: Path,
) -> None:
    graph = _graph()
    coordinator = _accepted_coordinator(graph)
    graph_authority = RepositoryGraphAuthority(
        checkpoint_id="checkpoint:graph:1",
        project_id=PROJECT_ID,
        spec_version=3,
        row_version=7,
        graph_version=5,
        graph_digest=GRAPH_DIGEST,
        graph=graph,
        dependency_edges=(),
    )
    spec = reviewed_component_build_spec(
        authority=graph_authority,
        coordinator=coordinator,
        component_id=COMPONENT_ID,
        policies=BuildAdmissionPolicies(_platform()),
    )

    python_executable = str(Path(sys.executable).resolve())
    execution_authority = ExecutionAuthority(
        ProjectExecutionAuthority(
            project_id=PROJECT_ID,
            repository_id=REPOSITORY_ID,
            work_id=spec.request.work_id,
            permissions=frozenset({"build_release"}),
            allowed_node_ids=(NODE_ID,),
            allowed_workspace_paths=("products/build",),
            network_scopes=(),
            credential_refs=(),
            commands=(
                ApprovedBuildCommand(
                    "build",
                    (python_executable, "-m", "build"),
                ),
            ),
            evidence_refs=("authority://packaged-build/integration",),
        )
    )
    output_policies = OutputPolicies(
        BuildOutputPolicy(
            project_id=PROJECT_ID,
            repository_id=REPOSITORY_ID,
            work_id=spec.request.work_id,
            allowed_paths=AllowedPathPolicy(("products/build",)),
            max_changed_files=8,
            path_identity=_path_identity(),
        )
    )

    workspace_parent = tmp_path / "PF5 workspaces"
    workspace_parent.mkdir()
    startup = PackagedLocalProductFactoryStartup(
        workspace_parent=workspace_parent.resolve(),
        policy=ContainedLocalCodingPolicy(
            allowed_executables=(python_executable,),
            resource_budget=ResourceBudget(
                timeout_seconds=30,
                max_output_bytes=100_000,
                max_changed_files=8,
            ),
            lease_seconds=120,
        ),
        git_executable=Path(sys.executable).resolve(),
    )
    node = ExecutionNode(
        NodeIdentity(
            NODE_ID,
            _platform(),
            "x86_64",
            "instance-packaged-local-1",
        ),
        NodeCapabilities(
            frozenset({"build"}),
            frozenset({"python"}),
            False,
        ),
        ResourceEnvelope(4, 8192, 32768),
    )

    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    task = TaskQueue(store).create(
        workspace_id="ws-product",
        agent_id="product-factory",
        payload={
            "kind": "product_factory",
            "product_project_id": PROJECT_ID,
        },
    )
    host = build_packaged_local_durable_build_host(
        store,
        host_task_id=task.task_id,
        project_id=PROJECT_ID,
        node=node,
        startup=startup,
        trusted_authority=execution_authority,
        output_policies=output_policies,
    )

    submitted = host.submit(spec)
    prepared = host.prepare(spec.request.work_id)

    dangerous_plan_argv = graph.components[0].build_commands[0]
    assert submitted.grant.argv == (python_executable, "-m", "build")
    assert submitted.grant.argv != dangerous_plan_argv
    assert prepared.grant.argv == submitted.grant.argv
    assert prepared.node_id == NODE_ID
    assert host.checkpoints.latest().snapshot.sequence == 2
