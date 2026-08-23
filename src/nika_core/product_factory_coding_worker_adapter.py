from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from nika_core.product_factory_coordinator import (
    ComponentWorkRequest,
    CoordinatorError,
    ProductFactoryCoordinator,
    WorkerResultEnvelope,
    WorkRecord,
    WorkState,
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
    WorkerFailure,
    WorkerFailureKind,
    WorkspaceLease,
    normalize_relative_path,
)


class CodingWorkerAdapterError(ValueError):
    """Raised when Product Factory cannot safely map public coding-worker evidence."""


class ComponentWorkerDisposition(StrEnum):
    """Product Factory interpretation of one bounded worker attempt."""

    REVIEW_REQUIRED = "review_required"
    RETRYABLE_FAILURE = "retryable_failure"
    TERMINAL_FAILURE = "terminal_failure"
    CANCELLED = "cancelled"
    CANCEL_RECOVERY_UNAVAILABLE = "cancel_recovery_unavailable"


class RepositoryPathIdentity(StrEnum):
    """Trusted repository path case semantics for changed-file identity checks."""

    CASE_SENSITIVE = "case_sensitive"
    CASE_INSENSITIVE = "case_insensitive"


@dataclass(frozen=True, slots=True)
class CodingWorkerDispatchContext:
    """Trusted host-provided execution context for one bounded component job.

    ``path_identity`` is intentionally owned by the Product Factory host boundary rather
    than inferred from the machine running Nika. A remote or containerized worker may
    use different repository semantics from the coordinator host. ``None`` remains
    backward-compatible for results without case-variant ambiguity, but case-variant
    evidence fails closed until the host declares the authoritative semantic.
    """

    repository_tree_digest: str
    lease: WorkspaceLease
    process_policy: ProcessPolicy
    network_policy: NetworkPolicy
    resource_budget: ResourceBudget
    path_identity: RepositoryPathIdentity | None = None

    def __post_init__(self) -> None:
        if not self.repository_tree_digest.strip():
            raise CodingWorkerAdapterError("repository tree digest must not be empty")
        if self.path_identity is not None and not isinstance(
            self.path_identity, RepositoryPathIdentity
        ):
            raise CodingWorkerAdapterError("repository path identity semantics are invalid")


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


@dataclass(frozen=True, slots=True)
class ComponentWorkerOutcome:
    """Normalized Product Factory outcome without leaking worker-framework types."""

    component_id: str
    work_id: str
    disposition: ComponentWorkerDisposition
    record: WorkRecord
    failure: WorkerFailure | None = None
    recovery_state: RecoveryState | None = None


class CodingWorkerContextPort(Protocol):
    async def context_for(self, request: ComponentWorkRequest) -> CodingWorkerDispatchContext: ...


class CodingWorkerEvidencePort(Protocol):
    async def collect(
        self,
        request: ComponentWorkRequest,
        job: CodingJob,
        result: CodingResult,
    ) -> CodingWorkerExecutionEvidence: ...


_REPAIRABLE_FAILURE_KINDS = frozenset(
    {
        WorkerFailureKind.TIMEOUT,
        WorkerFailureKind.PROCESS_FAILED,
        WorkerFailureKind.INTERNAL_ERROR,
    }
)


