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
    CoordinatorError,
    CoordinatorSnapshot,
    ProductFactoryCoordinator,
    ReviewDecision,
    WorkerResultEnvelope,
    WorkRecord,
    WorkState,
    validate_trusted_plan_snapshot,
)
from nika_core.product_factory_coordinator import (
    trusted_plan_fingerprint as compute_trusted_plan_fingerprint,
)
from nika_core.product_factory_project_binding import (
    ProductProjectBindingError,
    ProductProjectCoordinatorBinding,
    ProductProjectCoordinatorCheckpoint,
    StaleProductProjectBindingError,
    verify_live_checkpoint_authority,
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
_CHECKPOINT_HEAD_SCHEMA = "nika-product-factory-checkpoint-head-v1"
_HOST_KIND = "product_factory"
_TRUSTED_PLAN_KEY = "trusted_plan_fingerprint"
_CHECKPOINT_HEAD_KEY = "product_factory_checkpoint_head"


class ProductFactoryCheckpointError(ValueError):
    """Raised when PF2 host checkpoint invariants are violated."""


class ProductFactoryCheckpointIntegrityError(ProductFactoryCheckpointError):
    """Raised when persisted PF2 checkpoint bytes cannot be trusted."""


class ProductFactoryTrustedPlanAuthorityError(ProductFactoryCheckpointError):
    """Raised when a durable PF2 checkpoint has no independent plan authority."""


class ProductFactoryRecoveryDisposition(StrEnum):
    RESUMABLE = "resumable"
    MISSING = "missing"
    STALE_PROJECT = "stale_project"
    INVALID_HOST_TASK = "invalid_host_task"
    MISSING_TRUSTED_PLAN = "missing_trusted_plan"
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
        candidate_authority = _checkpoint_trusted_plan_fingerprint(checkpoint)
        live_authority = _live_checkpoint_authority(checkpoint)
        now = datetime.now(UTC).isoformat()
        with self._store.connection() as conn:
            # The host task already owns independent trusted-plan authority. Commit the
            # exact admitted checkpoint head there as well so restart authority never
            # depends on candidate-created wall-clock ordering or public row hashes alone.
            conn.execute("BEGIN IMMEDIATE")
            host_payload = self._require_host_task(
                conn,
                host_task_id=host_task_id,
                project_id=checkpoint.project_id,
            )
            host_authority = _host_task_trusted_plan(host_payload, required=False)
            host_head = _host_task_checkpoint_head(host_payload, required=False)
            checkpoint_count = self._checkpoint_count(conn, host_task_id=host_task_id)

            previous_record = None
            if host_head is not None:
                if host_authority is None:
                    raise ProductFactoryTrustedPlanAuthorityError(
                        "Product Factory checkpoint head has no trusted plan authority"
                    )
                previous_record = self._validated_committed_head(
                    conn,
                    host_task_id=host_task_id,
                    project_id=checkpoint.project_id,
                    host_payload=host_payload,
                )
            elif checkpoint_count:
                raise ProductFactoryCheckpointIntegrityError(
                    "durable Product Factory checkpoints have no canonical host-task head; "
                    "explicit reconciliation is required"
                )
            elif host_authority is not None and live_authority is None:
                # Covers legacy clear/reset states that retained only the old plan anchor.
                # No candidate may silently turn that stale anchor into a new lineage.
                raise ProductFactoryTrustedPlanAuthorityError(
                    "Product Factory checkpoint lineage has no durable host head; "
                    "fresh live trusted plan authority proof is required"
                )

            if host_authority is None:
                if live_authority is None:
                    raise ProductFactoryTrustedPlanAuthorityError(
                        "first Product Factory checkpoint requires live trusted plan authority proof"
                    )
                if live_authority != candidate_authority:
                    raise ProductFactoryCheckpointIntegrityError(
                        "live trusted plan authority disagrees with coordinator checkpoint"
                    )
                host_payload = dict(host_payload)
                host_payload[_TRUSTED_PLAN_KEY] = live_authority
                host_authority = live_authority
                self._audit(
                    conn,
                    event_type="product_factory.trusted_plan_bound",
                    entity_id=checkpoint.project_id,
                    payload={
                        "host_task_id": host_task_id,
                        "trusted_plan_fingerprint": host_authority,
                    },
                )
            else:
                if live_authority is not None and live_authority != host_authority:
                    raise ProductFactoryTrustedPlanAuthorityError(
                        "live trusted plan authority disagrees with canonical host-task anchor"
                    )
                if candidate_authority != host_authority:
                    raise ProductFactoryCheckpointIntegrityError(
                        "coordinator checkpoint disagrees with canonical host-task plan authority"
                    )

            _validate_checkpoint_authority(checkpoint, host_authority)
            if previous_record is not None:
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
                _validate_checkpoint_transition(
                    previous_record.checkpoint,
                    checkpoint,
                    live_authority=live_authority,
                )

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

            host_payload = dict(host_payload)
            host_payload[_CHECKPOINT_HEAD_KEY] = _checkpoint_head_payload(
                checkpoint_id=checkpoint_id,
                checksum=checksum,
                revision=checkpoint.coordinator.revision,
            )
            conn.execute(
                "UPDATE tasks SET payload_json = ? WHERE task_id = ?",
                (_canonical(host_payload), host_task_id),
            )
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
            host_payload = self._require_host_task(
                conn,
                host_task_id=record.host_task_id,
                project_id=record.checkpoint.project_id,
            )
            authority = _host_task_trusted_plan(host_payload, required=True)
            _validate_checkpoint_authority(record.checkpoint, authority)
            if _host_task_checkpoint_head(host_payload, required=False) is None:
                raise ProductFactoryCheckpointIntegrityError(
                    "durable Product Factory checkpoint lineage has no canonical host-task head"
                )
            committed = self._validated_committed_head(
                conn,
                host_task_id=record.host_task_id,
                project_id=record.checkpoint.project_id,
                host_payload=host_payload,
            )
            if record.checkpoint_id != committed.checkpoint_id:
                raise ProductFactoryCheckpointIntegrityError(
                    "requested checkpoint is not the canonical committed host-task head"
                )
        return committed

    def latest(
        self,
        *,
        host_task_id: str,
        project_id: str,
    ) -> PersistedProductFactoryCheckpoint | None:
        with self._store.connection() as conn:
            host_payload = self._require_host_task(
                conn,
                host_task_id=host_task_id,
                project_id=project_id,
            )
            head = _host_task_checkpoint_head(host_payload, required=False)
            checkpoint_count = self._checkpoint_count(conn, host_task_id=host_task_id)
            if head is None:
                if checkpoint_count:
                    raise ProductFactoryCheckpointIntegrityError(
                        "durable Product Factory checkpoints have no canonical host-task head; "
                        "explicit reconciliation is required"
                    )
                return None
            return self._validated_committed_head(
                conn,
                host_task_id=host_task_id,
                project_id=project_id,
                host_payload=host_payload,
            )

    def inspect_latest(
        self,
        *,
        host_task_id: str,
        binding: ProductProjectCoordinatorBinding,
    ) -> ProductFactoryRecoveryCandidate:
        project_id = binding.project.project_id
        try:
            record = self.latest(host_task_id=host_task_id, project_id=project_id)
        except ProductFactoryTrustedPlanAuthorityError as exc:
            return ProductFactoryRecoveryCandidate(
                host_task_id,
                project_id,
                None,
                ProductFactoryRecoveryDisposition.MISSING_TRUSTED_PLAN,
                str(exc),
            )
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
            authority = self._read_host_trusted_plan(
                host_task_id=host_task_id,
                project_id=project_id,
            )
            binding.restore(
                record.checkpoint,
                trusted_plan_fingerprint=authority,
            )
        except ProductFactoryTrustedPlanAuthorityError as exc:
            return ProductFactoryRecoveryCandidate(
                host_task_id,
                project_id,
                record.checkpoint_id,
                ProductFactoryRecoveryDisposition.MISSING_TRUSTED_PLAN,
                str(exc),
            )
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
            "durable checkpoint matches current ProductProject, trusted plan and repository graph",
        )

    def restore_latest(
        self,
        *,
        host_task_id: str,
        binding: ProductProjectCoordinatorBinding,
    ) -> ProductFactoryCoordinator:
        project_id = binding.project.project_id
        record = self.latest(
            host_task_id=host_task_id,
            project_id=project_id,
        )
        if record is None:
            raise ProductFactoryCheckpointError(
                "no durable Product Factory checkpoint exists for host task"
            )
        authority = self._read_host_trusted_plan(
            host_task_id=host_task_id,
            project_id=project_id,
        )
        try:
            return binding.restore(
                record.checkpoint,
                trusted_plan_fingerprint=authority,
            )
        except StaleProductProjectBindingError:
            raise
        except ProductProjectBindingError as exc:
            raise ProductFactoryCheckpointError(
                "durable Product Factory checkpoint failed trusted restore validation"
            ) from exc

    def clear(self, *, host_task_id: str, project_id: str) -> int:
        with self._store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            host_payload = self._require_host_task(
                conn,
                host_task_id=host_task_id,
                project_id=project_id,
            )
            cursor = conn.execute(
                "DELETE FROM checkpoints WHERE task_id = ? AND stage = ?",
                (host_task_id, _CHECKPOINT_STAGE),
            )
            count = int(cursor.rowcount)
            reset_payload = dict(host_payload)
            trusted_plan_revoked = reset_payload.pop(_TRUSTED_PLAN_KEY, None) is not None
            checkpoint_head_revoked = reset_payload.pop(_CHECKPOINT_HEAD_KEY, None) is not None
            if reset_payload != host_payload:
                conn.execute(
                    "UPDATE tasks SET payload_json = ? WHERE task_id = ?",
                    (_canonical(reset_payload), host_task_id),
                )
            self._audit(
                conn,
                event_type="product_factory.checkpoints_cleared",
                entity_id=project_id,
                payload={
                    "host_task_id": host_task_id,
                    "deleted_count": count,
                    "trusted_plan_revoked": trusted_plan_revoked,
                    "checkpoint_head_revoked": checkpoint_head_revoked,
                },
            )
        return count

    def _validated_committed_head(
        self,
        conn: Any,
        *,
        host_task_id: str,
        project_id: str,
        host_payload: dict[str, Any],
    ) -> PersistedProductFactoryCheckpoint:
        authority = _host_task_trusted_plan(host_payload, required=True)
        head = _host_task_checkpoint_head(host_payload, required=True)
        assert head is not None
        head_checkpoint_id, head_checksum, head_revision = head
        rows = conn.execute(
            """
            SELECT checkpoint_id, task_id, payload_json, checksum_sha256, created_at
            FROM checkpoints
            WHERE task_id = ? AND stage = ?
            """,
            (host_task_id, _CHECKPOINT_STAGE),
        ).fetchall()
        if not rows:
            raise ProductFactoryCheckpointIntegrityError(
                "canonical host-task checkpoint head points to a missing durable lineage"
            )

        committed = None
        revisions: dict[int, tuple[str, str]] = {}
        for row in rows:
            record = self._row_to_record(row)
            if record.checkpoint.project_id != project_id:
                raise ProductFactoryCheckpointIntegrityError(
                    "durable checkpoint project does not match host task project"
                )
            _validate_checkpoint_authority(record.checkpoint, authority)
            revision = record.checkpoint.coordinator.revision
            identity = (record.checkpoint_id, record.checksum_sha256)
            prior = revisions.get(revision)
            if prior is not None and prior != identity:
                raise ProductFactoryCheckpointIntegrityError(
                    "durable checkpoint lineage contains conflicting coordinator revision"
                )
            revisions[revision] = identity
            if revision > head_revision:
                raise ProductFactoryCheckpointIntegrityError(
                    "durable checkpoint lineage contains an uncommitted successor beyond host authority"
                )
            if record.checkpoint_id == head_checkpoint_id:
                committed = record

        if committed is None:
            raise ProductFactoryCheckpointIntegrityError(
                "canonical host-task checkpoint head row is missing"
            )
        if committed.checksum_sha256 != head_checksum:
            raise ProductFactoryCheckpointIntegrityError(
                "canonical host-task checkpoint head checksum does not match durable row"
            )
        if committed.checkpoint.coordinator.revision != head_revision:
            raise ProductFactoryCheckpointIntegrityError(
                "canonical host-task checkpoint head revision does not match durable row"
            )
        return committed

    @staticmethod
    def _checkpoint_count(conn: Any, *, host_task_id: str) -> int:
        row = conn.execute(
            "SELECT COUNT(*) AS count FROM checkpoints WHERE task_id = ? AND stage = ?",
            (host_task_id, _CHECKPOINT_STAGE),
        ).fetchone()
        return int(row["count"])

    def _row_to_record(self, row: Any) -> PersistedProductFactoryCheckpoint:
        payload_json = row["payload_json"]
        checksum = row["checksum_sha256"]
        checkpoint_id = row["checkpoint_id"]
        host_task_id = row["task_id"]
        if not all(
            isinstance(value, str)
            for value in (payload_json, checksum, checkpoint_id, host_task_id)
        ):
            raise ProductFactoryCheckpointIntegrityError(
                "checkpoint durable identity and payload fields must be text"
            )
        if _sha256(payload_json) != checksum:
            raise ProductFactoryCheckpointIntegrityError("checkpoint checksum mismatch")
        try:
            payload = json.loads(payload_json)
            checkpoint = _decode_checkpoint(payload)
        except (KeyError, TypeError, ValueError) as exc:
            raise ProductFactoryCheckpointIntegrityError(
                "checkpoint payload is not valid Product Factory checkpoint v1"
            ) from exc
        canonical = _canonical(_encode_checkpoint(checkpoint))
        if payload_json != canonical:
            raise ProductFactoryCheckpointIntegrityError(
                "checkpoint payload is not canonical Product Factory checkpoint v1"
            )
        expected_checkpoint_id = _checkpoint_id(host_task_id, checkpoint, checksum)
        if checkpoint_id != expected_checkpoint_id:
            raise ProductFactoryCheckpointIntegrityError(
                "checkpoint identity does not match durable payload"
            )
        return PersistedProductFactoryCheckpoint(
            checkpoint_id=checkpoint_id,
            host_task_id=host_task_id,
            checkpoint=checkpoint,
            checksum_sha256=checksum,
            created_at=str(row["created_at"]),
        )

    def _read_host_trusted_plan(self, *, host_task_id: str, project_id: str) -> str:
        with self._store.connection() as conn:
            payload = self._require_host_task(
                conn,
                host_task_id=host_task_id,
                project_id=project_id,
            )
            return _host_task_trusted_plan(payload, required=True)

    @staticmethod
    def _require_host_task(
        conn: Any,
        *,
        host_task_id: str,
        project_id: str,
    ) -> dict[str, Any]:
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
        return payload

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


