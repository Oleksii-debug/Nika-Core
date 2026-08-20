from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_factory_coordinator import (
    ComponentWorkRequest,
    CoordinatorSnapshot,
    ProductFactoryCoordinator,
    ReviewDecision,
    WorkerResultEnvelope,
    WorkRecord,
    WorkState,
)
from nika_core.product_factory_project_binding import (
    ProductProjectBindingError,
    ProductProjectCoordinatorBinding,
    ProductProjectCoordinatorCheckpoint,
    StaleProductProjectBindingError,
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

_CHECKPOINT_SCHEMA = "nika-product-factory-checkpoint-v1"
_CHECKPOINT_STAGE = "product_factory.coordinator.v1"
_HOST_KIND = "product_factory"


class ProductFactoryCheckpointError(ValueError):
    """Raised when PF2 host checkpoint invariants are violated."""


class ProductFactoryCheckpointIntegrityError(ProductFactoryCheckpointError):
    """Raised when persisted PF2 checkpoint bytes cannot be trusted."""


class ProductFactoryRecoveryDisposition(StrEnum):
    RESUMABLE = "resumable"
    MISSING = "missing"
    STALE_PROJECT = "stale_project"
    INVALID_HOST_TASK = "invalid_host_task"
    CORRUPT = "corrupt"


@dataclass(frozen=True, slots=True)
class PersistedProductFactoryCheckpoint:
    checkpoint_id: str
    host_task_id: str
    checkpoint: ProductProjectCoordinatorCheckpoint
    checksum_sha256: str
    created_at: str


@dataclass(frozen=True, slots=True)
class ProductFactoryRecoveryCandidate:
    host_task_id: str
    project_id: str
    checkpoint_id: str | None
    disposition: ProductFactoryRecoveryDisposition
    reason: str


class ProductFactoryCheckpointHost:
    """Durable PF2 checkpoint adapter over Nika's canonical SQLite checkpoint table."""

    def __init__(self, store: SQLiteStore) -> None:
        self._store = store

    def save(
        self,
        *,
        host_task_id: str,
        checkpoint: ProductProjectCoordinatorCheckpoint,
    ) -> PersistedProductFactoryCheckpoint:
        payload = _encode_checkpoint(checkpoint)
        canonical = _canonical(payload)
        checksum = _sha256(canonical)
        checkpoint_id = _checkpoint_id(host_task_id, checkpoint, checksum)
        now = datetime.now(UTC).isoformat()
        with self._store.connection() as conn:
            self._require_host_task(
                conn,
                host_task_id=host_task_id,
                project_id=checkpoint.project_id,
            )
            previous = conn.execute(
                """
                SELECT checkpoint_id, task_id, payload_json, checksum_sha256, created_at
                FROM checkpoints
                WHERE task_id = ? AND stage = ?
                ORDER BY created_at DESC, checkpoint_id DESC
                LIMIT 1
                """,
                (host_task_id, _CHECKPOINT_STAGE),
            ).fetchone()
            previous_record = None
            if previous is not None:
                previous_record = self._row_to_record(previous)
                if (
                    previous_record.checkpoint.coordinator.revision
                    > checkpoint.coordinator.revision
                ):
                    raise ProductFactoryCheckpointError(
                        "checkpoint revision regressed; explicit reconciliation required"
                    )
                if (
                    previous_record.checkpoint.coordinator.revision
                    == checkpoint.coordinator.revision
                ):
                    if previous_record.checksum_sha256 != checksum:
                        raise ProductFactoryCheckpointError(
                            "same coordinator revision has different durable state"
                        )
                    return previous_record
            try:
                conn.execute(
                    """
                    INSERT INTO checkpoints(
                        checkpoint_id, task_id, stage, payload_json, checksum_sha256, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        checkpoint_id,
                        host_task_id,
                        _CHECKPOINT_STAGE,
                        canonical,
                        checksum,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ProductFactoryCheckpointError(
                    "checkpoint identity already exists with incompatible host state"
                ) from exc
            self._audit(
                conn,
                event_type="product_factory.checkpoint_saved",
                entity_id=checkpoint.project_id,
                payload={
                    "host_task_id": host_task_id,
                    "checkpoint_id": checkpoint_id,
                    "spec_version": checkpoint.spec_version,
                    "row_version": checkpoint.row_version,
                    "coordinator_revision": checkpoint.coordinator.revision,
                },
            )
        return PersistedProductFactoryCheckpoint(
            checkpoint_id=checkpoint_id,
            host_task_id=host_task_id,
            checkpoint=checkpoint,
            checksum_sha256=checksum,
            created_at=now,
        )

    def load(
        self,
        checkpoint_id: str,
        *,
        host_task_id: str | None = None,
    ) -> PersistedProductFactoryCheckpoint:
        with self._store.connection() as conn:
            row = conn.execute(
                """
                SELECT checkpoint_id, task_id, stage, payload_json, checksum_sha256, created_at
                FROM checkpoints
                WHERE checkpoint_id = ?
                """,
                (checkpoint_id,),
            ).fetchone()
            if row is None:
                raise KeyError(checkpoint_id)
            if row["stage"] != _CHECKPOINT_STAGE:
                raise ProductFactoryCheckpointIntegrityError(
                    "checkpoint stage is not Product Factory coordinator v1"
                )
            record = self._row_to_record(row)
            if host_task_id is not None and record.host_task_id != host_task_id:
                raise ProductFactoryCheckpointError("checkpoint does not belong to host task")
            self._require_host_task(
                conn,
                host_task_id=record.host_task_id,
                project_id=record.checkpoint.project_id,
            )
        return record

    def latest(
        self,
        *,
        host_task_id: str,
        project_id: str,
    ) -> PersistedProductFactoryCheckpoint | None:
        with self._store.connection() as conn:
            self._require_host_task(conn, host_task_id=host_task_id, project_id=project_id)
            row = conn.execute(
                """
                SELECT checkpoint_id, task_id, stage, payload_json, checksum_sha256, created_at
                FROM checkpoints
                WHERE task_id = ? AND stage = ?
                ORDER BY created_at DESC, checkpoint_id DESC
                LIMIT 1
                """,
                (host_task_id, _CHECKPOINT_STAGE),
            ).fetchone()
            if row is None:
                return None
            record = self._row_to_record(row)
            if record.checkpoint.project_id != project_id:
                raise ProductFactoryCheckpointIntegrityError(
                    "durable checkpoint project does not match host task project"
                )
        return record

    def inspect_latest(
        self,
        *,
        host_task_id: str,
        binding: ProductProjectCoordinatorBinding,
    ) -> ProductFactoryRecoveryCandidate:
        project_id = binding.project.project_id
        try:
            record = self.latest(host_task_id=host_task_id, project_id=project_id)
        except ProductFactoryCheckpointIntegrityError as exc:
            return ProductFactoryRecoveryCandidate(
                host_task_id,
                project_id,
                None,
                ProductFactoryRecoveryDisposition.CORRUPT,
                str(exc),
            )
        except ProductFactoryCheckpointError as exc:
            return ProductFactoryRecoveryCandidate(
                host_task_id,
                project_id,
                None,
                ProductFactoryRecoveryDisposition.INVALID_HOST_TASK,
                str(exc),
            )
        if record is None:
            return ProductFactoryRecoveryCandidate(
                host_task_id,
                project_id,
                None,
                ProductFactoryRecoveryDisposition.MISSING,
                "no durable Product Factory checkpoint exists for host task",
            )
        try:
            binding.restore(record.checkpoint)
        except StaleProductProjectBindingError as exc:
            return ProductFactoryRecoveryCandidate(
                host_task_id,
                project_id,
                record.checkpoint_id,
                ProductFactoryRecoveryDisposition.STALE_PROJECT,
                str(exc),
            )
        except ProductProjectBindingError as exc:
            return ProductFactoryRecoveryCandidate(
                host_task_id,
                project_id,
                record.checkpoint_id,
                ProductFactoryRecoveryDisposition.CORRUPT,
                str(exc),
            )
        return ProductFactoryRecoveryCandidate(
            host_task_id,
            project_id,
            record.checkpoint_id,
            ProductFactoryRecoveryDisposition.RESUMABLE,
            "durable checkpoint matches current ProductProject and repository graph",
        )

    def restore_latest(
        self,
        *,
        host_task_id: str,
        binding: ProductProjectCoordinatorBinding,
    ) -> ProductFactoryCoordinator:
        record = self.latest(
            host_task_id=host_task_id,
            project_id=binding.project.project_id,
        )
        if record is None:
            raise ProductFactoryCheckpointError(
                "no durable Product Factory checkpoint exists for host task"
            )
        return binding.restore(record.checkpoint)

    def clear(self, *, host_task_id: str, project_id: str) -> int:
        with self._store.connection() as conn:
            self._require_host_task(conn, host_task_id=host_task_id, project_id=project_id)
            cursor = conn.execute(
                "DELETE FROM checkpoints WHERE task_id = ? AND stage = ?",
                (host_task_id, _CHECKPOINT_STAGE),
            )
            count = int(cursor.rowcount)
            self._audit(
                conn,
                event_type="product_factory.checkpoints_cleared",
                entity_id=project_id,
                payload={"host_task_id": host_task_id, "deleted_count": count},
            )
        return count

    def _row_to_record(self, row: Any) -> PersistedProductFactoryCheckpoint:
        payload_json = str(row["payload_json"])
        checksum = str(row["checksum_sha256"])
        if _sha256(payload_json) != checksum:
            raise ProductFactoryCheckpointIntegrityError("checkpoint checksum mismatch")
        try:
            payload = json.loads(payload_json)
            checkpoint = _decode_checkpoint(payload)
        except (KeyError, TypeError, ValueError) as exc:
            raise ProductFactoryCheckpointIntegrityError(
                "checkpoint payload is not valid Product Factory checkpoint v1"
            ) from exc
        return PersistedProductFactoryCheckpoint(
            checkpoint_id=str(row["checkpoint_id"]),
            host_task_id=str(row["task_id"]),
            checkpoint=checkpoint,
            checksum_sha256=checksum,
            created_at=str(row["created_at"]),
        )

    @staticmethod
    def _require_host_task(conn: Any, *, host_task_id: str, project_id: str) -> None:
        row = conn.execute(
            "SELECT payload_json FROM tasks WHERE task_id = ?",
            (host_task_id,),
        ).fetchone()
        if row is None:
            raise ProductFactoryCheckpointError("Product Factory host task does not exist")
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, ValueError) as exc:
            raise ProductFactoryCheckpointError("Product Factory host task payload is invalid") from exc
        if not isinstance(payload, dict) or payload.get("kind") != _HOST_KIND:
            raise ProductFactoryCheckpointError(
                "host task is not explicitly typed as Product Factory orchestration"
            )
        if payload.get("product_project_id") != project_id:
            raise ProductFactoryCheckpointError(
                "host task ProductProject identity does not match checkpoint project"
            )

    @staticmethod
    def _audit(
        conn: Any,
        *,
        event_type: str,
        entity_id: str,
        payload: dict[str, object],
    ) -> None:
        conn.execute(
            """
            INSERT INTO audit_events(
                event_type, entity_type, entity_id, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                event_type,
                "product_project",
                entity_id,
                _canonical(payload),
                datetime.now(UTC).isoformat(),
            ),
        )


def _checkpoint_id(
    host_task_id: str,
    checkpoint: ProductProjectCoordinatorCheckpoint,
    checksum: str,
) -> str:
    identity = _canonical(
        {
            "host_task_id": host_task_id,
            "project_id": checkpoint.project_id,
            "spec_version": checkpoint.spec_version,
            "row_version": checkpoint.row_version,
            "revision": checkpoint.coordinator.revision,
            "checksum": checksum,
        }
    )
    return f"pf2-{_sha256(identity)[:32]}"


def _encode_checkpoint(checkpoint: ProductProjectCoordinatorCheckpoint) -> dict[str, object]:
    return {
        "schema": _CHECKPOINT_SCHEMA,
        "project_id": checkpoint.project_id,
        "spec_version": checkpoint.spec_version,
        "row_version": checkpoint.row_version,
        "coordinator": _encode_coordinator(checkpoint.coordinator),
    }


def _decode_checkpoint(payload: Any) -> ProductProjectCoordinatorCheckpoint:
    data = _dict(payload, "checkpoint")
    if data.get("schema") != _CHECKPOINT_SCHEMA:
        raise ValueError("unsupported Product Factory checkpoint schema")
    project_id = _text(data, "project_id")
    coordinator = _decode_coordinator(data["coordinator"])
    if coordinator.project_id != project_id:
        raise ValueError("checkpoint coordinator project identity mismatch")
    return ProductProjectCoordinatorCheckpoint(
        project_id=project_id,
        spec_version=_positive_int(data, "spec_version"),
        row_version=_nonnegative_int(data, "row_version"),
        coordinator=coordinator,
    )


def _encode_coordinator(snapshot: CoordinatorSnapshot) -> dict[str, object]:
    return {
        "project_id": snapshot.project_id,
        "revision": snapshot.revision,
        "records": [_encode_record(record) for record in snapshot.records],
    }


def _decode_coordinator(payload: Any) -> CoordinatorSnapshot:
    data = _dict(payload, "coordinator")
    records = data.get("records")
    if not isinstance(records, list):
        raise TypeError("coordinator records must be a list")
    return CoordinatorSnapshot(
        project_id=_text(data, "project_id"),
        revision=_nonnegative_int(data, "revision"),
        records=tuple(_decode_record(item) for item in records),
    )


def _encode_record(record: WorkRecord) -> dict[str, object]:
    return {
        "request": _encode_request(record.request),
        "state": record.state.value,
        "result": None if record.result is None else _encode_envelope(record.result),
        "review": None if record.review is None else _encode_review(record.review),
        "blocker": record.blocker,
    }


def _decode_record(payload: Any) -> WorkRecord:
    data = _dict(payload, "work record")
    blocker = data.get("blocker")
    if blocker is not None and not isinstance(blocker, str):
        raise ValueError("work blocker must be text or null")
    result = data.get("result")
    review = data.get("review")
    return WorkRecord(
        request=_decode_request(data["request"]),
        state=WorkState(_text(data, "state")),
        result=None if result is None else _decode_envelope(result),
        review=None if review is None else _decode_review(review),
        blocker=blocker,
    )


def _encode_request(request: ComponentWorkRequest) -> dict[str, object]:
    return {
        "work_id": request.work_id,
        "project_id": request.project_id,
        "component_id": request.component_id,
        "repository_id": request.repository_id,
        "goal": request.goal,
        "base_sha": request.base_sha,
        "allowed_paths": list(request.allowed_paths),
        "permission_ceiling": sorted(request.permission_ceiling),
        "acceptance_commands": [list(command) for command in request.acceptance_commands],
        "attempt": request.attempt,
    }


def _decode_request(payload: Any) -> ComponentWorkRequest:
    data = _dict(payload, "work request")
    return ComponentWorkRequest(
        work_id=_text(data, "work_id"),
        project_id=_text(data, "project_id"),
        component_id=_text(data, "component_id"),
        repository_id=_text(data, "repository_id"),
        goal=_text(data, "goal"),
        base_sha=_text(data, "base_sha"),
        allowed_paths=_text_tuple(data, "allowed_paths"),
        permission_ceiling=frozenset(_text_tuple(data, "permission_ceiling")),
        acceptance_commands=_argv_tuple(data, "acceptance_commands"),
        attempt=_positive_int(data, "attempt"),
    )


def _encode_envelope(envelope: WorkerResultEnvelope) -> dict[str, object]:
    return {
        "work_id": envelope.work_id,
        "component_id": envelope.component_id,
        "repository_id": envelope.repository_id,
        "base_sha": envelope.base_sha,
        "result_sha": envelope.result_sha,
        "diff_digest": envelope.diff_digest,
        "coding_result": _encode_coding_result(envelope.coding_result),
    }


def _decode_envelope(payload: Any) -> WorkerResultEnvelope:
    data = _dict(payload, "worker result envelope")
    return WorkerResultEnvelope(
        work_id=_text(data, "work_id"),
        component_id=_text(data, "component_id"),
        repository_id=_text(data, "repository_id"),
        base_sha=_text(data, "base_sha"),
        result_sha=_text(data, "result_sha"),
        diff_digest=_text(data, "diff_digest"),
        coding_result=_decode_coding_result(data["coding_result"]),
    )


def _encode_review(review: ReviewDecision) -> dict[str, object]:
    return {
        "reviewer_id": review.reviewer_id,
        "accepted": review.accepted,
        "reason": review.reason,
        "evidence_refs": list(review.evidence_refs),
    }


def _decode_review(payload: Any) -> ReviewDecision:
    data = _dict(payload, "review decision")
    accepted = data.get("accepted")
    if not isinstance(accepted, bool):
        raise TypeError("review accepted must be boolean")
    return ReviewDecision(
        reviewer_id=_text(data, "reviewer_id"),
        accepted=accepted,
        reason=_text(data, "reason"),
        evidence_refs=_text_tuple(data, "evidence_refs"),
    )


def _encode_coding_result(result: CodingResult) -> dict[str, object]:
    return {
        "job_id": result.job_id,
        "changed_files": [
            {"path": item.path, "sha256": item.sha256, "size_bytes": item.size_bytes}
            for item in result.changed_files
        ],
        "test_evidence": [
            {
                "command": list(item.command),
                "exit_code": item.exit_code,
                "output_digest": item.output_digest,
            }
            for item in result.test_evidence
        ],
        "artifacts": [
            {"name": item.name, "digest": item.digest, "media_type": item.media_type}
            for item in result.artifacts
        ],
        "recovery_state": (
            None
            if result.recovery_state is None
            else {
                "phase": result.recovery_state.phase,
                "opaque_token": result.recovery_state.opaque_token,
            }
        ),
        "failure": (
            None
            if result.failure is None
            else {
                "kind": result.failure.kind.value,
                "message": result.failure.message,
                "retryable": result.failure.retryable,
            }
        ),
    }


def _decode_coding_result(payload: Any) -> CodingResult:
    data = _dict(payload, "coding result")
    changed_files = data.get("changed_files")
    test_evidence = data.get("test_evidence")
    artifacts = data.get("artifacts")
    if not all(isinstance(value, list) for value in (changed_files, test_evidence, artifacts)):
        raise ValueError("coding result evidence collections must be lists")
    recovery = data.get("recovery_state")
    failure = data.get("failure")
    return CodingResult(
        job_id=_text(data, "job_id"),
        changed_files=tuple(_decode_changed_file(item) for item in changed_files),
        test_evidence=tuple(_decode_test_evidence(item) for item in test_evidence),
        artifacts=tuple(_decode_artifact(item) for item in artifacts),
        recovery_state=None if recovery is None else _decode_recovery(recovery),
        failure=None if failure is None else _decode_failure(failure),
    )


def _decode_changed_file(payload: Any) -> ChangedFile:
    data = _dict(payload, "changed file")
    return ChangedFile(
        path=_text(data, "path"),
        sha256=_text(data, "sha256"),
        size_bytes=_nonnegative_int(data, "size_bytes"),
    )


def _decode_test_evidence(payload: Any) -> TestEvidence:
    data = _dict(payload, "test evidence")
    command = data.get("command")
    if not isinstance(command, list) or not command or not all(
        isinstance(item, str) and item for item in command
    ):
        raise ValueError("test evidence command must be non-empty argv")
    exit_code = data.get("exit_code")
    if not isinstance(exit_code, int) or isinstance(exit_code, bool):
        raise TypeError("test evidence exit_code must be an integer")
    return TestEvidence(
        command=tuple(command),
        exit_code=exit_code,
        output_digest=_text(data, "output_digest"),
    )


def _decode_artifact(payload: Any) -> ArtifactEvidence:
    data = _dict(payload, "artifact evidence")
    return ArtifactEvidence(
        name=_text(data, "name"),
        digest=_text(data, "digest"),
        media_type=_text(data, "media_type"),
    )


def _decode_recovery(payload: Any) -> RecoveryState:
    data = _dict(payload, "recovery state")
    opaque = data.get("opaque_token")
    if opaque is not None and not isinstance(opaque, str):
        raise ValueError("recovery opaque token must be text or null")
    return RecoveryState(phase=_text(data, "phase"), opaque_token=opaque)


def _decode_failure(payload: Any) -> WorkerFailure:
    data = _dict(payload, "worker failure")
    retryable = data.get("retryable")
    if not isinstance(retryable, bool):
        raise TypeError("worker failure retryable must be boolean")
    return WorkerFailure(
        kind=WorkerFailureKind(_text(data, "kind")),
        message=_text(data, "message"),
        retryable=retryable,
    )


def _dict(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"{label} must be an object")
    return value


def _text(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be non-empty text")
    return value


def _text_tuple(data: dict[str, Any], key: str) -> tuple[str, ...]:
    value = data.get(key)
    if not isinstance(value, list) or not value or not all(
        isinstance(item, str) and item for item in value
    ):
        raise ValueError(f"{key} must be a non-empty text list")
    return tuple(value)


def _argv_tuple(data: dict[str, Any], key: str) -> tuple[tuple[str, ...], ...]:
    value = data.get(key)
    if not isinstance(value, list):
        raise TypeError(f"{key} must be an argv list")
    commands: list[tuple[str, ...]] = []
    for command in value:
        if not isinstance(command, list) or not command or not all(
            isinstance(item, str) and item for item in command
        ):
            raise ValueError(f"{key} contains invalid argv")
        commands.append(tuple(command))
    return tuple(commands)


def _positive_int(data: dict[str, Any], key: str) -> int:
    value = data.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"{key} must be a positive integer")
    return value


def _nonnegative_int(data: dict[str, Any], key: str) -> int:
    value = data.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{key} must be a non-negative integer")
    return value


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
