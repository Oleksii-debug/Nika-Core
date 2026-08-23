from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from typing import Any

from nika_core.product_project import (
    ProductProjectError,
    ProductProjectSpec,
    StaleProjectVersionError,
    _canonical,
    _now,
    _reject_secret_material,
)

_SPEC_MUTATION_EVENT = "product_project.spec_mutation_committed"
_SPEC_MUTATION_KIND = "product_project.spec_update.v1"


@dataclass(frozen=True, slots=True)
class ProductProjectSpecMutationReceipt:
    project_id: str
    operation_key: str
    previous_spec_version: int
    spec_version: int
    previous_row_version: int
    row_version: int
    input_fingerprint: str
    spec_sha256: str
    change_reason: str
    created_at: str


class ProductProjectSpecDurabilityService:
    """Crash-safe, idempotent PF0/PF12 specification mutation boundary."""

    def __init__(self, store: Any) -> None:
        self.store = store

    def update_spec(
        self,
        project_id: str,
        spec: ProductProjectSpec,
        *,
        expected_row_version: int,
        idempotency_key: str,
        change_reason: str = "specification revision",
    ) -> ProductProjectSpecMutationReceipt:
        if not project_id.strip():
            raise ProductProjectError("project_id must not be empty")
        if type(expected_row_version) is not int or expected_row_version < 0:
            raise ProductProjectError("expected_row_version must be a non-negative integer")
        if not idempotency_key.strip():
            raise ProductProjectError("idempotency_key must not be empty")
        if not change_reason.strip():
            raise ProductProjectError("change_reason must not be empty")
        _reject_secret_material(
            {"idempotency_key": idempotency_key, "change_reason": change_reason},
            path="product_project.spec_mutation",
        )
        fingerprint = self._input_fingerprint(
            project_id,
            spec,
            expected_row_version=expected_row_version,
            change_reason=change_reason,
        )

        with self.store.connection() as conn:
            # Reserve the SQLite writer before checking the ledger. A retry cannot observe
            # a half-committed spec/ledger/audit tuple from another writer.
            conn.execute("BEGIN IMMEDIATE")
            replay = conn.execute(
                "SELECT * FROM product_project_spec_idempotency WHERE operation_key=?",
                (idempotency_key,),
            ).fetchone()
            if replay is not None:
                return self._validate_replay(
                    conn,
                    replay,
                    project_id=project_id,
                    expected_row_version=expected_row_version,
                    idempotency_key=idempotency_key,
                    fingerprint=fingerprint,
                    change_reason=change_reason,
                )

            row = conn.execute(
                "SELECT current_spec_version,row_version FROM product_projects "
                "WHERE project_id=?",
                (project_id,),
            ).fetchone()
            if row is None:
                raise KeyError(project_id)
            current_spec_version = self._strict_db_int(
                row["current_spec_version"],
                label="current_spec_version",
                minimum=1,
            )
            current_row_version = self._strict_db_int(
                row["row_version"],
                label="row_version",
                minimum=0,
            )
            if current_row_version != expected_row_version:
                raise StaleProjectVersionError(
                    f"stale ProductProject write: expected {expected_row_version}, "
                    f"current {current_row_version}"
                )

            next_spec_version = current_spec_version + 1
            next_row_version = current_row_version + 1
            stored_spec = replace(
                spec,
                supersedes_spec_version=current_spec_version,
                revision_reason=change_reason,
            )
            spec_json = _canonical(stored_spec.to_dict())
            spec_sha256 = hashlib.sha256(spec_json.encode()).hexdigest()
            now = _now()

            cursor = conn.execute(
                "UPDATE product_projects SET current_spec_version=?,row_version=row_version+1,"
                "updated_at=? WHERE project_id=? AND row_version=?",
                (
                    next_spec_version,
                    now,
                    project_id,
                    expected_row_version,
                ),
            )
            if cursor.rowcount != 1:
                raise StaleProjectVersionError("concurrent ProductProject update")
            conn.execute(
                "INSERT INTO product_project_specs(project_id,spec_version,spec_json,created_at) "
                "VALUES (?,?,?,?)",
                (project_id, next_spec_version, spec_json, now),
            )
            conn.execute(
                "INSERT INTO product_project_spec_idempotency("
                "operation_key,project_id,operation_kind,expected_row_version,"
                "previous_spec_version,result_spec_version,result_row_version,"
                "input_fingerprint,spec_sha256,change_reason,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    idempotency_key,
                    project_id,
                    _SPEC_MUTATION_KIND,
                    expected_row_version,
                    current_spec_version,
                    next_spec_version,
                    next_row_version,
                    fingerprint,
                    spec_sha256,
                    change_reason,
                    now,
                ),
            )
            audit_payload = self._audit_payload(
                idempotency_key=idempotency_key,
                expected_row_version=expected_row_version,
                previous_spec_version=current_spec_version,
                spec_version=next_spec_version,
                row_version=next_row_version,
                input_fingerprint=fingerprint,
                spec_sha256=spec_sha256,
                change_reason=change_reason,
            )
            conn.execute(
                "INSERT INTO audit_events(event_type,entity_type,entity_id,payload_json,"
                "created_at) VALUES (?,?,?,?,?)",
                (
                    _SPEC_MUTATION_EVENT,
                    "product_project",
                    project_id,
                    _canonical(audit_payload),
                    now,
                ),
            )
            return ProductProjectSpecMutationReceipt(
                project_id=project_id,
                operation_key=idempotency_key,
                previous_spec_version=current_spec_version,
                spec_version=next_spec_version,
                previous_row_version=expected_row_version,
                row_version=next_row_version,
                input_fingerprint=fingerprint,
                spec_sha256=spec_sha256,
                change_reason=change_reason,
                created_at=now,
            )

    def _validate_replay(
        self,
        conn: Any,
        replay: Any,
        *,
        project_id: str,
        expected_row_version: int,
        idempotency_key: str,
        fingerprint: str,
        change_reason: str,
    ) -> ProductProjectSpecMutationReceipt:
        if (
            replay["project_id"] != project_id
            or replay["operation_kind"] != _SPEC_MUTATION_KIND
            or replay["input_fingerprint"] != fingerprint
            or replay["change_reason"] != change_reason
        ):
            raise ProductProjectError(
                "idempotency key was already used with different specification mutation input"
            )
        stored_expected_row_version = self._strict_db_int(
            replay["expected_row_version"],
            label="spec idempotency expected_row_version",
            minimum=0,
        )
        previous_spec_version = self._strict_db_int(
            replay["previous_spec_version"],
            label="spec idempotency previous_spec_version",
            minimum=1,
        )
        result_spec_version = self._strict_db_int(
            replay["result_spec_version"],
            label="spec idempotency result_spec_version",
            minimum=2,
        )
        result_row_version = self._strict_db_int(
            replay["result_row_version"],
            label="spec idempotency result_row_version",
            minimum=1,
        )
        if stored_expected_row_version != expected_row_version:
            raise ProductProjectError(
                "idempotency key was already used with a different expected row version"
            )
        if result_spec_version != previous_spec_version + 1:
            raise ProductProjectError("corrupt spec idempotency version lineage")
        if result_row_version != stored_expected_row_version + 1:
            raise ProductProjectError("corrupt spec idempotency row-version lineage")
        spec_sha256 = str(replay["spec_sha256"])
        if not self._is_sha256(spec_sha256):
            raise ProductProjectError("corrupt spec idempotency digest")

        project_row = conn.execute(
            "SELECT current_spec_version,row_version FROM product_projects WHERE project_id=?",
            (project_id,),
        ).fetchone()
        if project_row is None:
            raise ProductProjectError("spec idempotency record references missing project")
        current_spec_version = self._strict_db_int(
            project_row["current_spec_version"],
            label="current_spec_version",
            minimum=1,
        )
        current_row_version = self._strict_db_int(
            project_row["row_version"],
            label="row_version",
            minimum=0,
        )
        if current_spec_version < result_spec_version or current_row_version < result_row_version:
            raise ProductProjectError("spec idempotency record is ahead of durable project state")

        spec_row = conn.execute(
            "SELECT spec_json,created_at FROM product_project_specs "
            "WHERE project_id=? AND spec_version=?",
            (project_id, result_spec_version),
        ).fetchone()
        if spec_row is None:
            raise ProductProjectError("spec idempotency record has no durable specification")
        raw_spec = spec_row["spec_json"]
        if not isinstance(raw_spec, str):
            raise ProductProjectError("invalid durable specification payload type")
        if hashlib.sha256(raw_spec.encode()).hexdigest() != spec_sha256:
            raise ProductProjectError(
                "spec idempotency digest does not match durable specification"
            )
        try:
            parsed = json.loads(raw_spec)
        except json.JSONDecodeError as exc:
            raise ProductProjectError("invalid durable specification JSON") from exc
        if not isinstance(parsed, dict):
            raise ProductProjectError("invalid durable specification: expected object")
        try:
            stored_spec = ProductProjectSpec.from_dict(parsed)
        except (KeyError, TypeError, ValueError) as exc:
            raise ProductProjectError("invalid durable specification") from exc
        if stored_spec.supersedes_spec_version != previous_spec_version:
            raise ProductProjectError("durable specification disagrees with idempotency lineage")
        if stored_spec.revision_reason != change_reason:
            raise ProductProjectError("durable specification disagrees with mutation reason")
        if spec_row["created_at"] != replay["created_at"]:
            raise ProductProjectError("spec idempotency timestamp disagrees with specification")

        expected_audit = self._audit_payload(
            idempotency_key=idempotency_key,
            expected_row_version=stored_expected_row_version,
            previous_spec_version=previous_spec_version,
            spec_version=result_spec_version,
            row_version=result_row_version,
            input_fingerprint=fingerprint,
            spec_sha256=spec_sha256,
            change_reason=change_reason,
        )
        audit_matches = 0
        for event in conn.execute(
            "SELECT payload_json,created_at FROM audit_events "
            "WHERE event_type=? AND entity_type='product_project' AND entity_id=? "
            "ORDER BY event_id",
            (_SPEC_MUTATION_EVENT, project_id),
        ).fetchall():
            try:
                payload = json.loads(event["payload_json"])
            except (TypeError, json.JSONDecodeError) as exc:
                raise ProductProjectError("invalid spec mutation audit evidence") from exc
            if not isinstance(payload, dict):
                raise ProductProjectError("invalid spec mutation audit evidence")
            if payload.get("operation_key") != idempotency_key:
                continue
            if payload != expected_audit or event["created_at"] != replay["created_at"]:
                raise ProductProjectError("spec idempotency disagrees with durable audit evidence")
            audit_matches += 1
        if audit_matches != 1:
            raise ProductProjectError(
                "spec idempotency record must have exactly one durable audit evidence event"
            )

        return ProductProjectSpecMutationReceipt(
            project_id=project_id,
            operation_key=idempotency_key,
            previous_spec_version=previous_spec_version,
            spec_version=result_spec_version,
            previous_row_version=stored_expected_row_version,
            row_version=result_row_version,
            input_fingerprint=fingerprint,
            spec_sha256=spec_sha256,
            change_reason=change_reason,
            created_at=str(replay["created_at"]),
        )

    @staticmethod
    def _input_fingerprint(
        project_id: str,
        spec: ProductProjectSpec,
        *,
        expected_row_version: int,
        change_reason: str,
    ) -> str:
        payload = {
            "project_id": project_id,
            "expected_row_version": expected_row_version,
            "spec": spec.to_dict(),
            "change_reason": change_reason,
        }
        return hashlib.sha256(_canonical(payload).encode()).hexdigest()

    @staticmethod
    def _audit_payload(
        *,
        idempotency_key: str,
        expected_row_version: int,
        previous_spec_version: int,
        spec_version: int,
        row_version: int,
        input_fingerprint: str,
        spec_sha256: str,
        change_reason: str,
    ) -> dict[str, Any]:
        return {
            "operation_key": idempotency_key,
            "operation_kind": _SPEC_MUTATION_KIND,
            "expected_row_version": expected_row_version,
            "previous_spec_version": previous_spec_version,
            "spec_version": spec_version,
            "row_version": row_version,
            "input_fingerprint": input_fingerprint,
            "spec_sha256": spec_sha256,
            "change_reason": change_reason,
        }

    @staticmethod
    def _strict_db_int(value: Any, *, label: str, minimum: int) -> int:
        if type(value) is not int or value < minimum:
            raise ProductProjectError(f"invalid durable {label}")
        return value

    @staticmethod
    def _is_sha256(value: str) -> bool:
        return len(value) == 64 and all(character in "0123456789abcdef" for character in value)