@dataclass(slots=True)
class CodingWorkerComponentAdapter:
    """Thin Product Factory adapter over the stable public ``CodingWorkerPort``.

    The adapter does not create a worker runtime, workspace, sandbox, network policy or
    process policy. Those stay owned by the trusted host/worker boundary and arrive via
    ``CodingWorkerContextPort``. Product Factory only maps bounded component identity,
    scope and acceptance commands into ``CodingJob`` and maps exact post-run evidence
    back into ``WorkerResultEnvelope`` for coordinator reconciliation/review.
    """

    worker: CodingWorkerPort
    contexts: CodingWorkerContextPort
    evidence: CodingWorkerEvidencePort

    async def dispatch(self, request: ComponentWorkRequest) -> WorkerResultEnvelope:
        job, context = await self._job_for(request)
        result = await self.worker.execute(job)
        return await self._envelope(request, job, result, context=context)

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
        job, context = await self._job_for(request)
        result = await self.worker.recover(job, state)
        return await self._envelope(request, job, result, context=context)

    async def run_component(
        self,
        coordinator: ProductFactoryCoordinator,
        component_id: str,
    ) -> WorkRecord:
        """Compatibility surface returning the coordinator record for one component run."""

        return (await self.run_component_outcome(coordinator, component_id)).record

    async def run_component_outcome(
        self,
        coordinator: ProductFactoryCoordinator,
        component_id: str,
    ) -> ComponentWorkerOutcome:
        """Run exactly one ready component and normalize its typed worker disposition."""

        request = coordinator.start(component_id)
        envelope = await self.dispatch(request)
        record = coordinator.record_result(envelope)
        return _outcome_for_record(record)

    async def cancel_component(
        self,
        coordinator: ProductFactoryCoordinator,
        component_id: str,
    ) -> ComponentWorkerOutcome:
        """Cancel a running component and reconcile the worker's exact post-cancel evidence.

        Cancellation is not considered durable merely because ``cancel()`` returned. The
        worker must expose recovery state and return an exact result envelope through its
        public recovery contract. A race where work completed before cancellation is
        accepted as a normal successful result and still requires independent review.
        """

        record = _record_for_component(coordinator, component_id)
        if record.state is not WorkState.RUNNING:
            raise CoordinatorError(f"component {component_id} must be running before cancellation")

        await self.cancel(record.request.work_id)
        state = await self.inspect(record.request.work_id)
        if state is None:
            blocked = coordinator.block(
                component_id,
                "worker cancellation has no recoverable state; host reconciliation required",
            )
            return ComponentWorkerOutcome(
                component_id=component_id,
                work_id=record.request.work_id,
                disposition=ComponentWorkerDisposition.CANCEL_RECOVERY_UNAVAILABLE,
                record=blocked,
            )

        envelope = await self.recover(record.request, state)
        updated = coordinator.record_result(envelope)
        outcome = _outcome_for_record(updated)
        return ComponentWorkerOutcome(
            component_id=outcome.component_id,
            work_id=outcome.work_id,
            disposition=outcome.disposition,
            record=outcome.record,
            failure=outcome.failure,
            recovery_state=state,
        )

    def prepare_safe_repair(
        self,
        coordinator: ProductFactoryCoordinator,
        component_id: str,
        *,
        reason: str,
    ) -> ComponentWorkRequest:
        """Prepare a bounded retry from the exact failed result SHA, never a caller base.

        Policy/invalid-request/cancelled failures are not repair-loop candidates even if a
        worker incorrectly marks them retryable. A retryable process/timeout/internal
        failure may advance to a new attempt, whose base is bound to the exact result SHA
        already accepted by Product Factory evidence collection.
        """

        if not reason.strip():
            raise CodingWorkerAdapterError("repair reason must not be empty")
        record = _record_for_component(coordinator, component_id)
        if record.state is not WorkState.REPAIR_REQUIRED or record.result is None:
            raise CodingWorkerAdapterError("component is not awaiting a worker repair")
        failure = record.result.coding_result.failure
        if failure is None:
            raise CodingWorkerAdapterError("review rejection requires reviewer-directed repair")
        if not _failure_is_repairable(failure):
            raise CodingWorkerAdapterError(
                f"worker failure is not eligible for automatic repair: {failure.kind.value}"
            )
        return coordinator.prepare_repair(
            component_id,
            base_sha=record.result.result_sha,
            reason=reason,
        )

    async def _job_for(
        self,
        request: ComponentWorkRequest,
    ) -> tuple[CodingJob, CodingWorkerDispatchContext]:
        context = await self.contexts.context_for(request)
        try:
            commands = tuple(AcceptanceCommand(argv=argv) for argv in request.acceptance_commands)
            job = CodingJob(
                job_id=request.work_id,
                task_id=component_task_id(request),
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
            return job, context
        except ValueError as exc:
            raise CodingWorkerAdapterError(f"invalid coding job mapping: {exc}") from exc

    async def _envelope(
        self,
        request: ComponentWorkRequest,
        job: CodingJob,
        result: CodingResult,
        *,
        context: CodingWorkerDispatchContext,
    ) -> WorkerResultEnvelope:
        if result.job_id != request.work_id:
            raise CodingWorkerAdapterError(
                "coding result job id does not match active work request"
            )
        if len(result.changed_files) > job.resource_budget.max_changed_files:
            raise CodingWorkerAdapterError("coding result exceeded component changed-file budget")

        seen_exact: set[str] = set()
        seen_folded: set[str] = set()
        for changed_file in result.changed_files:
            if not job.allowed_paths.allows(changed_file.path):
                raise CodingWorkerAdapterError(
                    f"changed file is outside component allowed paths: {changed_file.path}"
                )
            canonical = _canonical_changed_path(changed_file.path)
            folded = canonical.casefold()
            if canonical in seen_exact:
                raise CodingWorkerAdapterError(
                    f"coding result repeats changed-file identity: {changed_file.path}"
                )
            if folded in seen_folded:
                if context.path_identity is None:
                    raise CodingWorkerAdapterError(
                        "repository path identity semantics must be declared for "
                        "case-variant changed files"
                    )
                if context.path_identity is RepositoryPathIdentity.CASE_INSENSITIVE:
                    raise CodingWorkerAdapterError(
                        f"coding result repeats changed-file identity: {changed_file.path}"
                    )
            seen_exact.add(canonical)
            seen_folded.add(folded)

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


def component_task_id(request: ComponentWorkRequest) -> str:
    """Stable original-task identity shared by Product Factory and Toolsmith."""

    return f"product:{request.project_id}:component:{request.component_id}"


def _record_for_component(
    coordinator: ProductFactoryCoordinator,
    component_id: str,
) -> WorkRecord:
    for record in coordinator.snapshot().records:
        if record.request.component_id == component_id:
            return record
    raise CoordinatorError(f"unknown component {component_id}")


def _outcome_for_record(record: WorkRecord) -> ComponentWorkerOutcome:
    if record.state is WorkState.REVIEW_REQUIRED:
        disposition = ComponentWorkerDisposition.REVIEW_REQUIRED
        failure = None
    elif record.state is WorkState.REPAIR_REQUIRED and record.result is not None:
        failure = record.result.coding_result.failure
        if failure is None:
            disposition = ComponentWorkerDisposition.TERMINAL_FAILURE
        elif failure.kind is WorkerFailureKind.CANCELLED:
            disposition = ComponentWorkerDisposition.CANCELLED
        elif _failure_is_repairable(failure):
            disposition = ComponentWorkerDisposition.RETRYABLE_FAILURE
        else:
            disposition = ComponentWorkerDisposition.TERMINAL_FAILURE
    else:
        raise CodingWorkerAdapterError(
            f"worker attempt produced unsupported coordinator state: {record.state.value}"
        )
    return ComponentWorkerOutcome(
        component_id=record.request.component_id,
        work_id=record.request.work_id,
        disposition=disposition,
        record=record,
        failure=failure,
        recovery_state=(record.result.coding_result.recovery_state if record.result else None),
    )


def _failure_is_repairable(failure: WorkerFailure) -> bool:
    return failure.retryable and failure.kind in _REPAIRABLE_FAILURE_KINDS


def _canonical_changed_path(value: str) -> str:
    try:
        return normalize_relative_path(value).as_posix()
    except ValueError as exc:
        raise CodingWorkerAdapterError("changed-file identity is not repository-relative") from exc


def _validate_sha(value: str, label: str) -> None:
    if len(value) != 40 or any(char not in "0123456789abcdef" for char in value.casefold()):
        raise CodingWorkerAdapterError(f"{label} must be a 40-character hexadecimal SHA")


def _validate_digest(value: str, label: str) -> None:
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value.casefold()):
        raise CodingWorkerAdapterError(f"{label} must be a 64-character hexadecimal digest")