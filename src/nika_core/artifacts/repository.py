from __future__ import annotations

import sqlite3
from typing import Any

from nika_core.artifacts.contracts import (
    ArtifactConflictError,
    ArtifactRecord,
    ArtifactRegistryError,
    ArtifactVerification,
)
from nika_core.data.sqlite import SQLiteStore


def _same_registration(left: ArtifactRecord, right: ArtifactRecord) -> bool:
    left_data = left.model_dump(exclude={"created_at"})
    right_data = right.model_dump(exclude={"created_at"})
    return left_data == right_data


def _stored_int(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ArtifactRegistryError(f"{field} must be stored as SQLite INTEGER")
    return value


def _record_from_row(row: Any) -> ArtifactRecord:
    try:
        record = ArtifactRecord.model_validate_json(str(row["record_json"]))
    except (TypeError, ValueError) as exc:
        raise ArtifactRegistryError("artifact registry record payload is invalid") from exc

    stored_size = _stored_int(row["size_bytes"], field="artifact size")
    bindings = (
        (str(row["artifact_id"]), record.artifact_id),
        (str(row["workspace_id"]), record.workspace_id),
        (str(row["idempotency_key"]), record.idempotency_key),
        (str(row["kind"]), record.kind),
        (str(row["sha256"]), record.sha256),
        (str(row["location_kind"]), record.location_kind.value),
        (row["producer_id"], record.producer_id),
        (str(row["created_at"]), record.created_at.isoformat()),
    )
    if stored_size != record.size_bytes or any(left != right for left, right in bindings):
        raise ArtifactRegistryError(
            "artifact registry indexed metadata does not match immutable record payload"
        )
    return record


def _verification_from_row(row: Any) -> ArtifactVerification:
    try:
        verification = ArtifactVerification.model_validate_json(
            str(row["verification_json"])
        )
    except (TypeError, ValueError) as exc:
        raise ArtifactRegistryError("artifact verification payload is invalid") from exc

    bindings = (
        (str(row["verification_id"]), verification.verification_id),
        (str(row["artifact_id"]), verification.artifact_id),
        (str(row["state"]), verification.state.value),
        (str(row["checked_at"]), verification.checked_at.isoformat()),
    )
    if any(left != right for left, right in bindings):
        raise ArtifactRegistryError(
            "artifact verification indexed metadata does not match evidence payload"
        )
    return verification


_RECORD_COLUMNS = (
    "artifact_id, workspace_id, idempotency_key, kind, sha256, size_bytes, "
    "location_kind, producer_id, record_json, created_at"
)
_VERIFICATION_COLUMNS = (
    "verification_id, artifact_id, state, verification_json, checked_at"
)


class SQLiteArtifactRepository:
    def __init__(self, store: SQLiteStore) -> None:
        self._store = store

    def put_record(self, record: ArtifactRecord) -> ArtifactRecord:
        try:
            with self._store.connection() as conn:
                conn.execute(
                    """INSERT INTO artifact_registry_records(
                        artifact_id,
                        workspace_id,
                        idempotency_key,
                        kind,
                        sha256,
                        size_bytes,
                        location_kind,
                        producer_id,
                        record_json,
                        created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        record.artifact_id,
                        record.workspace_id,
                        record.idempotency_key,
                        record.kind,
                        record.sha256,
                        record.size_bytes,
                        record.location_kind.value,
                        record.producer_id,
                        record.model_dump_json(),
                        record.created_at.isoformat(),
                    ),
                )
            return record
        except sqlite3.IntegrityError:
            existing = self.get_by_idempotency(record.workspace_id, record.idempotency_key)
            if existing is not None and _same_registration(existing, record):
                return existing
            raise ArtifactConflictError(
                "artifact idempotency key is already bound to different immutable metadata"
            ) from None

    def get(self, artifact_id: str) -> ArtifactRecord:
        with self._store.connection() as conn:
            row = conn.execute(
                f"SELECT {_RECORD_COLUMNS} FROM artifact_registry_records "
                "WHERE artifact_id = ?",
                (artifact_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"Unknown artifact: {artifact_id}")
        return _record_from_row(row)

    def get_by_idempotency(
        self,
        workspace_id: str,
        idempotency_key: str,
    ) -> ArtifactRecord | None:
        with self._store.connection() as conn:
            row = conn.execute(
                f"SELECT {_RECORD_COLUMNS} FROM artifact_registry_records "
                "WHERE workspace_id = ? AND idempotency_key = ?",
                (workspace_id, idempotency_key),
            ).fetchone()
        if row is None:
            return None
        return _record_from_row(row)

    def list_records(
        self,
        *,
        workspace_id: str | None = None,
        kind: str | None = None,
        producer_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[ArtifactRecord, ...]:
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        if offset < 0:
            raise ValueError("offset must be non-negative")

        clauses: list[str] = []
        parameters: list[object] = []
        if workspace_id is not None:
            clauses.append("workspace_id = ?")
            parameters.append(workspace_id)
        if kind is not None:
            clauses.append("kind = ?")
            parameters.append(kind)
        if producer_id is not None:
            clauses.append("producer_id = ?")
            parameters.append(producer_id)

        query = f"SELECT {_RECORD_COLUMNS} FROM artifact_registry_records"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at, artifact_id LIMIT ? OFFSET ?"
        parameters.extend((limit, offset))

        with self._store.connection() as conn:
            rows = conn.execute(query, tuple(parameters)).fetchall()

        records = tuple(_record_from_row(row) for row in rows)
        if workspace_id is not None and any(
            record.workspace_id != workspace_id for record in records
        ):
            raise ArtifactRegistryError("artifact registry workspace index is inconsistent")
        if kind is not None and any(record.kind != kind for record in records):
            raise ArtifactRegistryError("artifact registry kind index is inconsistent")
        if producer_id is not None and any(
            record.producer_id != producer_id for record in records
        ):
            raise ArtifactRegistryError("artifact registry producer index is inconsistent")
        return records

    def find_by_sha256(
        self,
        sha256: str,
        *,
        workspace_id: str | None = None,
    ) -> tuple[ArtifactRecord, ...]:
        clauses = ["sha256 = ?"]
        parameters: list[object] = [sha256]
        if workspace_id is not None:
            clauses.append("workspace_id = ?")
            parameters.append(workspace_id)
        query = (
            f"SELECT {_RECORD_COLUMNS} FROM artifact_registry_records WHERE "
            + " AND ".join(clauses)
            + " ORDER BY created_at, artifact_id"
        )
        with self._store.connection() as conn:
            rows = conn.execute(query, tuple(parameters)).fetchall()
        records = tuple(_record_from_row(row) for row in rows)
        if any(record.sha256 != sha256 for record in records):
            raise ArtifactRegistryError("artifact registry digest index is inconsistent")
        if workspace_id is not None and any(
            record.workspace_id != workspace_id for record in records
        ):
            raise ArtifactRegistryError("artifact registry workspace index is inconsistent")
        return records

    def put_verification(self, verification: ArtifactVerification) -> ArtifactVerification:
        try:
            with self._store.connection() as conn:
                conn.execute(
                    """INSERT INTO artifact_registry_verifications(
                        verification_id,
                        artifact_id,
                        state,
                        verification_json,
                        checked_at
                    ) VALUES (?, ?, ?, ?, ?)""",
                    (
                        verification.verification_id,
                        verification.artifact_id,
                        verification.state.value,
                        verification.model_dump_json(),
                        verification.checked_at.isoformat(),
                    ),
                )
            return verification
        except sqlite3.IntegrityError:
            with self._store.connection() as conn:
                row = conn.execute(
                    f"SELECT {_VERIFICATION_COLUMNS} "
                    "FROM artifact_registry_verifications WHERE verification_id = ?",
                    (verification.verification_id,),
                ).fetchone()
            if row is None:
                raise
            existing = _verification_from_row(row)
            if existing != verification:
                raise ArtifactConflictError(
                    "verification identity is already bound to different evidence"
                ) from None
            return existing

    def list_verifications(self, artifact_id: str) -> tuple[ArtifactVerification, ...]:
        with self._store.connection() as conn:
            rows = conn.execute(
                f"SELECT {_VERIFICATION_COLUMNS} FROM artifact_registry_verifications "
                "WHERE artifact_id = ? ORDER BY checked_at, verification_id",
                (artifact_id,),
            ).fetchall()
        verifications = tuple(_verification_from_row(row) for row in rows)
        if any(item.artifact_id != artifact_id for item in verifications):
            raise ArtifactRegistryError("artifact verification index is inconsistent")
        return verifications
