from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from nika_core.artifacts.contracts import (
    ArtifactConflictError,
    ArtifactRecord,
    ArtifactRegistryError,
    ArtifactVerification,
)
from nika_core.data.sqlite import SQLiteStore


_MAX_DURABLE_JSON_BYTES = 1_048_576
_MAX_QUERY_TEXT_BYTES = 4096


def _query_text(value: object, *, field: str) -> str:
    if type(value) is not str:
        raise ValueError(f"{field} must be text")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{field} must be valid UTF-8 text") from exc
    if len(encoded) > _MAX_QUERY_TEXT_BYTES:
        raise ValueError(f"{field} exceeds the {_MAX_QUERY_TEXT_BYTES}-byte query limit")
    return value


def _query_int(
    value: object,
    *,
    field: str,
    minimum: int,
    maximum: int | None = None,
) -> int:
    if type(value) is not int:
        raise ValueError(f"{field} must be an integer")
    if value < minimum or (maximum is not None and value > maximum):
        if maximum is None:
            raise ValueError(f"{field} must be at least {minimum}")
        raise ValueError(f"{field} must be between {minimum} and {maximum}")
    return value


def _sha256_text(value: str) -> str:
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ArtifactRegistryError("artifact identity material is invalid UTF-8 text") from exc
    return hashlib.sha256(encoded).hexdigest()


def _expected_artifact_id(record: ArtifactRecord) -> str:
    return _sha256_text(f"{record.workspace_id}\0{record.idempotency_key}")


def _expected_verification_id(verification: ArtifactVerification) -> str:
    material = "\0".join(
        (
            verification.artifact_id,
            verification.checked_at.isoformat(),
            verification.state.value,
            verification.actual_sha256 or "",
            "" if verification.actual_size_bytes is None else str(verification.actual_size_bytes),
        )
    )
    return _sha256_text(material)


def _validated_record_for_write(record: ArtifactRecord) -> ArtifactRecord:
    try:
        validated = ArtifactRecord.model_validate(record.model_dump(round_trip=True))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ArtifactRegistryError("artifact record input is invalid") from exc
    if validated.artifact_id != _expected_artifact_id(validated):
        raise ArtifactRegistryError(
            "artifact record deterministic identity does not match record input"
        )
    return validated


def _validated_verification_for_write(
    verification: ArtifactVerification,
) -> ArtifactVerification:
    try:
        validated = ArtifactVerification.model_validate(
            verification.model_dump(round_trip=True)
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise ArtifactRegistryError("artifact verification input is invalid") from exc
    if validated.verification_id != _expected_verification_id(validated):
        raise ArtifactRegistryError(
            "artifact verification deterministic identity does not match evidence input"
        )
    return validated


def _same_registration(left: ArtifactRecord, right: ArtifactRecord) -> bool:
    left_data = left.model_dump(exclude={"created_at"})
    right_data = right.model_dump(exclude={"created_at"})
    return left_data == right_data


def _stored_int(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ArtifactRegistryError(f"{field} must be stored as SQLite INTEGER")
    return value


def _reject_nonfinite_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant is not allowed: {value}")


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate durable JSON key: {key}")
        value[key] = item
    return value


def _bounded_durable_json_text(value: object, *, field: str) -> str:
    if type(value) is not str:
        raise ArtifactRegistryError(f"{field} must be stored as SQLite TEXT")
    if len(value) > _MAX_DURABLE_JSON_BYTES:
        raise ArtifactRegistryError(f"{field} exceeds the 1 MiB durable JSON limit")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ArtifactRegistryError(f"{field} is invalid UTF-8 text") from exc
    if len(encoded) > _MAX_DURABLE_JSON_BYTES:
        raise ArtifactRegistryError(f"{field} exceeds the 1 MiB durable JSON limit")
    return value


def _load_durable_json(value: object, *, field: str) -> dict[str, Any]:
    text = _bounded_durable_json_text(value, field=field)
    try:
        payload = json.loads(
            text,
            parse_constant=_reject_nonfinite_json_constant,
            object_pairs_hook=_unique_json_object,
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise ArtifactRegistryError(f"{field} is invalid") from exc
    if type(payload) is not dict:
        raise ArtifactRegistryError(f"{field} is invalid")
    return payload


def _record_from_row(row: Any) -> ArtifactRecord:
    payload = _load_durable_json(
        row["record_json"],
        field="artifact registry record payload",
    )
    try:
        record = ArtifactRecord.model_validate(payload)
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
    if record.artifact_id != _expected_artifact_id(record):
        raise ArtifactRegistryError(
            "artifact registry deterministic identity does not match immutable record payload"
        )
    return record


def _verification_from_row(row: Any) -> ArtifactVerification:
    payload = _load_durable_json(
        row["verification_json"],
        field="artifact verification payload",
    )
    try:
        verification = ArtifactVerification.model_validate(payload)
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
    if verification.verification_id != _expected_verification_id(verification):
        raise ArtifactRegistryError(
            "artifact verification deterministic identity does not match evidence payload"
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
        record = _validated_record_for_write(record)
        record_json = _bounded_durable_json_text(
            record.model_dump_json(),
            field="artifact registry record payload",
        )
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
                        record_json,
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
        artifact_id = _query_text(artifact_id, field="artifact_id")
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
        workspace_id = _query_text(workspace_id, field="workspace_id")
        idempotency_key = _query_text(idempotency_key, field="idempotency_key")
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
        limit = _query_int(limit, field="limit", minimum=1, maximum=1000)
        offset = _query_int(offset, field="offset", minimum=0)
        if workspace_id is not None:
            workspace_id = _query_text(workspace_id, field="workspace_id")
        if kind is not None:
            kind = _query_text(kind, field="kind")
        if producer_id is not None:
            producer_id = _query_text(producer_id, field="producer_id")

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
        sha256 = _query_text(sha256, field="sha256")
        if workspace_id is not None:
            workspace_id = _query_text(workspace_id, field="workspace_id")
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
        verification = _validated_verification_for_write(verification)
        try:
            record = self.get(verification.artifact_id)
        except KeyError as exc:
            raise ArtifactRegistryError(
                "artifact verification references an unknown artifact"
            ) from exc
        if (
            verification.expected_sha256 != record.sha256
            or verification.expected_size_bytes != record.size_bytes
        ):
            raise ArtifactRegistryError(
                "artifact verification expected metadata does not match immutable artifact"
            )
        verification_json = _bounded_durable_json_text(
            verification.model_dump_json(),
            field="artifact verification payload",
        )
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
                        verification_json,
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
        record = self.get(artifact_id)
        with self._store.connection() as conn:
            rows = conn.execute(
                f"SELECT {_VERIFICATION_COLUMNS} FROM artifact_registry_verifications "
                "WHERE artifact_id = ? ORDER BY checked_at, verification_id",
                (artifact_id,),
            ).fetchall()
        verifications = tuple(_verification_from_row(row) for row in rows)
        if any(item.artifact_id != artifact_id for item in verifications):
            raise ArtifactRegistryError("artifact verification index is inconsistent")
        if any(
            item.expected_sha256 != record.sha256
            or item.expected_size_bytes != record.size_bytes
            for item in verifications
        ):
            raise ArtifactRegistryError(
                "artifact verification expected metadata does not match immutable artifact"
            )
        return verifications
