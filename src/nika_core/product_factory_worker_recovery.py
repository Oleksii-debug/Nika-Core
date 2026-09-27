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
    ArtifactEvidence,
    ChangedFile,
    CodingResult,
    RecoveryState,
    TestEvidence,
    WorkerFailure,
    WorkerFailureKind,
)


class WorkerRecoveryDisposition(StrEnum):
    RECOVERED = "recovered"
    BLOCKED_MISSING_STATE = "blocked_missing_state"
    BLOCKED_INSPECTION_FAILED = "blocked_inspection_failed"
    BLOCKED_RECOVERY_FAILED = "blocked_recovery_failed"
    BLOCKED_INVALID_EVIDENCE = "blocked_invalid_evidence"
    SUPERSEDED = "superseded"


@dataclass(frozen=True, slots=True)
class WorkerRecoveryOutcome:
    component_id: str
    disposition: WorkerRecoveryDisposition
    record: WorkRecord
    recovery_state: RecoveryState | None


class ComponentRecoveryPort(Protocol):
    async def inspect(self, work_id: str) -> RecoveryState | None: ...

    async def recover(
        self,
        request: ComponentWorkRequest,
        state: RecoveryState,
    ) -> WorkerResultEnvelope: ...


@dataclass(slots=True)
class ProductFactoryWorkerRecovery:
    """Restart reconciliation for in-flight PF2 component work.

    Durable ProductProject persistence remains outside this service. The caller restores
    a coordinator snapshot, then this service reconciles only work that was already
    RUNNING by consulting the stable public coding-worker recovery boundary.
    """

    worker: ComponentRecoveryPort

    async def recover_running(
        self,
        coordinator: ProductFactoryCoordinator,
        component_id: str,
    ) -> WorkerRecoveryOutcome:
        record = _record_from_snapshot(coordinator, component_id)
        if record.state is not WorkState.RUNNING:
            raise CoordinatorError(
                f"component {component_id} must be running before worker recovery"
            )
        expected_work_id = record.request.work_id

        try:
            state = await self.worker.inspect(expected_work_id)
        except Exception:  # noqa: BLE001 - isolate one external worker boundary failure
            return _block_or_superseded(
                coordinator=coordinator,
                component_id=component_id,
                expected_work_id=expected_work_id,
                disposition=WorkerRecoveryDisposition.BLOCKED_INSPECTION_FAILED,
                reason="worker recovery inspection failed; host reconciliation required",
                recovery_state=None,
            )

        superseded = _superseded_outcome_if_needed(
            coordinator,
            component_id,
            expected_work_id,
            recovery_state=None,
        )
        if superseded is not None:
            return superseded

        if state is None:
            return _block_or_superseded(
                coordinator=coordinator,
                component_id=component_id,
                expected_work_id=expected_work_id,
                disposition=WorkerRecoveryDisposition.BLOCKED_MISSING_STATE,
                reason=(
                    "worker recovery state is unavailable after restart; "
                    "host reconciliation required"
                ),
                recovery_state=None,
            )

        observed_state = _canonical_recovery_state(state)
        if observed_state is None:
            return _block_or_superseded(
                coordinator=coordinator,
                component_id=component_id,
                expected_work_id=expected_work_id,
                disposition=WorkerRecoveryDisposition.BLOCKED_INVALID_EVIDENCE,
                reason="worker recovery evidence is invalid; host reconciliation required",
                recovery_state=None,
            )

        try:
            envelope = await self.worker.recover(
                record.request,
                RecoveryState(observed_state.phase, observed_state.opaque_token),
            )
        except Exception:  # noqa: BLE001 - isolate one external worker boundary failure
            return _block_or_superseded(
                coordinator=coordinator,
                component_id=component_id,
                expected_work_id=expected_work_id,
                disposition=WorkerRecoveryDisposition.BLOCKED_RECOVERY_FAILED,
                reason="worker recovery attempt failed; host reconciliation required",
                recovery_state=observed_state,
            )

        superseded = _superseded_outcome_if_needed(
            coordinator,
            component_id,
            expected_work_id,
            recovery_state=observed_state,
        )
        if superseded is not None:
            return superseded

        canonical_envelope = _canonical_recovery_envelope(envelope)
        if canonical_envelope is None or canonical_envelope.component_id != component_id:
            return _block_or_superseded(
                coordinator=coordinator,
                component_id=component_id,
                expected_work_id=expected_work_id,
                disposition=WorkerRecoveryDisposition.BLOCKED_INVALID_EVIDENCE,
                reason="worker recovery evidence is invalid; host reconciliation required",
                recovery_state=observed_state,
            )

        superseded = _superseded_outcome_if_needed(
            coordinator,
            component_id,
            expected_work_id,
            recovery_state=observed_state,
        )
        if superseded is not None:
            return superseded

        try:
            updated = coordinator.record_result(canonical_envelope)
        except (CoordinatorError, AttributeError, TypeError):
            return _block_or_superseded(
                coordinator=coordinator,
                component_id=component_id,
                expected_work_id=expected_work_id,
                disposition=WorkerRecoveryDisposition.BLOCKED_INVALID_EVIDENCE,
                reason="worker recovery evidence is invalid; host reconciliation required",
                recovery_state=observed_state,
            )

        return WorkerRecoveryOutcome(
            component_id=component_id,
            disposition=WorkerRecoveryDisposition.RECOVERED,
            record=updated,
            recovery_state=observed_state,
        )


