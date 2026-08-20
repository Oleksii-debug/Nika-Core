from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from nika_core.product_factory_coordinator import (
    ComponentWorkRequest,
    ProductFactoryCoordinator,
    WorkerResultEnvelope,
    WorkRecord,
)
from nika_core.toolsmith.contracts import (
    AcceptanceCommand,
    AllowedPathPolicy,
    CodingJob,
    CodingResult,
    CodingWorkerPort,
    NetworkPolicy,
    ProcessPolicy,
    RecoveryState,
    RepositorySnapshot,
    ResourceBudget,
    WorkspaceLease,
)


class CodingWorkerAdapterError(ValueError):
    """Raised when Product Factory cannot safely map public coding-worker evidence."""


@dataclass(frozen=True, slots=True)
class CodingWorkerDispatchContext:
    """Trusted host-provided execution context for one bounded component job."""

    repository_tree_digest: str
    lease: WorkspaceLease
    process_policy: ProcessPolicy
    network_policy: NetworkPolicy
    resource_budget: ResourceBudget

    def __post_init__(self) -> None:
        if not self.repository_tree_digest.strip():
            raise CodingWorkerAdapterError("repository tree digest must not be empty")


@dataclass(frozen=True, slots=True)
class CodingWorkerExecutionEvidence:
    """Exact repository evidence captured after a worker execution or recovery attempt."""

    work_id: str
    repository_id: str
    base_sha: str
    result_sha: str
    diff_digest: str

    def __post_init__(self) -> None:
        if not self.work_id.strip() or not self.repository_id.strip():
            raise CodingWorkerAdapterError("worker evidence identity must not be empty")
        _validate_sha(self.base_sha, "base_sha")
        _validate_sha(self.result_sha, "result_sha")
        _validate_digest(self.diff_digest, "diff_digest")


class CodingWorkerContextPort(Protocol):
    async def context_for(self, request: ComponentWorkRequest) -> CodingWorkerDispatchContext: ...


class CodingWorkerEvidencePort(Protocol):
    async def collect(
        self,
        request: ComponentWorkRequest,
        job: CodingJob,
        result: CodingResult,
    ) -> CodingWorkerExecutionEvidence: ...


@dataclass(slots=True)
class CodingWorkerComponentAdapter:
    """Thin Product Factory adapter over the stable public ``CodingWorkerPort``.

    The adapter does not create a worker runtime, workspace, sandbox, network policy or
    process policy. Those stay owned by the trusted host/DEV02 boundary and arrive via
    ``CodingWorkerContextPort``. Product Factory only maps bounded component identity,
    scope and acceptance commands into ``CodingJob`` and maps exact post-run evidence
    back into ``WorkerResultEnvelope`` for coordinator reconciliation/review.
    """

    worker: CodingWorkerPort
    contexts: CodingWorkerContextPort
    evidence: CodingWorkerEvidencePort

    async def dispatch(self, request: ComponentWorkRequest) -> WorkerResultEnvelope:
        job = await self._job_for(request)
        result = await self.worker.execute(job)
        return await self._envelope(request, job, result)

    async def cancel(self, work_id: str) -> None:
        if not work_id.strip():
            raise CodingWorkerAdapterError("work_id must not be empty")
        await self.worker.cancel(work_id)

    async def inspect(self, work_id: str) -> RecoveryState | None:
        if not work_id.strip():
            raise CodingWorkerAdapterError("work_id must not be empty")
        return await self.worker.inspect(work_id)

    async def recover(
        self,
        request: ComponentWorkRequest,
        state: RecoveryState,
    ) -> WorkerResultEnvelope:
        job = await self._job_for(request)
        result = await self.worker.recover(job, state)
        return await self._envelope(request, job, result)

    async def run_component(
        self,
        coordinator: ProductFactoryCoordinator,
        component_id: str,
    ) -> WorkRecord:
        """Run one ready component and hand its evidence back to independent review state."""

        request = coordinator.start(component_id)
        envelope = await self.dispatch(request)
        return coordinator.record_result(envelope)

    async def _job_for(self, request: ComponentWorkRequest) -> CodingJob:
        context = await self.contexts.context_for(request)
        try:
            commands = tuple(AcceptanceCommand(argv=argv) for argv in request.acceptance_commands)
            return CodingJob(
                job_id=request.work_id,
                task_id=_component_task_id(request),
                goal=request.goal,
                repository=RepositorySnapshot(
                    repository_id=request.repository_id,
                    base_sha=request.base_sha,
                    tree_digest=context.repository_tree_digest,
                ),
                lease=context.lease,
                allowed_paths=AllowedPathPolicy(request.allowed_paths),
                process_policy=context.process_policy,
                network_policy=context.network_policy,
                resource_budget=context.resource_budget,
                acceptance_commands=commands,
                permission_ceiling=request.permission_ceiling,
            )
        except ValueError as exc:
            raise CodingWorkerAdapterError(f"invalid coding job mapping: {exc}") from exc

    async def _envelope(
        self,
        request: ComponentWorkRequest,
        job: CodingJob,
        result: CodingResult,
    ) -> WorkerResultEnvelope:
        if result.job_id != request.work_id:
            raise CodingWorkerAdapterError(
                "coding result job id does not match active work request"
            )
        for changed_file in result.changed_files:
            if not job.allowed_paths.allows(changed_file.path):
                raise CodingWorkerAdapterError(
                    f"changed file is outside component allowed paths: {changed_file.path}"
                )
        exact = await self.evidence.collect(request, job, result)
        if exact.work_id != request.work_id or exact.repository_id != request.repository_id:
            raise CodingWorkerAdapterError(
                "worker evidence identity does not match active work request"
            )
        if exact.base_sha != request.base_sha:
            raise CodingWorkerAdapterError(
                "stale worker evidence base SHA does not match active request"
            )
        return WorkerResultEnvelope(
            work_id=request.work_id,
            component_id=request.component_id,
            repository_id=request.repository_id,
            base_sha=exact.base_sha,
            result_sha=exact.result_sha,
            diff_digest=exact.diff_digest,
            coding_result=result,
        )


def _component_task_id(request: ComponentWorkRequest) -> str:
    return f"product:{request.project_id}:component:{request.component_id}"


def _validate_sha(value: str, label: str) -> None:
    if len(value) != 40 or any(char not in "0123456789abcdef" for char in value.casefold()):
        raise CodingWorkerAdapterError(f"{label} must be a 40-character hexadecimal SHA")


def _validate_digest(value: str, label: str) -> None:
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value.casefold()):
        raise CodingWorkerAdapterError(f"{label} must be a 64-character hexadecimal digest")
