from __future__ import annotations

from collections.abc import Iterable

from nika_core.kernel.checkpoint import CheckpointService

from .classifier import classify_gap
from .contracts import (
    CandidateState,
    CapabilityGap,
    CapabilityManifestV1,
    CodingJob,
    CodingResult,
    CodingWorkerPort,
    GapDisposition,
    ReuseCandidate,
    WorkerFailure,
    WorkerFailureKind,
)
from .repository import ToolsmithRepository


class CapabilityEscalationService:
    """Durable orchestration kernel for reuse/build/verify/register/resume.

    This service owns policy/state only. It does not treat a worktree as a sandbox, does not
    authorize production GitHub writes, and does not trust coding-worker self-report as
    verification evidence.
    """

    def __init__(
        self,
        *,
        repository: ToolsmithRepository,
        checkpoints: CheckpointService,
        worker: CodingWorkerPort,
    ) -> None:
        self._repository = repository
        self._checkpoints = checkpoints
        self._worker = worker

    def ensure_host_task(
        self,
        *,
        task_id: str,
        workspace_id: str,
        agent_id: str,
        payload: dict[str, object],
    ) -> None:
        """Register a trusted host-derived child task before Toolsmith references it."""

        self._repository.ensure_host_task(
            task_id=task_id,
            workspace_id=workspace_id,
            agent_id=agent_id,
            payload=payload,
        )

    def begin(self, gap: CapabilityGap) -> tuple[int, CandidateState]:
        version, state = self._repository.create_escalation(gap)
        if state is not CandidateState.PROPOSED:
            return version, state
        decision = classify_gap(gap)
        if decision.disposition is GapDisposition.BLOCK:
            self._checkpoint_block(gap, decision.reason)
            version = self._repository.transition(
                task_id=gap.task_id,
                capability_id=gap.requested_capability,
                expected_version=version,
                target=CandidateState.BLOCKED,
                evidence={"reason": decision.reason},
            )
            return version, CandidateState.BLOCKED
        return version, state

    def choose_reuse(
        self,
        *,
        gap: CapabilityGap,
        candidates: Iterable[ReuseCandidate],
        expected_version: int,
    ) -> tuple[int, ReuseCandidate | None]:
        ordered = tuple(candidates)
        for candidate in ordered:
            self._repository.record_search_candidate(task_id=gap.task_id, candidate=candidate)
        compatible = [
            candidate
            for candidate in ordered
            if candidate.capability_id == gap.requested_capability
            and candidate.permissions.issubset(gap.permission_ceiling)
        ]
        if compatible:
            selected = min(
                compatible,
                key=lambda item: (item.source, item.version, item.digest),
            )
            version = self._repository.transition(
                task_id=gap.task_id,
                capability_id=gap.requested_capability,
                expected_version=expected_version,
                target=CandidateState.REUSE_SELECTED,
                evidence={
                    "source": selected.source,
                    "version": selected.version,
                    "digest": selected.digest,
                },
            )
            return version, selected
        version = self._repository.transition(
            task_id=gap.task_id,
            capability_id=gap.requested_capability,
            expected_version=expected_version,
            target=CandidateState.BUILD_REQUIRED,
            evidence={"candidate_count": len(ordered)},
        )
        return version, None

    async def build(
        self,
        *,
        gap: CapabilityGap,
        job: CodingJob,
        expected_version: int,
    ) -> tuple[int, CodingResult]:
        self._validate_job(gap, job)
        version = self._repository.transition(
            task_id=gap.task_id,
            capability_id=gap.requested_capability,
            expected_version=expected_version,
            target=CandidateState.BUILDING,
            evidence={"job_id": job.job_id, "isolation_class": job.lease.isolation_class.value},
        )
        prior = await self._worker.inspect(job.job_id)
        result = (
            await self._worker.recover(job, prior)
            if prior is not None
            else await self._worker.execute(job)
        )
        return self._finish_build(gap=gap, job=job, row_version=version, result=result)

    async def recover_build(
        self,
        *,
        gap: CapabilityGap,
        job: CodingJob,
        expected_version: int,
    ) -> tuple[int, CodingResult]:
        """Resume an already-persisted BUILDING job without replaying the BUILDING transition."""

        self._validate_job(gap, job)
        row = self._repository.get_escalation(
            task_id=gap.task_id, capability_id=gap.requested_capability
        )
        if row is None:
            raise KeyError((gap.task_id, gap.requested_capability))
        if int(row["row_version"]) != expected_version:
            raise ValueError("recovery row version does not match durable escalation")
        if CandidateState(str(row["state"])) is not CandidateState.BUILDING:
            raise ValueError("recover_build requires durable BUILDING state")
        recovery = await self._worker.inspect(job.job_id)
        if recovery is None:
            message = "BUILDING state has no recoverable worker state"
            self._checkpoint_block(gap, message)
            version = self._repository.transition(
                task_id=gap.task_id,
                capability_id=gap.requested_capability,
                expected_version=expected_version,
                target=CandidateState.BLOCKED,
                evidence={"reason": "missing worker recovery state", "job_id": job.job_id},
            )
            return version, CodingResult(
                job_id=job.job_id,
                failure=WorkerFailure(
                    WorkerFailureKind.INTERNAL_ERROR,
                    message,
                    retryable=False,
                ),
            )
        result = await self._worker.recover(job, recovery)
        return self._finish_build(gap=gap, job=job, row_version=expected_version, result=result)

    def start_verification(self, *, gap: CapabilityGap, expected_version: int) -> int:
        return self._repository.transition(
            task_id=gap.task_id,
            capability_id=gap.requested_capability,
            expected_version=expected_version,
            target=CandidateState.VERIFYING,
            evidence={"verifier": "nika-independent"},
        )

    def accept_verification(
        self,
        *,
        gap: CapabilityGap,
        expected_version: int,
        candidate_digest: str,
        verifier_evidence: dict[str, object],
    ) -> int:
        return self._repository.accept_verification(
            task_id=gap.task_id,
            capability_id=gap.requested_capability,
            expected_version=expected_version,
            candidate_digest=candidate_digest,
            verifier_evidence=verifier_evidence,
        )

    def reject_verification(
        self,
        *,
        gap: CapabilityGap,
        expected_version: int,
        reason: str,
        quarantine: bool = False,
    ) -> int:
        if not reason.strip():
            raise ValueError("rejection reason must not be empty")
        target = CandidateState.QUARANTINED if quarantine else CandidateState.REJECTED
        return self._repository.transition(
            task_id=gap.task_id,
            capability_id=gap.requested_capability,
            expected_version=expected_version,
            target=target,
            evidence={"reason": reason},
        )

    def register(
        self,
        *,
        gap: CapabilityGap,
        expected_version: int,
        manifest: CapabilityManifestV1,
    ) -> int:
        if manifest.capability_id != gap.requested_capability:
            raise ValueError("manifest capability id must match escalation")
        if not manifest.permissions.issubset(gap.permission_ceiling):
            raise PermissionError("manifest permissions exceed original task ceiling")
        row = self._repository.get_escalation(
            task_id=gap.task_id,
            capability_id=gap.requested_capability,
        )
        if row is None:
            raise KeyError((gap.task_id, gap.requested_capability))
        if int(row["row_version"]) != expected_version:
            raise ValueError("registration row version does not match durable escalation")
        if CandidateState(str(row["state"])) is not CandidateState.VERIFIED:
            raise ValueError("registration requires durable VERIFIED state")
        verified_digest = row.get("pinned_digest")
        if verified_digest is not None and str(verified_digest) != manifest.digest:
            raise ValueError("manifest digest does not match independently verified candidate")
        version = self._repository.transition(
            task_id=gap.task_id,
            capability_id=gap.requested_capability,
            expected_version=expected_version,
            target=CandidateState.REGISTERING,
            evidence={"version": manifest.version, "digest": manifest.digest},
        )
        self._repository.register_exact(task_id=gap.task_id, manifest=manifest)
        version = self._repository.transition(
            task_id=gap.task_id,
            capability_id=gap.requested_capability,
            expected_version=version,
            target=CandidateState.REGISTERED,
            evidence={"version": manifest.version, "digest": manifest.digest},
        )
        self._repository.mark_resume_ready(task_id=gap.task_id, capability_id=gap.requested_capability)
        return version

    def reconcile_resume(self, *, task_id: str, capability_id: str) -> dict[str, str] | None:
        row = self._repository.get_escalation(task_id=task_id, capability_id=capability_id)
        if row is None or CandidateState(str(row["state"])) is not CandidateState.REGISTERED:
            return None
        version = row.get("pinned_version")
        digest = row.get("pinned_digest")
        if not version or not digest:
            raise RuntimeError("registered escalation lost exact pinned capability identity")
        self._repository.mark_resume_ready(task_id=task_id, capability_id=capability_id)
        return {
            "task_id": task_id,
            "capability_id": capability_id,
            "version": str(version),
            "digest": str(digest),
        }

    def _checkpoint_block(self, gap: CapabilityGap, reason: str) -> None:
        self._checkpoints.save(
            task_id=gap.task_id,
            stage="capability_escalation_blocked",
            payload={
                "task_id": gap.task_id,
                "capability_id": gap.requested_capability,
                "gap_kind": gap.kind.value,
                "reason": reason,
                "permission_ceiling": sorted(gap.permission_ceiling),
            },
        )

    @staticmethod
    def _validate_job(gap: CapabilityGap, job: CodingJob) -> None:
        if job.task_id != gap.task_id:
            raise ValueError("coding job must retain the original task id")
        if not job.permission_ceiling.issubset(gap.permission_ceiling):
            raise PermissionError("coding job permissions exceed original task ceiling")

    def _finish_build(
        self,
        *,
        gap: CapabilityGap,
        job: CodingJob,
        row_version: int,
        result: CodingResult,
    ) -> tuple[int, CodingResult]:
        self._validate_worker_result(job, result)
        if result.failure is not None:
            self._checkpoint_block(gap, result.failure.message)
            version = self._repository.transition(
                task_id=gap.task_id,
                capability_id=gap.requested_capability,
                expected_version=row_version,
                target=CandidateState.BLOCKED,
                evidence={
                    "worker_failure": result.failure.kind.value,
                    "message": result.failure.message,
                },
            )
            return version, result
        version = self._repository.transition(
            task_id=gap.task_id,
            capability_id=gap.requested_capability,
            expected_version=row_version,
            target=CandidateState.BUILT,
            evidence={"job_id": job.job_id, "changed_files": len(result.changed_files)},
        )
        return version, result

    @staticmethod
    def _validate_worker_result(job: CodingJob, result: CodingResult) -> None:
        if result.job_id != job.job_id:
            raise ValueError("coding worker result job id mismatch")
        if len(result.changed_files) > job.resource_budget.max_changed_files:
            raise ValueError("coding worker exceeded changed-file budget")
        for changed in result.changed_files:
            if not job.allowed_paths.allows(changed.path):
                raise ValueError(f"coding worker changed path outside allowed scope: {changed.path}")
        if result.failure is None and job.acceptance_commands and not result.test_evidence:
            raise ValueError("coding worker self-report lacks required test evidence")
        declared_commands = {command.argv for command in job.acceptance_commands}
        for evidence in result.test_evidence:
            if evidence.command not in declared_commands:
                raise ValueError("coding worker reported undeclared acceptance command")