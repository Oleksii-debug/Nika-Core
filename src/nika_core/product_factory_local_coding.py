from __future__ import annotations

import os
import pathlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Mapping

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_factory_coding_program import (
    build_product_factory_coding_program_host,
)
from nika_core.product_factory_coding_worker_adapter import (
    CodingWorkerDispatchContext,
    CodingWorkerExecutionEvidence,
    RepositoryPathIdentity,
)
from nika_core.product_factory_coordinator import ComponentWorkRequest
from nika_core.product_factory_multi_repository import MultiRepositoryProductFactoryHost
from nika_core.product_factory_orchestration import OwnershipLease, TeamPlan
from nika_core.product_factory_program_host import ProductFactoryProgramHost
from nika_core.product_factory_review_authority import (
    ProductFactoryReviewAuthorityPort,
    ReviewerPrincipalBindings,
)
from nika_core.runtime.idempotency import IdempotencyLedger
from nika_core.toolsmith.contracts import (
    IsolationClass,
    NetworkPolicy,
    ProcessPolicy,
    ResourceBudget,
    WorkspaceLease,
)
from nika_core.toolsmith.local_worker import (
    ContainedLocalCodingWorker,
    LocalCodingPlanPort,
)


@dataclass(frozen=True, slots=True)
class ContainedLocalCodingPolicy:
    """Trusted host policy for the local Product Factory coding backend."""

    allowed_executables: tuple[str, ...]
    resource_budget: ResourceBudget
    lease_seconds: int = 3600
    producer_actor_id: str = "contained-local-coding-worker"

    def __post_init__(self) -> None:
        if type(self.allowed_executables) is not tuple or not self.allowed_executables:
            raise ValueError("contained local coding requires pinned executables")
        canonical: list[str] = []
        for item in self.allowed_executables:
            if type(item) is not str or not item.strip():
                raise ValueError("contained local executable identities must be text")
            path = pathlib.Path(item)
            if not path.is_absolute():
                raise ValueError("contained local executables must be absolute paths")
            try:
                resolved = path.resolve(strict=True)
            except OSError as exc:
                raise ValueError("contained local executable does not exist") from exc
            if not resolved.is_file():
                raise ValueError("contained local executable must be a regular file")
            canonical.append(str(resolved))
        if len(set(value.casefold() for value in canonical)) != len(canonical):
            raise ValueError("contained local executable identities must be unique")
        if tuple(canonical) != self.allowed_executables:
            raise ValueError("contained local executable paths must already be canonical")
        if type(self.resource_budget) is not ResourceBudget:
            raise ValueError("contained local resource budget carrier is invalid")
        self.resource_budget.__post_init__()
        if type(self.lease_seconds) is not int or not 1 <= self.lease_seconds <= 3600:
            raise ValueError("contained local lease duration must be 1..3600 seconds")
        if (
            type(self.producer_actor_id) is not str
            or not self.producer_actor_id
            or self.producer_actor_id != self.producer_actor_id.strip()
            or len(self.producer_actor_id.encode("utf-8")) > 256
            or any(ord(character) < 32 or ord(character) == 127 for character in self.producer_actor_id)
        ):
            raise ValueError("contained local producer actor id must be canonical bounded text")


@dataclass(slots=True)
class ContainedLocalProductFactoryPorts:
    """Product Factory context/evidence ports backed by one local CodingWorker."""

    worker: ContainedLocalCodingWorker
    policy: ContainedLocalCodingPolicy

    async def context_for(
        self,
        request: ComponentWorkRequest,
    ) -> CodingWorkerDispatchContext:
        self.policy.__post_init__()
        tree_digest = self.worker.repository_tree_digest(
            request.repository_id,
            request.base_sha,
        )
        root = self.worker.ensure_workspace_root(request.work_id)
        expiry = datetime.now(UTC) + timedelta(seconds=self.policy.lease_seconds)
        return CodingWorkerDispatchContext(
            repository_tree_digest=tree_digest,
            ownership_lease=OwnershipLease(
                lease_id=f"contained-local-assignment:{request.work_id}",
                worker_id=self.policy.producer_actor_id,
                component_ids=(request.component_id,),
                allowed_paths=request.allowed_paths,
            ),
            lease=WorkspaceLease(
                lease_id=f"contained-local:{request.work_id}",
                workspace_root=root,
                isolation_class=(
                    IsolationClass.PROCESS_CONTAINED
                    if os.name == "nt"
                    else IsolationClass.POLICY_ONLY
                ),
                expires_at=expiry.isoformat(),
            ),
            process_policy=ProcessPolicy(self.policy.allowed_executables),
            network_policy=NetworkPolicy(),
            resource_budget=ResourceBudget(
                self.policy.resource_budget.timeout_seconds,
                self.policy.resource_budget.max_output_bytes,
                self.policy.resource_budget.max_changed_files,
            ),
            path_identity=(
                RepositoryPathIdentity.CASE_INSENSITIVE
                if os.name == "nt"
                else RepositoryPathIdentity.CASE_SENSITIVE
            ),
        )

    async def collect(
        self,
        request: ComponentWorkRequest,
        job,
        result,
    ) -> CodingWorkerExecutionEvidence:
        evidence = self.worker.execution_evidence(job.job_id)
        if result.job_id != job.job_id:
            raise ValueError(
                "local coding result identity changed before evidence collection"
            )
        return CodingWorkerExecutionEvidence(
            work_id=evidence.job_id,
            repository_id=evidence.repository_id,
            base_sha=evidence.base_sha,
            result_sha=evidence.result_sha,
            diff_digest=evidence.diff_digest,
        )


@dataclass(frozen=True, slots=True)
class ContainedLocalCodingProgram:
    """Production composition retaining access to worker candidate evidence."""

    host: ProductFactoryProgramHost
    multi_repository_host: MultiRepositoryProductFactoryHost
    worker: ContainedLocalCodingWorker
    ports: ContainedLocalProductFactoryPorts


def build_contained_local_coding_program(
    store: SQLiteStore,
    *,
    workspace_parent: pathlib.Path,
    repositories: Mapping[str, pathlib.Path],
    planner: LocalCodingPlanPort,
    policy: ContainedLocalCodingPolicy,
    idempotency: IdempotencyLedger | None = None,
    review_evidence_authority: ProductFactoryReviewAuthorityPort | None = None,
    team_plan: TeamPlan | None = None,
    reviewer_principals: ReviewerPrincipalBindings = (),
    git_executable: str = "git",
) -> ContainedLocalCodingProgram:
    """Compose Product Factory with the contained local CodingWorker.

    This wiring owns neither planning/model selection nor trusted review/GitHub
    publication. It only connects the canonical Product Factory program host to the
    canonical local mutation, containment, recovery and evidence boundary.
    """

    worker = ContainedLocalCodingWorker(
        workspace_parent=workspace_parent,
        repositories=repositories,
        planner=planner,
        git_executable=git_executable,
    )
    ports = ContainedLocalProductFactoryPorts(worker=worker, policy=policy)
    host = build_product_factory_coding_program_host(
        store,
        worker=worker,
        contexts=ports,
        evidence=ports,
        idempotency=idempotency,
        review_evidence_authority=review_evidence_authority,
    )
    multi_repository_host = MultiRepositoryProductFactoryHost(
        store=store,
        worker=host.worker,
        team_plan=team_plan,
        review_evidence_authority=review_evidence_authority,
        reviewer_principals=reviewer_principals,
        program_host=host,
    )
    return ContainedLocalCodingProgram(
        host=host,
        multi_repository_host=multi_repository_host,
        worker=worker,
        ports=ports,
    )