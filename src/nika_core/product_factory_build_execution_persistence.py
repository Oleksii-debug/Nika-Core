from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field, fields, is_dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog
from nika_core.product_factory_build_execution import (
    BuildExecutionDispatch,
    BuildExecutionRecord,
    BuildExecutionResult,
    BuildExecutionScopeRequest,
    BuildExecutionSnapshot,
    BuildExecutionSpec,
    BuildExecutionState,
    ExecutionGrant,
)
from nika_core.product_factory_coding_worker_adapter import RepositoryPathIdentity
from nika_core.product_factory_deployment import (
    ExecutionRequest,
    NormalizedBuildEvidence,
    Platform,
    ResourceEnvelope,
    WorkLease,
)
from nika_core.toolsmith.contracts import ChangedFile


class BuildExecutionDurabilityError(RuntimeError):
    """Raised when PF5 durable execution cannot prove a safe state transition."""


class BuildExecutionCheckpointIntegrityError(BuildExecutionDurabilityError):
    """Raised when durable PF5 state fails checksum, shape, or identity validation."""


@dataclass(frozen=True, slots=True)
class BuildFileEvidence:
    dispatch_id: str
    project_id: str
    repository_id: str
    work_id: str
    source_sha: str
    platform: Platform
    changed_files: tuple[ChangedFile, ...]

    def __post_init__(self) -> None:
        if not all(
            value.strip()
            for value in (
                self.dispatch_id,
                self.project_id,
                self.repository_id,
                self.work_id,
                self.source_sha,
            )
        ):
            raise BuildExecutionDurabilityError("build file evidence identity must not be empty")


@dataclass(frozen=True, slots=True)
class DurableBuildExecutionSnapshot:
    sequence: int
    coordinator: BuildExecutionSnapshot
    leases: tuple[WorkLease, ...]
    registry_next_lease: int
    file_evidence: tuple[BuildFileEvidence, ...]

    def __post_init__(self) -> None:
        if type(self.sequence) is not int or self.sequence <= 0:
            raise BuildExecutionDurabilityError(
                "PF5 durable sequence must be a positive exact integer"
            )
        if type(self.registry_next_lease) is not int or self.registry_next_lease < 1:
            raise BuildExecutionDurabilityError("PF5 registry next-lease counter is invalid")


@dataclass(frozen=True, slots=True)
class SavedBuildExecutionCheckpoint:
    checkpoint_id: str
    snapshot: DurableBuildExecutionSnapshot
    checksum_sha256: str


_CHECKPOINT_SCHEMA = "nika-product-factory-build-execution-v1"
_CHECKPOINT_STAGE = "product_factory.build_execution.v1"
_HOST_KIND = "product_factory"