def _host_task_trusted_plan(payload: dict[str, Any], *, required: bool) -> str | None:
    value = payload.get(_TRUSTED_PLAN_KEY)
    if value is None:
        if required:
            raise ProductFactoryTrustedPlanAuthorityError(
                "Product Factory host task has no trusted plan authority; explicit reconciliation is required"
            )
        return None
    if not isinstance(value, str):
        raise ProductFactoryTrustedPlanAuthorityError(
            "Product Factory host task trusted plan authority is malformed"
        )
    _validate_fingerprint(value)
    return value


def _host_task_checkpoint_head(
    payload: dict[str, Any],
    *,
    required: bool,
) -> tuple[str, str, int] | None:
    value = payload.get(_CHECKPOINT_HEAD_KEY)
    if value is None:
        if required:
            raise ProductFactoryCheckpointIntegrityError(
                "Product Factory host task has no canonical checkpoint head"
            )
        return None
    if not isinstance(value, dict) or value.get("schema") != _CHECKPOINT_HEAD_SCHEMA:
        raise ProductFactoryCheckpointIntegrityError(
            "Product Factory host task checkpoint head is malformed"
        )
    checkpoint_id = value.get("checkpoint_id")
    checksum = value.get("checksum_sha256")
    revision = value.get("coordinator_revision")
    if (
        not isinstance(checkpoint_id, str)
        or not checkpoint_id.startswith("pf2-")
        or len(checkpoint_id) != 36
        or any(char not in "0123456789abcdef" for char in checkpoint_id[4:].casefold())
    ):
        raise ProductFactoryCheckpointIntegrityError(
            "Product Factory host task checkpoint head identity is malformed"
        )
    if (
        not isinstance(checksum, str)
        or len(checksum) != 64
        or any(char not in "0123456789abcdef" for char in checksum.casefold())
    ):
        raise ProductFactoryCheckpointIntegrityError(
            "Product Factory host task checkpoint head checksum is malformed"
        )
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        raise ProductFactoryCheckpointIntegrityError(
            "Product Factory host task checkpoint head revision is malformed"
        )
    return checkpoint_id, checksum, revision