def _superseded_outcome_if_needed(
    coordinator: ProductFactoryCoordinator,
    component_id: str,
    expected_work_id: str,
    *,
    recovery_state: RecoveryState | None,
) -> WorkerRecoveryOutcome | None:
    current = _record_from_snapshot(coordinator, component_id)
    if current.state is WorkState.RUNNING and current.request.work_id == expected_work_id:
        return None
    return WorkerRecoveryOutcome(
        component_id=component_id,
        disposition=WorkerRecoveryDisposition.SUPERSEDED,
        record=current,
        recovery_state=recovery_state,
    )


def _block_or_superseded(
    *,
    coordinator: ProductFactoryCoordinator,
    component_id: str,
    expected_work_id: str,
    disposition: WorkerRecoveryDisposition,
    reason: str,
    recovery_state: RecoveryState | None,
) -> WorkerRecoveryOutcome:
    superseded = _superseded_outcome_if_needed(
        coordinator,
        component_id,
        expected_work_id,
        recovery_state=recovery_state,
    )
    if superseded is not None:
        return superseded
    blocked = coordinator.block(component_id, reason)
    return WorkerRecoveryOutcome(
        component_id=component_id,
        disposition=disposition,
        record=blocked,
        recovery_state=recovery_state,
    )


def _canonical_recovery_state(value: object) -> RecoveryState | None:
    if not (
        type(value) is RecoveryState
        and type(value.phase) is str
        and (value.opaque_token is None or type(value.opaque_token) is str)
    ):
        return None
    try:
        return RecoveryState(value.phase, value.opaque_token)
    except (TypeError, ValueError):
        return None


def _canonical_changed_file(value: object) -> ChangedFile | None:
    if not (
        type(value) is ChangedFile
        and type(value.path) is str
        and type(value.sha256) is str
        and type(value.size_bytes) is int
    ):
        return None
    try:
        return ChangedFile(value.path, value.sha256, value.size_bytes)
    except (TypeError, ValueError):
        return None


def _canonical_test_evidence(value: object) -> TestEvidence | None:
    if not (
        type(value) is TestEvidence
        and type(value.command) is tuple
        and all(type(part) is str for part in value.command)
        and type(value.exit_code) is int
        and type(value.output_digest) is str
    ):
        return None
    try:
        return TestEvidence(value.command, value.exit_code, value.output_digest)
    except (TypeError, ValueError):
        return None


def _canonical_artifact_evidence(value: object) -> ArtifactEvidence | None:
    if not (
        type(value) is ArtifactEvidence
        and type(value.name) is str
        and type(value.digest) is str
        and type(value.media_type) is str
    ):
        return None
    try:
        return ArtifactEvidence(value.name, value.digest, value.media_type)
    except (TypeError, ValueError):
        return None


def _canonical_worker_failure(value: object) -> WorkerFailure | None:
    if not (
        type(value) is WorkerFailure
        and type(value.kind) is WorkerFailureKind
        and type(value.message) is str
        and type(value.retryable) is bool
    ):
        return None
    try:
        return WorkerFailure(value.kind, value.message, value.retryable)
    except (TypeError, ValueError):
        return None


def _canonical_recovery_envelope(value: object) -> WorkerResultEnvelope | None:
    if type(value) is not WorkerResultEnvelope:
        return None
    if not all(
        type(item) is str
        for item in (
            value.work_id,
            value.component_id,
            value.repository_id,
            value.base_sha,
            value.result_sha,
            value.diff_digest,
        )
    ):
        return None

    result = value.coding_result
    if type(result) is not CodingResult or type(result.job_id) is not str:
        return None
    if type(result.changed_files) is not tuple:
        return None
    if type(result.test_evidence) is not tuple:
        return None
    if type(result.artifacts) is not tuple:
        return None

    changed_files: list[ChangedFile] = []
    for item in result.changed_files:
        canonical = _canonical_changed_file(item)
        if canonical is None:
            return None
        changed_files.append(canonical)

    test_evidence: list[TestEvidence] = []
    for item in result.test_evidence:
        canonical = _canonical_test_evidence(item)
        if canonical is None:
            return None
        test_evidence.append(canonical)

    artifacts: list[ArtifactEvidence] = []
    for item in result.artifacts:
        canonical = _canonical_artifact_evidence(item)
        if canonical is None:
            return None
        artifacts.append(canonical)

    recovery_state = None
    if result.recovery_state is not None:
        recovery_state = _canonical_recovery_state(result.recovery_state)
        if recovery_state is None:
            return None

    failure = None
    if result.failure is not None:
        failure = _canonical_worker_failure(result.failure)
        if failure is None:
            return None

    canonical_result = CodingResult(
        job_id=result.job_id,
        changed_files=tuple(changed_files),
        test_evidence=tuple(test_evidence),
        artifacts=tuple(artifacts),
        recovery_state=recovery_state,
        failure=failure,
    )
    try:
        return WorkerResultEnvelope(
            work_id=value.work_id,
            component_id=value.component_id,
            repository_id=value.repository_id,
            base_sha=value.base_sha,
            result_sha=value.result_sha,
            diff_digest=value.diff_digest,
            coding_result=canonical_result,
        )
    except (TypeError, ValueError):
        return None


def _record_from_snapshot(
    coordinator: ProductFactoryCoordinator,
    component_id: str,
) -> WorkRecord:
    for record in coordinator.snapshot().records:
        if record.request.component_id == component_id:
            return record
    raise CoordinatorError(f"unknown component {component_id}")
