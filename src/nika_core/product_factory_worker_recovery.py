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

        try:
            state = await self.worker.inspect(record.request.work_id)
        except Exception:  # noqa: BLE001 - isolate one external worker boundary failure
            blocked = coordinator.block(
                component_id,
                "worker recovery inspection failed; host reconciliation required",
            )
            return WorkerRecoveryOutcome(
                component_id=component_id,
                disposition=WorkerRecoveryDisposition.BLOCKED_INSPECTION_FAILED,
                record=blocked,
                recovery_state=None,
            )
        if state is None:
            blocked = coordinator.block(
                component_id,
                "worker recovery state is unavailable after restart; host reconciliation required",
            )
            return WorkerRecoveryOutcome(
                component_id=component_id,
                disposition=WorkerRecoveryDisposition.BLOCKED_MISSING_STATE,
                record=blocked,
                recovery_state=None,
            )
        if not _valid_recovery_state_carrier(state):
            blocked = coordinator.block(
                component_id,
                "worker recovery evidence is invalid; host reconciliation required",
            )
            return WorkerRecoveryOutcome(
                component_id=component_id,
                disposition=WorkerRecoveryDisposition.BLOCKED_INVALID_EVIDENCE,
                record=blocked,
                recovery_state=None,
            )

        try:
            envelope = await self.worker.recover(record.request, state)
        except Exception:  # noqa: BLE001 - isolate one external worker boundary failure
            blocked = coordinator.block(
                component_id,
                "worker recovery attempt failed; host reconciliation required",
            )
            return WorkerRecoveryOutcome(
                component_id=component_id,
                disposition=WorkerRecoveryDisposition.BLOCKED_RECOVERY_FAILED,
                record=blocked,
                recovery_state=state,
            )
        if not _valid_recovery_envelope_carriers(envelope):
            blocked = coordinator.block(
                component_id,
                "worker recovery evidence is invalid; host reconciliation required",
            )
            return WorkerRecoveryOutcome(
                component_id=component_id,
                disposition=WorkerRecoveryDisposition.BLOCKED_INVALID_EVIDENCE,
                record=blocked,
                recovery_state=state,
            )
        if envelope.component_id != component_id:
            blocked = coordinator.block(
                component_id,
                "worker recovery evidence is invalid; host reconciliation required",
            )
            return WorkerRecoveryOutcome(
                component_id=component_id,
                disposition=WorkerRecoveryDisposition.BLOCKED_INVALID_EVIDENCE,
                record=blocked,
                recovery_state=state,
            )
        try:
            updated = coordinator.record_result(envelope)
        except (CoordinatorError, AttributeError, TypeError):
            blocked = coordinator.block(
                component_id,
                "worker recovery evidence is invalid; host reconciliation required",
            )
            return WorkerRecoveryOutcome(
                component_id=component_id,
                disposition=WorkerRecoveryDisposition.BLOCKED_INVALID_EVIDENCE,
                record=blocked,
                recovery_state=state,
            )
        return WorkerRecoveryOutcome(
            component_id=component_id,
            disposition=WorkerRecoveryDisposition.RECOVERED,
            record=updated,
            recovery_state=state,
        )


def _valid_recovery_state_carrier(value: object) -> bool:
    if not (
        type(value) is RecoveryState
        and type(value.phase) is str
        and (value.opaque_token is None or type(value.opaque_token) is str)
    ):
        return False
    try:
        RecoveryState(value.phase, value.opaque_token)
    except (TypeError, ValueError):
        return False
    return True


def _valid_changed_file_carrier(value: object) -> bool:
    if not (
        type(value) is ChangedFile
        and type(value.path) is str
        and type(value.sha256) is str
        and type(value.size_bytes) is int
    ):
        return False
    try:
        ChangedFile(value.path, value.sha256, value.size_bytes)
    except (TypeError, ValueError):
        return False
    return True


def _valid_test_evidence_carrier(value: object) -> bool:
    if not (
        type(value) is TestEvidence
        and type(value.command) is tuple
        and all(type(part) is str for part in value.command)
        and type(value.exit_code) is int
        and type(value.output_digest) is str
    ):
        return False
    try:
        TestEvidence(value.command, value.exit_code, value.output_digest)
    except (TypeError, ValueError):
        return False
    return True


def _valid_artifact_evidence_carrier(value: object) -> bool:
    if not (
        type(value) is ArtifactEvidence
        and type(value.name) is str
        and type(value.digest) is str
        and type(value.media_type) is str
    ):
        return False
    try:
        ArtifactEvidence(value.name, value.digest, value.media_type)
    except (TypeError, ValueError):
        return False
    return True


def _valid_worker_failure_carrier(value: object) -> bool:
    if not (
        type(value) is WorkerFailure
        and type(value.kind) is WorkerFailureKind
        and type(value.message) is str
        and type(value.retryable) is bool
    ):
        return False
    try:
        WorkerFailure(value.kind, value.message, value.retryable)
    except (TypeError, ValueError):
        return False
    return True


def _valid_recovery_envelope_carriers(value: object) -> bool:
    if type(value) is not WorkerResultEnvelope:
        return False
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
        return False
    try:
        WorkerResultEnvelope(
            work_id=value.work_id,
            component_id=value.component_id,
            repository_id=value.repository_id,
            base_sha=value.base_sha,
            result_sha=value.result_sha,
            diff_digest=value.diff_digest,
            coding_result=value.coding_result,
        )
    except (TypeError, ValueError):
        return False

    result = value.coding_result
    if type(result) is not CodingResult or type(result.job_id) is not str:
        return False
    if type(result.changed_files) is not tuple or not all(
        _valid_changed_file_carrier(item) for item in result.changed_files
    ):
        return False
    if type(result.test_evidence) is not tuple or not all(
        _valid_test_evidence_carrier(item) for item in result.test_evidence
    ):
        return False
    if type(result.artifacts) is not tuple or not all(
        _valid_artifact_evidence_carrier(item) for item in result.artifacts
    ):
        return False
    if result.recovery_state is not None and not _valid_recovery_state_carrier(
        result.recovery_state
    ):
        return False
    if result.failure is not None and not _valid_worker_failure_carrier(result.failure):
        return False
    return True


def _record_from_snapshot(
    coordinator: ProductFactoryCoordinator,
    component_id: str,
) -> WorkRecord:
    for record in coordinator.snapshot().records:
        if record.request.component_id == component_id:
            return record
    raise CoordinatorError(f"unknown component {component_id}")