def _checkpoint_head_payload(
    *,
    checkpoint_id: str,
    checksum: str,
    revision: int,
) -> dict[str, object]:
    return {
        "schema": _CHECKPOINT_HEAD_SCHEMA,
        "checkpoint_id": checkpoint_id,
        "checksum_sha256": checksum,
        "coordinator_revision": revision,
    }


def _checkpoint_trusted_plan_fingerprint(
    checkpoint: ProductProjectCoordinatorCheckpoint,
) -> str:
    plan = checkpoint.coordinator.trusted_plan
    if plan is None:
        raise ProductFactoryCheckpointIntegrityError(
            "checkpoint is missing immutable trusted plan descriptor"
        )
    try:
        return compute_trusted_plan_fingerprint(plan)
    except CoordinatorError as exc:
        raise ProductFactoryCheckpointIntegrityError(
            "checkpoint trusted plan descriptor is invalid"
        ) from exc


def _live_checkpoint_authority(
    checkpoint: ProductProjectCoordinatorCheckpoint,
) -> str | None:
    fingerprint = checkpoint.trusted_plan_fingerprint
    proof = checkpoint.trusted_plan_authority_proof
    if fingerprint is None and proof is None:
        return None
    try:
        return verify_live_checkpoint_authority(checkpoint)
    except ProductProjectBindingError as exc:
        raise ProductFactoryTrustedPlanAuthorityError(
            "live trusted plan authority proof is invalid"
        ) from exc