@dataclass(slots=True)
class SQLiteBuildExecutionCheckpointStore:
    """PF5 adapter over canonical SQLite ``tasks/checkpoints/audit_events`` storage."""

    store: SQLiteStore
    host_task_id: str
    project_id: str
    _audit: AuditLog = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.host_task_id.strip() or not self.project_id.strip():
            raise BuildExecutionDurabilityError("PF5 host task/project identity must not be empty")
        self._audit = AuditLog(self.store)

    def has_checkpoint(self) -> bool:
        with self.store.connection() as conn:
            self._assert_host_task(conn)
            rows = self._history_rows(conn)
            if not rows:
                return False
            self._validate_history(rows)
            return True

    def latest(self) -> SavedBuildExecutionCheckpoint:
        with self.store.connection() as conn:
            self._assert_host_task(conn)
            rows = self._history_rows(conn)
            if not rows:
                raise BuildExecutionDurabilityError(
                    "no durable PF5 build-execution checkpoint exists"
                )
            return self._validate_history(rows)[-1]

    def save(self, snapshot: DurableBuildExecutionSnapshot) -> SavedBuildExecutionCheckpoint:
        self._validate_project_binding(snapshot)
        payload = _encode_payload(snapshot)
        checksum = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        checkpoint_id = _checkpoint_identity(
            self.host_task_id,
            snapshot.sequence,
            checksum,
        )
        with self.store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._assert_host_task(conn)
            history_rows = self._history_rows(conn)
            if not history_rows:
                if snapshot.sequence != 1:
                    raise BuildExecutionDurabilityError(
                        "first durable PF5 transition must have sequence 1"
                    )
            else:
                previous = self._validate_history(history_rows)[-1]
                if snapshot.sequence == previous.snapshot.sequence:
                    if checksum != previous.checksum_sha256:
                        raise BuildExecutionDurabilityError(
                            "same PF5 durable sequence has conflicting state"
                        )
                    return previous
                if snapshot.sequence != previous.snapshot.sequence + 1:
                    raise BuildExecutionDurabilityError(
                        "PF5 durable transition sequence skipped or regressed"
                    )
                _validate_snapshot_transition(previous.snapshot, snapshot)
            try:
                conn.execute(
                    "INSERT INTO checkpoints("
                    "checkpoint_id, task_id, stage, payload_json, checksum_sha256, created_at"
                    ") VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        checkpoint_id,
                        self.host_task_id,
                        _CHECKPOINT_STAGE,
                        payload,
                        checksum,
                        datetime.now(UTC).isoformat(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise BuildExecutionDurabilityError(
                    "PF5 checkpoint identity conflicted with another durable writer"
                ) from exc
            self._audit.append_with_connection(
                conn,
                event_type="product_factory.build_execution_checkpoint_saved",
                entity_type="product_project",
                entity_id=self.project_id,
                payload={
                    "checkpoint_id": checkpoint_id,
                    "host_task_id": self.host_task_id,
                    "sequence": snapshot.sequence,
                    "record_count": len(snapshot.coordinator.records),
                },
            )
        return SavedBuildExecutionCheckpoint(checkpoint_id, snapshot, checksum)

    def _history_rows(self, conn: sqlite3.Connection):
        return conn.execute(
            "SELECT rowid AS checkpoint_rowid, checkpoint_id, payload_json, checksum_sha256 "
            "FROM checkpoints WHERE task_id=? AND stage=? ORDER BY rowid",
            (self.host_task_id, _CHECKPOINT_STAGE),
        ).fetchall()

    def _validate_history(self, rows) -> tuple[SavedBuildExecutionCheckpoint, ...]:
        validated: list[SavedBuildExecutionCheckpoint] = []
        previous: SavedBuildExecutionCheckpoint | None = None
        for expected_sequence, row in enumerate(rows, start=1):
            row_id = row["checkpoint_rowid"]
            if type(row_id) is not int or row_id <= 0:
                raise BuildExecutionCheckpointIntegrityError(
                    "PF5 checkpoint row identity is malformed"
                )
            current = self._decode_row(row)
            if current.snapshot.sequence != expected_sequence:
                raise BuildExecutionCheckpointIntegrityError(
                    "PF5 checkpoint sequence history is not contiguous"
                )
            if previous is not None:
                _validate_snapshot_transition(previous.snapshot, current.snapshot)
            validated.append(current)
            previous = current
        return tuple(validated)

    def _decode_row(self, row) -> SavedBuildExecutionCheckpoint:
        checkpoint_id = row["checkpoint_id"]
        payload = row["payload_json"]
        stored_checksum = row["checksum_sha256"]
        if (
            type(checkpoint_id) is not str
            or type(payload) is not str
            or type(stored_checksum) is not str
            or not checkpoint_id
            or not payload
            or not stored_checksum
        ):
            raise BuildExecutionCheckpointIntegrityError(
                "PF5 checkpoint storage types are malformed"
            )
        checksum = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        if checksum != stored_checksum:
            raise BuildExecutionCheckpointIntegrityError("PF5 checkpoint checksum mismatch")
        try:
            raw = json.loads(payload)
            if not isinstance(raw, dict) or raw.get("schema") != _CHECKPOINT_SCHEMA:
                raise BuildExecutionCheckpointIntegrityError("PF5 checkpoint schema mismatch")
            decoded = _decode_value(raw.get("snapshot"))
        except BuildExecutionCheckpointIntegrityError:
            raise
        except Exception as exc:
            raise BuildExecutionCheckpointIntegrityError(
                "PF5 checkpoint payload is malformed"
            ) from exc
        if not isinstance(decoded, DurableBuildExecutionSnapshot):
            raise BuildExecutionCheckpointIntegrityError("PF5 checkpoint root type is invalid")
        _validate_durable_types(decoded)
        self._validate_project_binding(decoded)
        expected_id = _checkpoint_identity(
            self.host_task_id,
            decoded.sequence,
            checksum,
        )
        if checkpoint_id != expected_id:
            raise BuildExecutionCheckpointIntegrityError(
                "PF5 checkpoint identity does not match durable sequence/checksum"
            )
        return SavedBuildExecutionCheckpoint(checkpoint_id, decoded, checksum)

    def _assert_host_task(self, conn: sqlite3.Connection) -> None:
        row = conn.execute(
            "SELECT payload_json FROM tasks WHERE task_id=?", (self.host_task_id,)
        ).fetchone()
        if row is None:
            raise BuildExecutionDurabilityError("PF5 host task does not exist")
        try:
            payload = json.loads(row["payload_json"])
        except json.JSONDecodeError as exc:
            raise BuildExecutionDurabilityError("PF5 host task payload is invalid") from exc
        if (
            not isinstance(payload, dict)
            or payload.get("kind") != _HOST_KIND
            or payload.get("product_project_id") != self.project_id
        ):
            raise BuildExecutionDurabilityError(
                "PF5 host task is not bound to the exact ProductProject identity"
            )

    def _validate_project_binding(self, snapshot: DurableBuildExecutionSnapshot) -> None:
        if not snapshot.coordinator.records:
            raise BuildExecutionDurabilityError(
                "PF5 durable checkpoint must contain execution work"
            )
        work_ids = [record.spec.request.work_id for record in snapshot.coordinator.records]
        if len(work_ids) != len(set(work_ids)) or work_ids != sorted(work_ids):
            raise BuildExecutionCheckpointIntegrityError(
                "PF5 checkpoint work identities are duplicate or non-canonical"
            )
        for record in snapshot.coordinator.records:
            if record.spec.request.project_id != self.project_id:
                raise BuildExecutionDurabilityError(
                    "PF5 checkpoint contains work outside its ProductProject identity"
                )
        evidence_ids = [item.work_id for item in snapshot.file_evidence]
        if len(evidence_ids) != len(set(evidence_ids)) or evidence_ids != sorted(evidence_ids):
            raise BuildExecutionCheckpointIntegrityError(
                "PF5 checkpoint file-evidence identities are duplicate or non-canonical"
            )
        _validate_lease_record_bindings(snapshot)


def _checkpoint_identity(host_task_id: str, sequence: int, checksum: str) -> str:
    return "pf5-" + hashlib.sha256(
        f"{host_task_id}:{sequence}:{checksum}".encode()
    ).hexdigest()


def _validate_snapshot_transition(
    previous: DurableBuildExecutionSnapshot,
    current: DurableBuildExecutionSnapshot,
) -> None:
    if current.sequence != previous.sequence + 1:
        raise BuildExecutionCheckpointIntegrityError(
            "PF5 durable transition sequence skipped or regressed"
        )
    if current.registry_next_lease < previous.registry_next_lease:
        raise BuildExecutionCheckpointIntegrityError(
            "PF5 registry lease counter regressed across durable transition"
        )

    previous_records = {
        record.spec.request.work_id: record for record in previous.coordinator.records
    }
    current_records = {
        record.spec.request.work_id: record for record in current.coordinator.records
    }
    if not set(previous_records) <= set(current_records):
        raise BuildExecutionCheckpointIntegrityError(
            "PF5 durable transition removed existing execution work"
        )

    for work_id in set(current_records) - set(previous_records):
        record = current_records[work_id]
        if (
            record.state is not BuildExecutionState.PENDING
            or record.attempt != 0
            or record.node_id is not None
            or record.lease_id is not None
            or record.dispatch is not None
            or record.evidence is not None
            or record.block_reason is not None
        ):
            raise BuildExecutionCheckpointIntegrityError(
                "new PF5 durable work did not enter through PENDING"
            )

    for work_id, old in previous_records.items():
        new = current_records[work_id]
        if new.spec != old.spec or new.grant != old.grant:
            raise BuildExecutionCheckpointIntegrityError(
                "PF5 durable execution identity changed across checkpoint lineage"
            )
        if new.attempt < old.attempt:
            raise BuildExecutionCheckpointIntegrityError(
                "PF5 durable execution attempt regressed"
            )
        if new.updated_at < old.updated_at:
            raise BuildExecutionCheckpointIntegrityError(
                "PF5 durable execution timestamp regressed"
            )
        if new.state not in _DURABLE_STATE_TRANSITIONS[old.state]:
            raise BuildExecutionCheckpointIntegrityError(
                "PF5 durable execution state transition is not legal"
            )
        if old.state in _DISPATCH_LOCKED_STATES and new.dispatch != old.dispatch:
            raise BuildExecutionCheckpointIntegrityError(
                "PF5 durable dispatch identity changed after the external-effect boundary"
            )
        if old.state in _TERMINAL_STATES and new != old:
            raise BuildExecutionCheckpointIntegrityError(
                "terminal PF5 execution changed after durable completion"
            )

    previous_evidence = {item.work_id: item for item in previous.file_evidence}
    current_evidence = {item.work_id: item for item in current.file_evidence}
    if not set(previous_evidence) <= set(current_evidence):
        raise BuildExecutionCheckpointIntegrityError(
            "PF5 durable transition removed prior file evidence"
        )
    if any(current_evidence[work_id] != item for work_id, item in previous_evidence.items()):
        raise BuildExecutionCheckpointIntegrityError(
            "PF5 durable transition rewrote prior file evidence"
        )


def _validate_lease_record_bindings(snapshot: DurableBuildExecutionSnapshot) -> None:
    records = {
        (record.spec.request.project_id, record.spec.request.work_id): record
        for record in snapshot.coordinator.records
    }
    seen_lease: set[str] = set()
    seen_node: set[str] = set()
    for lease in snapshot.leases:
        if lease.lease_id in seen_lease or lease.node_id in seen_node:
            raise BuildExecutionCheckpointIntegrityError("PF5 durable leases contain duplicates")
        seen_lease.add(lease.lease_id)
        seen_node.add(lease.node_id)
        record = records.get((lease.project_id, lease.work_id))
        if record is None or record.lease_id != lease.lease_id or record.node_id != lease.node_id:
            raise BuildExecutionCheckpointIntegrityError(
                "PF5 durable lease does not bind exact active execution record"
            )
    for record in snapshot.coordinator.records:
        active = record.state in {
            BuildExecutionState.PREPARED,
            BuildExecutionState.DISPATCHING,
            BuildExecutionState.EFFECT_IN_FLIGHT,
        }
        matched = record.lease_id is not None and record.lease_id in seen_lease
        if active != matched:
            raise BuildExecutionCheckpointIntegrityError(
                "PF5 durable active-state lease set is incomplete or excessive"
            )


def durable_state_fingerprint(snapshot: DurableBuildExecutionSnapshot) -> str:
    # Sequence is transport ordering, not logical state identity.
    logical = replace(snapshot, sequence=1)
    return hashlib.sha256(_encode_payload(logical).encode("utf-8")).hexdigest()


def _encode_payload(snapshot: DurableBuildExecutionSnapshot) -> str:
    return json.dumps(
        {"schema": _CHECKPOINT_SCHEMA, "snapshot": _encode_value(snapshot)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _encode_value(value: Any) -> Any:
    if value is None or type(value) in {str, int, bool}:
        return value
    if isinstance(value, datetime):
        return {"$datetime": _aware(value).isoformat()}
    if isinstance(value, StrEnum):
        return {"$enum": type(value).__name__, "value": value.value}
    if isinstance(value, frozenset):
        return {"$frozenset": sorted(_encode_value(item) for item in value)}
    if isinstance(value, tuple):
        return {"$tuple": [_encode_value(item) for item in value]}
    if is_dataclass(value):
        name = type(value).__name__
        if name not in _DATACLASS_TYPES:
            raise BuildExecutionDurabilityError(f"unsupported PF5 durable type: {name}")
        return {
            "$type": name,
            "fields": {
                item.name: _encode_value(getattr(value, item.name)) for item in fields(value)
            },
        }
    raise BuildExecutionDurabilityError(
        f"unsupported PF5 durable value type: {type(value).__name__}"
    )


def _decode_value(value: Any) -> Any:
    if value is None or type(value) in {str, int, bool}:
        return value
    if isinstance(value, list):
        raise BuildExecutionCheckpointIntegrityError("untyped PF5 list is not allowed")
    if not isinstance(value, dict):
        raise BuildExecutionCheckpointIntegrityError("PF5 durable value has invalid JSON type")
    if set(value) == {"$datetime"}:
        raw = value["$datetime"]
        if not isinstance(raw, str):
            raise BuildExecutionCheckpointIntegrityError("PF5 datetime must be a string")
        try:
            return _aware(datetime.fromisoformat(raw))
        except ValueError as exc:
            raise BuildExecutionCheckpointIntegrityError("PF5 datetime is invalid") from exc
    if set(value) == {"$enum", "value"}:
        enum_name = value["$enum"]
        raw = value["value"]
        if (
            not isinstance(enum_name, str)
            or not isinstance(raw, str)
            or enum_name not in _ENUM_TYPES
        ):
            raise BuildExecutionCheckpointIntegrityError("PF5 enum identity is invalid")
        return _ENUM_TYPES[enum_name](raw)
    if set(value) == {"$frozenset"}:
        raw = value["$frozenset"]
        if not isinstance(raw, list):
            raise BuildExecutionCheckpointIntegrityError("PF5 frozenset payload is invalid")
        return frozenset(_decode_value(item) for item in raw)
    if set(value) == {"$tuple"}:
        raw = value["$tuple"]
        if not isinstance(raw, list):
            raise BuildExecutionCheckpointIntegrityError("PF5 tuple payload is invalid")
        return tuple(_decode_value(item) for item in raw)
    if set(value) == {"$type", "fields"}:
        name = value["$type"]
        raw_fields = value["fields"]
        cls = _DATACLASS_TYPES.get(name) if isinstance(name, str) else None
        if cls is None or not isinstance(raw_fields, dict):
            raise BuildExecutionCheckpointIntegrityError("PF5 dataclass identity is invalid")
        expected = {item.name for item in fields(cls)}
        if set(raw_fields) != expected:
            raise BuildExecutionCheckpointIntegrityError(
                f"PF5 durable {name} fields do not match current schema"
            )
        try:
            return cls(**{key: _decode_value(raw_fields[key]) for key in expected})
        except Exception as exc:
            raise BuildExecutionCheckpointIntegrityError(
                f"PF5 durable {name} failed invariant validation"
            ) from exc
    raise BuildExecutionCheckpointIntegrityError("PF5 durable object marker is invalid")


def _validate_durable_types(snapshot: DurableBuildExecutionSnapshot) -> None:
    if type(snapshot.sequence) is not int or type(snapshot.registry_next_lease) is not int:
        raise BuildExecutionCheckpointIntegrityError(
            "PF5 integer identity used a non-integer alias"
        )
    for record in snapshot.coordinator.records:
        request = record.spec.request
        resources = request.resources
        for value in (resources.cpu_cores, resources.memory_mb, resources.disk_mb):
            if type(value) is not int or value <= 0:
                raise BuildExecutionCheckpointIntegrityError(
                    "PF5 resource envelope requires positive exact integers"
                )
        if type(request.require_gpu) is not bool:
            raise BuildExecutionCheckpointIntegrityError(
                "PF5 GPU requirement must be exact boolean"
            )
        if not isinstance(request.required_features, frozenset) or not isinstance(
            request.required_toolchains, frozenset
        ):
            raise BuildExecutionCheckpointIntegrityError("PF5 capability sets are malformed")
        if any(
            type(item) is not str
            for item in (*request.required_features, *request.required_toolchains)
        ):
            raise BuildExecutionCheckpointIntegrityError("PF5 capability identity must be string")
        if type(record.attempt) is not int or record.attempt < 0:
            raise BuildExecutionCheckpointIntegrityError("PF5 attempt identity is malformed")
        if record.evidence is not None and type(record.evidence.succeeded) is not bool:
            raise BuildExecutionCheckpointIntegrityError(
                "PF5 evidence status must be exact boolean"
            )
    for lease in snapshot.leases:
        _aware(lease.issued_at)
        _aware(lease.expires_at)
        if lease.expires_at <= lease.issued_at:
            raise BuildExecutionCheckpointIntegrityError("PF5 lease time range is invalid")
    for evidence in snapshot.file_evidence:
        for changed in evidence.changed_files:
            if type(changed.size_bytes) is not int or changed.size_bytes < 0:
                raise BuildExecutionCheckpointIntegrityError(
                    "PF5 changed-file size must be a non-negative exact integer"
                )


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise BuildExecutionDurabilityError("PF5 durable datetime must be timezone-aware")
    return value.astimezone(UTC)


_DURABLE_STATE_TRANSITIONS = {
    BuildExecutionState.PENDING: frozenset(
        {
            BuildExecutionState.PENDING,
            BuildExecutionState.WAITING_FOR_NODE,
            BuildExecutionState.WAITING_FOR_AUTHORITY,
            BuildExecutionState.PREPARED,
        }
    ),
    BuildExecutionState.WAITING_FOR_NODE: frozenset(
        {
            BuildExecutionState.WAITING_FOR_NODE,
            BuildExecutionState.WAITING_FOR_AUTHORITY,
            BuildExecutionState.PREPARED,
        }
    ),
    BuildExecutionState.WAITING_FOR_AUTHORITY: frozenset(
        {
            BuildExecutionState.WAITING_FOR_NODE,
            BuildExecutionState.WAITING_FOR_AUTHORITY,
            BuildExecutionState.PREPARED,
        }
    ),
    BuildExecutionState.PREPARED: frozenset(
        {
            BuildExecutionState.PREPARED,
            BuildExecutionState.WAITING_FOR_NODE,
            BuildExecutionState.WAITING_FOR_AUTHORITY,
            BuildExecutionState.DISPATCHING,
        }
    ),
    BuildExecutionState.DISPATCHING: frozenset(
        {
            BuildExecutionState.DISPATCHING,
            BuildExecutionState.WAITING_FOR_AUTHORITY,
            BuildExecutionState.EFFECT_IN_FLIGHT,
            BuildExecutionState.RECONCILE_REQUIRED,
        }
    ),
    BuildExecutionState.EFFECT_IN_FLIGHT: frozenset(
        {
            BuildExecutionState.EFFECT_IN_FLIGHT,
            BuildExecutionState.RECONCILE_REQUIRED,
            BuildExecutionState.SUCCEEDED,
            BuildExecutionState.FAILED,
        }
    ),
    BuildExecutionState.RECONCILE_REQUIRED: frozenset(
        {
            BuildExecutionState.RECONCILE_REQUIRED,
            BuildExecutionState.SUCCEEDED,
            BuildExecutionState.FAILED,
        }
    ),
    BuildExecutionState.SUCCEEDED: frozenset({BuildExecutionState.SUCCEEDED}),
    BuildExecutionState.FAILED: frozenset({BuildExecutionState.FAILED}),
}
_DISPATCH_LOCKED_STATES = frozenset(
    {
        BuildExecutionState.EFFECT_IN_FLIGHT,
        BuildExecutionState.RECONCILE_REQUIRED,
        BuildExecutionState.SUCCEEDED,
        BuildExecutionState.FAILED,
    }
)
_TERMINAL_STATES = frozenset(
    {
        BuildExecutionState.SUCCEEDED,
        BuildExecutionState.FAILED,
    }
)

_DATACLASS_TYPES = {
    cls.__name__: cls
    for cls in (
        DurableBuildExecutionSnapshot,
        BuildFileEvidence,
        BuildExecutionSnapshot,
        BuildExecutionRecord,
        BuildExecutionSpec,
        BuildExecutionScopeRequest,
        ExecutionGrant,
        BuildExecutionDispatch,
        BuildExecutionResult,
        ExecutionRequest,
        ResourceEnvelope,
        WorkLease,
        NormalizedBuildEvidence,
        ChangedFile,
    )
}
_ENUM_TYPES = {
    cls.__name__: cls
    for cls in (
        BuildExecutionState,
        Platform,
        RepositoryPathIdentity,
    )
}
