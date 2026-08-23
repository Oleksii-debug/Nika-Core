from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_factory_fleet_replacement import (
    DurableReplacementDispatch,
    FleetReplacementError,
    ReplicaReplacementRequest,
    ReplicaReplacementResult,
    fleet_replacement_request_fingerprint,
    fleet_replacement_result_fingerprint,
)

_SCHEMA_VERSION = 1
_SCHEMA_TABLE = "fleet_replacement_schema_migrations"
_DISPATCH_TABLE = "fleet_replacement_dispatches"


class SQLiteFleetReplacementDispatchJournal:
    """PF3-local durable intent/result journal for replacement provider effects.

    The journal is deliberately narrow. It does not persist the whole fleet coordinator or
    replace ProductProject/checkpoint ownership. It only closes the crash window around an
    external replacement effect by durably recording the exact dispatch before ``apply``
    and exact terminal provider evidence before the in-memory record becomes terminal.
    """

    def __init__(self, store: SQLiteStore) -> None:
        self._store = store
        self._initialize()

    def prepare(
        self,
        request: ReplicaReplacementRequest,
        *,
        attempt: int,
        source_was_enabled: bool,
    ) -> DurableReplacementDispatch:
        if attempt <= 0:
            raise FleetReplacementError("durable replacement dispatch attempt must be positive")
        if not isinstance(source_was_enabled, bool):
            raise FleetReplacementError(
                "durable replacement dispatch requires exact source cordon provenance"
            )
        request_json = _canonical(_request_payload(request))
        request_checksum = fleet_replacement_request_fingerprint(request)
        now = datetime.now(UTC).isoformat()
        with self._store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                f"""
                SELECT * FROM {_DISPATCH_TABLE}
                WHERE plan_id = ? AND service_id = ? AND replica_id = ?
                """,
                (request.plan_id, request.service_id, request.replica_id),
            ).fetchone()
            if existing is not None:
                record = self._row_to_record(existing)
                if (
                    record.request != request
                    or record.attempt != attempt
                    or record.source_was_enabled is not source_was_enabled
                    or record.request_checksum_sha256 != request_checksum
                ):
                    raise FleetReplacementError(
                        "durable replacement dispatch conflicts with prior replica effect identity"
                    )
                return record
            try:
                conn.execute(
                    f"""
                    INSERT INTO {_DISPATCH_TABLE}(
                        request_id, plan_id, project_id, service_id, replica_id,
                        attempt, source_was_enabled, request_json, request_checksum_sha256,
                        result_json, result_checksum_sha256, created_at, resolved_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, NULL)
                    """,
                    (
                        request.request_id,
                        request.plan_id,
                        request.project_id,
                        request.service_id,
                        request.replica_id,
                        attempt,
                        int(source_was_enabled),
                        request_json,
                        request_checksum,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise FleetReplacementError(
                    "durable replacement dispatch identity conflicts with existing journal state"
                ) from exc
        return DurableReplacementDispatch(
            request=request,
            attempt=attempt,
            source_was_enabled=source_was_enabled,
            request_checksum_sha256=request_checksum,
        )

    def record_terminal(
        self,
        request: ReplicaReplacementRequest,
        result: ReplicaReplacementResult,
    ) -> DurableReplacementDispatch:
        if result.uncertain:
            raise FleetReplacementError(
                "uncertain replacement result cannot become durable terminal evidence"
            )
        result_json = _canonical(_result_payload(result))
        result_checksum = fleet_replacement_result_fingerprint(result)
        now = datetime.now(UTC).isoformat()
        with self._store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                f"SELECT * FROM {_DISPATCH_TABLE} WHERE request_id = ?",
                (request.request_id,),
            ).fetchone()
            if row is None:
                raise FleetReplacementError(
                    "terminal replacement evidence has no durable pre-effect dispatch"
                )
            existing = self._row_to_record(row)
            if existing.request != request:
                raise FleetReplacementError(
                    "terminal replacement evidence request conflicts with durable dispatch"
                )
            if existing.terminal_result is not None:
                if (
                    existing.terminal_result != result
                    or existing.result_checksum_sha256 != result_checksum
                ):
                    raise FleetReplacementError(
                        "terminal replacement evidence conflicts with durable provider result"
                    )
                return existing
            conn.execute(
                f"""
                UPDATE {_DISPATCH_TABLE}
                SET result_json = ?, result_checksum_sha256 = ?, resolved_at = ?
                WHERE request_id = ?
                """,
                (result_json, result_checksum, now, request.request_id),
            )
            return DurableReplacementDispatch(
                request=request,
                attempt=existing.attempt,
                source_was_enabled=existing.source_was_enabled,
                request_checksum_sha256=existing.request_checksum_sha256,
                terminal_result=result,
                result_checksum_sha256=result_checksum,
            )

    def list_plan(self, plan_id: str) -> tuple[DurableReplacementDispatch, ...]:
        if not plan_id.strip():
            raise FleetReplacementError("durable replacement plan identity must not be empty")
        with self._store.connection() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM {_DISPATCH_TABLE}
                WHERE plan_id = ?
                ORDER BY service_id, replica_id, attempt, request_id
                """,
                (plan_id,),
            ).fetchall()
        return tuple(self._row_to_record(row) for row in rows)

    def _initialize(self) -> None:
        with self._store.connection() as conn:
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {_SCHEMA_TABLE}(
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                )
                """
            )
            row = conn.execute(
                f"SELECT MAX(version) AS version FROM {_SCHEMA_TABLE}"
            ).fetchone()
            current = int(row["version"] or 0)
            if current > _SCHEMA_VERSION:
                raise FleetReplacementError(
                    "fleet replacement journal schema is newer than supported"
                )
            if current == 0:
                conn.execute(
                    f"""
                    CREATE TABLE {_DISPATCH_TABLE}(
                        request_id TEXT PRIMARY KEY,
                        plan_id TEXT NOT NULL,
                        project_id TEXT NOT NULL,
                        service_id TEXT NOT NULL,
                        replica_id TEXT NOT NULL,
                        attempt INTEGER NOT NULL,
                        source_was_enabled INTEGER NOT NULL,
                        request_json TEXT NOT NULL,
                        request_checksum_sha256 TEXT NOT NULL,
                        result_json TEXT,
                        result_checksum_sha256 TEXT,
                        created_at TEXT NOT NULL,
                        resolved_at TEXT,
                        UNIQUE(plan_id, service_id, replica_id)
                    )
                    """
                )
                conn.execute(
                    f"INSERT INTO {_SCHEMA_TABLE}(version, applied_at) VALUES (?, ?)",
                    (_SCHEMA_VERSION, datetime.now(UTC).isoformat()),
                )
            else:
                try:
                    conn.execute(f"SELECT request_id FROM {_DISPATCH_TABLE} LIMIT 1").fetchone()
                except sqlite3.OperationalError as exc:
                    raise FleetReplacementError(
                        "fleet replacement journal schema marker exists but dispatch table is missing"
                    ) from exc

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> DurableReplacementDispatch:
        try:
            attempt = int(row["attempt"])
            source_raw = int(row["source_was_enabled"])
            request_payload = json.loads(str(row["request_json"]))
            request = _request_from_payload(request_payload)
            stored_request_checksum = str(row["request_checksum_sha256"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise FleetReplacementError(
                "durable replacement dispatch row is malformed"
            ) from exc
        if attempt <= 0 or source_raw not in {0, 1}:
            raise FleetReplacementError("durable replacement dispatch metadata is invalid")
        if row["request_id"] != request.request_id:
            raise FleetReplacementError(
                "durable replacement dispatch row request identity is inconsistent"
            )
        if (
            row["plan_id"] != request.plan_id
            or row["project_id"] != request.project_id
            or row["service_id"] != request.service_id
            or row["replica_id"] != request.replica_id
        ):
            raise FleetReplacementError(
                "durable replacement dispatch row scope identity is inconsistent"
            )
        expected_request_checksum = fleet_replacement_request_fingerprint(request)
        if stored_request_checksum != expected_request_checksum:
            raise FleetReplacementError(
                "durable replacement dispatch request checksum is corrupt"
            )

        result_json = row["result_json"]
        result_checksum_raw = row["result_checksum_sha256"]
        result: ReplicaReplacementResult | None = None
        result_checksum: str | None = None
        if (result_json is None) != (result_checksum_raw is None):
            raise FleetReplacementError(
                "durable replacement terminal evidence is partially persisted"
            )
        if result_json is not None:
            try:
                result = _result_from_payload(json.loads(str(result_json)))
                result_checksum = str(result_checksum_raw)
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise FleetReplacementError(
                    "durable replacement terminal evidence is malformed"
                ) from exc
            if result.uncertain:
                raise FleetReplacementError(
                    "durable replacement journal contains uncertain terminal evidence"
                )
            if result_checksum != fleet_replacement_result_fingerprint(result):
                raise FleetReplacementError(
                    "durable replacement terminal result checksum is corrupt"
                )
        return DurableReplacementDispatch(
            request=request,
            attempt=attempt,
            source_was_enabled=bool(source_raw),
            request_checksum_sha256=stored_request_checksum,
            terminal_result=result,
            result_checksum_sha256=result_checksum,
        )


def _request_payload(request: ReplicaReplacementRequest) -> dict[str, Any]:
    return {
        "request_id": request.request_id,
        "plan_id": request.plan_id,
        "project_id": request.project_id,
        "fleet_plan_id": request.fleet_plan_id,
        "environment_id": request.environment_id,
        "service_id": request.service_id,
        "replica_id": request.replica_id,
        "deployment_operation_id": request.deployment_operation_id,
        "source_node_id": request.source_node_id,
        "target_node_id": request.target_node_id,
        "release_version": request.release_version,
        "release_sha": request.release_sha,
        "artifact_digest": request.artifact_digest,
        "reason": request.reason,
        "approval_ref": request.approval_ref,
        "authorization_work_id": request.authorization_work_id,
        "review_ref": request.review_ref,
        "plan_fingerprint": request.plan_fingerprint,
        "evidence_refs": list(request.evidence_refs),
    }


def _request_from_payload(payload: object) -> ReplicaReplacementRequest:
    expected = {
        "request_id",
        "plan_id",
        "project_id",
        "fleet_plan_id",
        "environment_id",
        "service_id",
        "replica_id",
        "deployment_operation_id",
        "source_node_id",
        "target_node_id",
        "release_version",
        "release_sha",
        "artifact_digest",
        "reason",
        "approval_ref",
        "authorization_work_id",
        "review_ref",
        "plan_fingerprint",
        "evidence_refs",
    }
    if not isinstance(payload, dict) or set(payload) != expected:
        raise FleetReplacementError("durable replacement request payload shape is invalid")
    evidence = payload["evidence_refs"]
    if not isinstance(evidence, list) or not all(isinstance(item, str) for item in evidence):
        raise FleetReplacementError("durable replacement request evidence is invalid")
    scalar_keys = expected - {"evidence_refs"}
    if any(not isinstance(payload[key], str) for key in scalar_keys):
        raise FleetReplacementError("durable replacement request scalar identity is invalid")
    return ReplicaReplacementRequest(
        request_id=payload["request_id"],
        plan_id=payload["plan_id"],
        project_id=payload["project_id"],
        fleet_plan_id=payload["fleet_plan_id"],
        environment_id=payload["environment_id"],
        service_id=payload["service_id"],
        replica_id=payload["replica_id"],
        deployment_operation_id=payload["deployment_operation_id"],
        source_node_id=payload["source_node_id"],
        target_node_id=payload["target_node_id"],
        release_version=payload["release_version"],
        release_sha=payload["release_sha"],
        artifact_digest=payload["artifact_digest"],
        reason=payload["reason"],
        approval_ref=payload["approval_ref"],
        authorization_work_id=payload["authorization_work_id"],
        review_ref=payload["review_ref"],
        plan_fingerprint=payload["plan_fingerprint"],
        evidence_refs=tuple(evidence),
    )


def _result_payload(result: ReplicaReplacementResult) -> dict[str, Any]:
    return {
        "applied": result.applied,
        "uncertain": result.uncertain,
        "evidence_refs": list(result.evidence_refs),
        "observed_node_id": result.observed_node_id,
        "release_version": result.release_version,
        "release_sha": result.release_sha,
        "artifact_digest": result.artifact_digest,
        "healthy": result.healthy,
    }


def _result_from_payload(payload: object) -> ReplicaReplacementResult:
    expected = {
        "applied",
        "uncertain",
        "evidence_refs",
        "observed_node_id",
        "release_version",
        "release_sha",
        "artifact_digest",
        "healthy",
    }
    if not isinstance(payload, dict) or set(payload) != expected:
        raise FleetReplacementError("durable replacement result payload shape is invalid")
    if not isinstance(payload["applied"], bool) or not isinstance(payload["uncertain"], bool):
        raise FleetReplacementError("durable replacement result disposition is invalid")
    evidence = payload["evidence_refs"]
    if not isinstance(evidence, list) or not all(isinstance(item, str) for item in evidence):
        raise FleetReplacementError("durable replacement result evidence is invalid")
    for key in ("observed_node_id", "release_version", "release_sha", "artifact_digest"):
        if payload[key] is not None and not isinstance(payload[key], str):
            raise FleetReplacementError("durable replacement result identity is invalid")
    if payload["healthy"] is not None and not isinstance(payload["healthy"], bool):
        raise FleetReplacementError("durable replacement result health is invalid")
    return ReplicaReplacementResult(
        applied=payload["applied"],
        uncertain=payload["uncertain"],
        evidence_refs=tuple(evidence),
        observed_node_id=payload["observed_node_id"],
        release_version=payload["release_version"],
        release_sha=payload["release_sha"],
        artifact_digest=payload["artifact_digest"],
        healthy=payload["healthy"],
    )


def _canonical(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