def _validate_checkpoint_authority(
    checkpoint: ProductProjectCoordinatorCheckpoint,
    authority: str,
) -> None:
    try:
        validate_trusted_plan_snapshot(checkpoint.coordinator, authority)
    except CoordinatorError as exc:
        raise ProductFactoryCheckpointIntegrityError(
            "checkpoint state disagrees with canonical host-task trusted plan authority"
        ) from exc


def _validate_checkpoint_transition(
    previous: ProductProjectCoordinatorCheckpoint,
    current: ProductProjectCoordinatorCheckpoint,
    *,
    live_authority: str | None,
) -> None:
    if (
        previous.project_id != current.project_id
        or previous.spec_version != current.spec_version
        or previous.row_version != current.row_version
    ):
        raise ProductFactoryCheckpointIntegrityError(
            "checkpoint ProductProject binding changed inside one host-task lineage"
        )

    previous_records = {
        record.request.component_id: record for record in previous.coordinator.records
    }
    current_records = {
        record.request.component_id: record for record in current.coordinator.records
    }
    if previous_records.keys() != current_records.keys():
        raise ProductFactoryCheckpointIntegrityError(
            "checkpoint component identity changed inside durable lineage"
        )

    # A durable checkpoint is not required after every in-memory coordinator method.
    # The host therefore validates transitive forward reachability through the legal
    # same-attempt state machine. Security-significant repair generation changes remain
    # stricter below and require a durable REPAIR_REQUIRED predecessor plus an exact
    # live host proof for the first durable state of the new generation.
    allowed_same_attempt = {
        WorkState.PLANNED: frozenset(
            {
                WorkState.PLANNED,
                WorkState.READY,
                WorkState.RUNNING,
                WorkState.REVIEW_REQUIRED,
                WorkState.ACCEPTED,
                WorkState.REPAIR_REQUIRED,
                WorkState.BLOCKED,
            }
        ),
        WorkState.READY: frozenset(
            {
                WorkState.READY,
                WorkState.RUNNING,
                WorkState.REVIEW_REQUIRED,
                WorkState.ACCEPTED,
                WorkState.REPAIR_REQUIRED,
                WorkState.BLOCKED,
            }
        ),
        WorkState.RUNNING: frozenset(
            {
                WorkState.RUNNING,
                WorkState.REVIEW_REQUIRED,
                WorkState.ACCEPTED,
                WorkState.REPAIR_REQUIRED,
                WorkState.BLOCKED,
            }
        ),
        WorkState.REVIEW_REQUIRED: frozenset(
            {
                WorkState.REVIEW_REQUIRED,
                WorkState.ACCEPTED,
                WorkState.REPAIR_REQUIRED,
                WorkState.BLOCKED,
            }
        ),
        WorkState.ACCEPTED: frozenset({WorkState.ACCEPTED}),
        WorkState.REPAIR_REQUIRED: frozenset(
            {WorkState.REPAIR_REQUIRED, WorkState.BLOCKED}
        ),
        WorkState.BLOCKED: frozenset({WorkState.BLOCKED}),
    }

    for component_id, previous_record in previous_records.items():
        current_record = current_records[component_id]
        previous_request = previous_record.request
        current_request = current_record.request
        attempt_delta = current_request.attempt - previous_request.attempt

        if attempt_delta == 0:
            if current_request != previous_request:
                raise ProductFactoryCheckpointIntegrityError(
                    "same-attempt work request changed inside durable checkpoint lineage"
                )
            if current_record.state not in allowed_same_attempt[previous_record.state]:
                raise ProductFactoryCheckpointIntegrityError(
                    "checkpoint work state regressed or is not legally forward-reachable"
                )
            continue

        if attempt_delta != 1:
            raise ProductFactoryCheckpointIntegrityError(
                "checkpoint work attempt skipped or regressed inside durable lineage"
            )
        if previous_record.state is not WorkState.REPAIR_REQUIRED:
            raise ProductFactoryCheckpointIntegrityError(
                "repair attempt requires a prior durable repair_required checkpoint"
            )
        if current_record.state is not WorkState.READY:
            raise ProductFactoryCheckpointIntegrityError(
                "new repair attempt must be durably checkpointed as ready before execution"
            )
        if live_authority is None:
            raise ProductFactoryTrustedPlanAuthorityError(
                "new repair generation requires live host authority proof"
            )
        _validate_repair_request_transition(previous_request, current_request)


