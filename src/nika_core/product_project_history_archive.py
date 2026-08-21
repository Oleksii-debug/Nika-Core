from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from nika_core.product_project import ProductProjectError, StaleProjectVersionError
from nika_core.product_project_history_integrity import (
    ProductProjectHistoricalIntegrityService,
)

_ARCHIVE_SCHEMA = "nika-product-project-history-archive-v1"
_JSON_COLUMNS = {
    "spec_json": "spec",
    "payload_json": "payload",
    "evidence_package_ids_json": "evidence_package_ids",
}


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ProductProjectHistoryArchiveSummary:
    project_id: str
    spec_version: int
    row_version: int
    spec_count: int
    research_package_count: int
    decision_version_count: int
    audit_event_count: int
    mutation_idempotency_count: int
    digest_sha256: str


@dataclass(frozen=True, slots=True)
class ProductProjectHistoryArchive:
    summary: ProductProjectHistoryArchiveSummary
    bytes: bytes


class ProductProjectHistoryArchiveService:
    """Build and verify deterministic, tamper-evident PF1 history archives."""

    def __init__(self, store: Any) -> None:
        self.store = store

    def build(
        self,
        project_id: str,
        *,
        expected_spec_version: int | None = None,
        expected_row_version: int | None = None,
    ) -> ProductProjectHistoryArchive:
        historical = ProductProjectHistoricalIntegrityService(self.store).validate(
            project_id,
            expected_spec_version=expected_spec_version,
            expected_row_version=expected_row_version,
        )
        spec_version = historical.current.spec_version
        row_version = historical.current.row_version

        with self.store.connection() as conn:
            conn.execute("BEGIN")
            project_row = conn.execute(
                "SELECT project_id,name,current_spec_version,row_version,status,created_at,"
                "updated_at FROM product_projects WHERE project_id=?",
                (project_id,),
            ).fetchone()
            if project_row is None:
                raise KeyError(project_id)
            self._require_exact_versions(project_row, spec_version, row_version)

            project = self._row(project_row)
            specs = self._rows(
                conn.execute(
                    "SELECT project_id,spec_version,spec_json,created_at "
                    "FROM product_project_specs WHERE project_id=? ORDER BY spec_version",
                    (project_id,),
                ).fetchall()
            )
            research = self._rows(
                conn.execute(
                    "SELECT project_id,package_id,payload_json,created_at "
                    "FROM product_research_handoffs WHERE project_id=? ORDER BY package_id",
                    (project_id,),
                ).fetchall()
            )
            decisions = self._rows(
                conn.execute(
                    "SELECT project_id,decision_id,decision_version,option_id,state,rationale,"
                    "decided_by_ref,evidence_package_ids_json,created_at "
                    "FROM product_decisions WHERE project_id=? "
                    "ORDER BY decision_id,decision_version",
                    (project_id,),
                ).fetchall()
            )
            creation_idempotency = self._rows(
                conn.execute(
                    "SELECT operation_key,project_id,input_fingerprint,created_at "
                    "FROM product_project_idempotency WHERE project_id=? ORDER BY operation_key",
                    (project_id,),
                ).fetchall()
            )
            mutation_idempotency = self._rows(
                conn.execute(
                    "SELECT operation_key,project_id,operation_kind,entity_id,entity_version,"
                    "input_fingerprint,created_at FROM product_project_mutation_idempotency "
                    "WHERE project_id=? ORDER BY operation_key",
                    (project_id,),
                ).fetchall()
            )
            audit = self._rows(
                conn.execute(
                    "SELECT event_type,entity_type,entity_id,payload_json,created_at "
                    "FROM audit_events WHERE entity_type='product_project' AND entity_id=? "
                    "ORDER BY event_id",
                    (project_id,),
                ).fetchall()
            )

        payload = {
            "schema": _ARCHIVE_SCHEMA,
            "project_id": project_id,
            "spec_version": spec_version,
            "row_version": row_version,
            "history": {
                "project": project,
                "specs": specs,
                "research_handoffs": research,
                "decisions": decisions,
                "creation_idempotency": creation_idempotency,
                "mutation_idempotency": mutation_idempotency,
                "audit_events": audit,
            },
        }
        digest = _sha256(payload)
        envelope = {"digest_sha256": digest, "payload": payload}
        archive_bytes = _canonical(envelope).encode("utf-8")
        summary = self._summary(payload, digest)
        return ProductProjectHistoryArchive(summary=summary, bytes=archive_bytes)

    def verify(self, archive_bytes: bytes) -> ProductProjectHistoryArchiveSummary:
        payload, digest = self._decode(archive_bytes)
        return self._summary(payload, digest)

    def verify_against_live(
        self,
        archive_bytes: bytes,
    ) -> ProductProjectHistoryArchiveSummary:
        archived = self.verify(archive_bytes)
        live = self.build(
            archived.project_id,
            expected_spec_version=archived.spec_version,
            expected_row_version=archived.row_version,
        )
        if live.summary.digest_sha256 != archived.digest_sha256:
            raise ProductProjectError(
                "ProductProject history archive differs from durable live history"
            )
        return archived

    @staticmethod
    def _require_exact_versions(row: Any, spec_version: int, row_version: int) -> None:
        live_spec_version = int(row["current_spec_version"])
        live_row_version = int(row["row_version"])
        if live_spec_version != spec_version:
            raise StaleProjectVersionError(
                f"stale ProductProject spec: expected {spec_version}, current {live_spec_version}"
            )
        if live_row_version != row_version:
            raise StaleProjectVersionError(
                f"stale ProductProject row: expected {row_version}, current {live_row_version}"
            )

    @classmethod
    def _rows(cls, rows: list[Any]) -> list[dict[str, Any]]:
        return [cls._row(row) for row in rows]

    @staticmethod
    def _row(row: Any) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key in row:
            value = row[key]
            mapped = _JSON_COLUMNS.get(key)
            if mapped is None:
                result[key] = value
                continue
            try:
                parsed = json.loads(value)
            except (TypeError, json.JSONDecodeError) as exc:
                raise ProductProjectError(f"invalid durable JSON column: {key}") from exc
            result[mapped] = parsed
        return result

    @staticmethod
    def _decode(archive_bytes: bytes) -> tuple[dict[str, Any], str]:
        try:
            envelope = json.loads(archive_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProductProjectError("invalid ProductProject history archive encoding") from exc
        if not isinstance(envelope, dict):
            raise ProductProjectError("invalid ProductProject history archive envelope")
        payload = envelope.get("payload")
        digest = envelope.get("digest_sha256")
        if not isinstance(payload, dict) or not isinstance(digest, str) or not digest.strip():
            raise ProductProjectError("incomplete ProductProject history archive envelope")
        if payload.get("schema") != _ARCHIVE_SCHEMA:
            raise ProductProjectError("unsupported ProductProject history archive schema")
        if _sha256(payload) != digest:
            raise ProductProjectError("ProductProject history archive digest mismatch")
        return payload, digest

    @staticmethod
    def _summary(
        payload: dict[str, Any],
        digest: str,
    ) -> ProductProjectHistoryArchiveSummary:
        history = payload.get("history")
        if not isinstance(history, dict):
            raise ProductProjectError("ProductProject history archive has no history object")
        required_lists = (
            "specs",
            "research_handoffs",
            "decisions",
            "creation_idempotency",
            "mutation_idempotency",
            "audit_events",
        )
        for key in required_lists:
            if not isinstance(history.get(key), list):
                raise ProductProjectError(f"ProductProject history archive has invalid {key}")
        project = history.get("project")
        if not isinstance(project, dict):
            raise ProductProjectError("ProductProject history archive has invalid project row")
        project_id = payload.get("project_id")
        if not isinstance(project_id, str) or not project_id.strip():
            raise ProductProjectError("ProductProject history archive has invalid project_id")
        try:
            spec_version = int(payload["spec_version"])
            row_version = int(payload["row_version"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ProductProjectError(
                "ProductProject history archive has invalid versions"
            ) from exc
        if spec_version < 1 or row_version < 0:
            raise ProductProjectError("ProductProject history archive has invalid versions")
        if project.get("project_id") != project_id:
            raise ProductProjectError("ProductProject history archive project identity mismatch")
        if int(project.get("current_spec_version", 0)) != spec_version:
            raise ProductProjectError("ProductProject history archive spec identity mismatch")
        if int(project.get("row_version", -1)) != row_version:
            raise ProductProjectError("ProductProject history archive row identity mismatch")
        if len(history["specs"]) != spec_version:
            raise ProductProjectError("ProductProject history archive is missing spec revisions")
        return ProductProjectHistoryArchiveSummary(
            project_id=project_id,
            spec_version=spec_version,
            row_version=row_version,
            spec_count=len(history["specs"]),
            research_package_count=len(history["research_handoffs"]),
            decision_version_count=len(history["decisions"]),
            audit_event_count=len(history["audit_events"]),
            mutation_idempotency_count=len(history["mutation_idempotency"]),
            digest_sha256=digest,
        )