def _validate_repair_request_transition(
    previous: ComponentWorkRequest,
    current: ComponentWorkRequest,
) -> None:
    if (
        current.project_id != previous.project_id
        or current.component_id != previous.component_id
        or current.repository_id != previous.repository_id
        or current.allowed_paths != previous.allowed_paths
        or current.permission_ceiling != previous.permission_ceiling
        or current.acceptance_commands != previous.acceptance_commands
    ):
        raise ProductFactoryCheckpointIntegrityError(
            "repair attempt changed immutable work authority"
        )
    marker = "\nRepair: "
    expected_prefix = previous.goal + marker
    if not current.goal.startswith(expected_prefix):
        raise ProductFactoryCheckpointIntegrityError(
            "repair attempt goal is not derived from the prior durable attempt"
        )
    reason = current.goal[len(expected_prefix) :]
    if not reason.strip() or marker in reason:
        raise ProductFactoryCheckpointIntegrityError(
            "repair attempt must append exactly one non-empty durable repair reason"
        )


def _validate_fingerprint(value: str) -> None:
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value.casefold()):
        raise ProductFactoryTrustedPlanAuthorityError(
            "trusted plan fingerprint must be a 64-character hexadecimal digest"
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
        "trusted_plan": (
            None
            if snapshot.trusted_plan is None
            else [_encode_request(request) for request in snapshot.trusted_plan]
        ),
    }


def _decode_coordinator(payload: Any) -> CoordinatorSnapshot:
    data = _dict(payload, "coordinator")
    records = data.get("records")
    if not isinstance(records, list):
        raise TypeError("coordinator records must be a list")
    trusted_plan = data.get("trusted_plan")
    if trusted_plan is not None and not isinstance(trusted_plan, list):
        raise TypeError("coordinator trusted plan must be a list or null")
    return CoordinatorSnapshot(
        project_id=_text(data, "project_id"),
        revision=_nonnegative_int(data, "revision"),
        records=tuple(_decode_record(item) for item in records),
        trusted_plan=(
            None
            if trusted_plan is None
            else tuple(_decode_request(item) for item in trusted_plan)
        ),
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
