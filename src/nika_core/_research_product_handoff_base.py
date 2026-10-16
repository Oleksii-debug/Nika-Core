from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from nika_core.product_project import (
    EvidenceRef,
    ProductOption,
    ProductProjectError,
    ProductProjectRepository,
    ResearchEvidencePackage,
)
from nika_core.research.models import FreshnessState, ResearchResultSet, SourceKind
from nika_core.research.network_repository import NetworkResearchRepository

_SEAL_EVENT_TYPE = "product_project.research_product_handoff_sealed"
_SEAL_ENTITY_TYPE = "product_project"
_FORMAL_REF_PREFIX = "research-result-set://"
_FORMAL_AUTH_OPERATION_KIND = "research_product_handoff.formal_authority"
_FORMAL_AUTH_KEY_PREFIX = "research-product-formal:"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _result_set_payload(result_set: ResearchResultSet) -> dict[str, Any]:
    return {
        "result_set_id": result_set.result_set_id,
        "workspace_id": result_set.workspace_id,
        "query": result_set.query,
        "created_at": result_set.created_at,
        "items": [
            {
                "ordinal": item.ordinal,
                "document_id": item.document_id,
                "title": item.title,
                "snippet": item.snippet,
                "rank": item.rank,
                "why_matched": item.why_matched,
                "evidence": [
                    {
                        "source_id": evidence.source_id,
                        "source_kind": evidence.source_kind.value,
                        "locator": evidence.locator,
                        "observed_at": evidence.observed_at,
                        "freshness": (
                            evidence.freshness.value
                            if evidence.freshness is not None
                            else None
                        ),
                    }
                    for evidence in item.evidence
                ],
            }
            for item in result_set.items
        ],
    }


def _handoff_payload(
    package: ResearchEvidencePackage,
    options: tuple[ProductOption, ...],
) -> dict[str, Any]:
    return {
        "package_id": package.package_id,
        "research_artifact_ref": package.research_artifact_ref,
        "evidence": [
            {
                "evidence_id": evidence.evidence_id,
                "provenance_ref": evidence.provenance_ref,
                "claim": evidence.claim,
            }
            for evidence in package.evidence
        ],
        "options": [
            {
                "option_id": option.option_id,
                "title": option.title,
                "summary": option.summary,
                "evidence_package_ids": list(option.evidence_package_ids),
            }
            for option in options
        ],
    }


def _required_text(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProductProjectError(f"{label} must be a non-empty string")
    return value


def _parse_object(raw: str, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ProductProjectError(f"{label} contains invalid JSON") from exc
    if not isinstance(value, dict):
        raise ProductProjectError(f"{label} must contain a JSON object")
    return value


def _is_formal_handoff_payload(payload: dict[str, Any]) -> bool:
    artifact_ref = payload.get("research_artifact_ref")
    if isinstance(artifact_ref, str) and artifact_ref.startswith(_FORMAL_REF_PREFIX):
        return True
    evidence = payload.get("evidence")
    if not isinstance(evidence, list):
        return False
    return any(
        isinstance(item, dict)
        and isinstance(item.get("provenance_ref"), str)
        and item["provenance_ref"].startswith(_FORMAL_REF_PREFIX)
        for item in evidence
    )


def _formal_authority_key(project_id: str, package_id: str) -> str:
    identity = {
        "project_id": project_id,
        "package_id": package_id,
    }
    return f"{_FORMAL_AUTH_KEY_PREFIX}{_sha256_json(identity)}"


def _formal_authority_fingerprint(
    *,
    project_id: str,
    package_id: str,
    result_set_id: str,
    workspace_id: str,
    result_set_sha256: str,
    handoff_payload_sha256: str,
) -> str:
    return _sha256_json(
        {
            "project_id": project_id,
            "package_id": package_id,
            "result_set_id": result_set_id,
            "workspace_id": workspace_id,
            "result_set_sha256": result_set_sha256,
            "handoff_payload_sha256": handoff_payload_sha256,
        }
    )


def _formal_authority_rows_conn(
    conn: Any,
    project_id: str,
    package_id: str,
) -> list[Any]:
    operation_key = _formal_authority_key(project_id, package_id)
    return conn.execute(
        "SELECT operation_key,project_id,operation_kind,entity_id,entity_version,"
        "input_fingerprint FROM product_project_mutation_idempotency "
        "WHERE operation_key=? OR "
        "(project_id=? AND operation_kind=? AND entity_id=?) ORDER BY operation_key",
        (operation_key, project_id, _FORMAL_AUTH_OPERATION_KIND, package_id),
    ).fetchall()


def _validate_formal_authority_rows(
    rows: list[Any],
    *,
    project_id: str,
    package_id: str,
    expected_fingerprint: str | None = None,
) -> bool:
    if not rows:
        return False
    if len(rows) != 1:
        raise ProductProjectError(
            f"conflicting formal research handoff authority: {package_id}"
        )
    row = rows[0]
    if (
        row["operation_key"] != _formal_authority_key(project_id, package_id)
        or row["project_id"] != project_id
        or row["operation_kind"] != _FORMAL_AUTH_OPERATION_KIND
        or row["entity_id"] != package_id
        or type(row["entity_version"]) is not int
        or row["entity_version"] != 1
    ):
        raise ProductProjectError(
            f"formal research handoff authority is malformed: {package_id}"
        )
    fingerprint = _required_text(
        row["input_fingerprint"],
        label="formal research handoff authority fingerprint",
    )
    if expected_fingerprint is not None and fingerprint != expected_fingerprint:
        raise ProductProjectError(
            f"formal research handoff authority mismatch: {package_id}"
        )
    return True


def _research_result_payload_conn(conn: Any, result_set_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT result_set_id,workspace_id,query,created_at "
        "FROM research_result_sets WHERE result_set_id=?",
        (result_set_id,),
    ).fetchone()
    if row is None:
        raise ProductProjectError(
            f"sealed research result set is missing: {result_set_id}"
        )
    item_rows = conn.execute(
        "SELECT ordinal,document_id,title,snippet,rank,why_matched,evidence_json "
        "FROM research_result_items WHERE result_set_id=? ORDER BY ordinal",
        (result_set_id,),
    ).fetchall()
    items: list[dict[str, Any]] = []
    for item_row in item_rows:
        try:
            evidence = json.loads(item_row["evidence_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise ProductProjectError(
                f"sealed research result contains invalid evidence JSON: {result_set_id}"
            ) from exc
        if not isinstance(evidence, list):
            raise ProductProjectError(
                f"sealed research result evidence must be a list: {result_set_id}"
            )
        normalized_evidence: list[dict[str, Any]] = []
        for value in evidence:
            if not isinstance(value, dict):
                raise ProductProjectError(
                    f"sealed research result contains invalid evidence: {result_set_id}"
                )
            normalized_evidence.append(
                {
                    "source_id": value.get("source_id"),
                    "source_kind": value.get("source_kind"),
                    "locator": value.get("locator"),
                    "observed_at": value.get("observed_at"),
                    "freshness": value.get("freshness"),
                }
            )
        items.append(
            {
                "ordinal": int(item_row["ordinal"]),
                "document_id": item_row["document_id"],
                "title": item_row["title"],
                "snippet": item_row["snippet"],
                "rank": float(item_row["rank"]),
                "why_matched": item_row["why_matched"],
                "evidence": normalized_evidence,
            }
        )
    return {
        "result_set_id": row["result_set_id"],
        "workspace_id": row["workspace_id"],
        "query": row["query"],
        "created_at": row["created_at"],
        "items": items,
    }


def _verify_live_http_sources_conn(
    conn: Any,
    research_payload: dict[str, Any],
) -> None:
    for item in research_payload.get("items", ()):
        if not isinstance(item, dict):
            raise ProductProjectError("sealed research result contains invalid item data")
        evidence_values = item.get("evidence")
        if not isinstance(evidence_values, list):
            raise ProductProjectError("sealed research result evidence must be a list")
        for evidence in evidence_values:
            if not isinstance(evidence, dict):
                raise ProductProjectError("sealed research result contains invalid evidence")
            source_kind = evidence.get("source_kind")
            if source_kind != SourceKind.HTTP.value:
                continue
            source_id = _required_text(
                evidence.get("source_id"),
                label="remote research evidence source_id",
            )
            if evidence.get("freshness") != FreshnessState.CURRENT.value:
                raise ProductProjectError(
                    f"remote research evidence is stale at handoff: {source_id}"
                )
            source = conn.execute(
                "SELECT freshness FROM research_http_sources WHERE source_id=?",
                (source_id,),
            ).fetchone()
            if source is None or source["freshness"] != FreshnessState.CURRENT.value:
                raise ProductProjectError(
                    f"remote research source is not current: {source_id}"
                )


def _seal_rows_conn(
    conn: Any,
    project_id: str,
    package_id: str,
) -> list[tuple[Any, dict[str, Any]]]:
    rows = conn.execute(
        "SELECT event_id,payload_json,created_at FROM audit_events "
        "WHERE event_type=? AND entity_type=? AND entity_id=? ORDER BY event_id",
        (_SEAL_EVENT_TYPE, _SEAL_ENTITY_TYPE, project_id),
    ).fetchall()
    matches: list[tuple[Any, dict[str, Any]]] = []
    for row in rows:
        payload = _parse_object(
            row["payload_json"],
            label="research-to-product integrity seal",
        )
        if payload.get("package_id") == package_id:
            matches.append((row, payload))
    return matches


def verify_sealed_handoffs_conn(
    conn: Any,
    project_id: str,
    package_ids: tuple[str, ...],
) -> None:
    """Verify formal PF1 seals when present while retaining legacy handoff compatibility."""

    for package_id in package_ids:
        row = conn.execute(
            "SELECT payload_json FROM product_research_handoffs "
            "WHERE project_id=? AND package_id=?",
            (project_id, package_id),
        ).fetchone()
        if row is None:
            raise ProductProjectError(
                f"product decision references unknown evidence package: {package_id}"
            )
        payload = _parse_object(
            row["payload_json"],
            label=f"research handoff {package_id}",
        )
        if payload.get("package_id") != package_id:
            raise ProductProjectError(
                f"research handoff package identity mismatch: {package_id}"
            )
        authority_rows = _formal_authority_rows_conn(conn, project_id, package_id)
        authority_present = _validate_formal_authority_rows(
            authority_rows,
            project_id=project_id,
            package_id=package_id,
        )
        seals = _seal_rows_conn(conn, project_id, package_id)
        if not seals:
            if authority_present or _is_formal_handoff_payload(payload):
                raise ProductProjectError(
                    f"formal research handoff integrity seal is missing: {package_id}"
                )
            # Direct ProductProjectRepository handoffs predate the formal PF1 adapter.
            continue
        current_digest = _sha256_json(payload)
        expected_seal: tuple[str, str, str, str] | None = None
        for _, seal in seals:
            seal_identity = (
                _required_text(
                    seal.get("result_set_id"),
                    label="integrity seal result_set_id",
                ),
                _required_text(
                    seal.get("workspace_id"),
                    label="integrity seal workspace_id",
                ),
                _required_text(
                    seal.get("result_set_sha256"),
                    label="integrity seal result_set_sha256",
                ),
                _required_text(
                    seal.get("handoff_payload_sha256"),
                    label="integrity seal handoff_payload_sha256",
                ),
            )
            if expected_seal is None:
                expected_seal = seal_identity
            elif seal_identity != expected_seal:
                raise ProductProjectError(
                    f"conflicting research handoff integrity seals: {package_id}"
                )
            if seal_identity[3] != current_digest:
                raise ProductProjectError(
                    f"research handoff integrity mismatch: {package_id}"
                )

        if expected_seal is None:
            raise ProductProjectError(
                f"research handoff integrity seal is missing: {package_id}"
            )
        result_set_id, workspace_id, result_set_sha256, handoff_payload_sha256 = (
            expected_seal
        )
        authority_fingerprint = _formal_authority_fingerprint(
            project_id=project_id,
            package_id=package_id,
            result_set_id=result_set_id,
            workspace_id=workspace_id,
            result_set_sha256=result_set_sha256,
            handoff_payload_sha256=handoff_payload_sha256,
        )
        if not _validate_formal_authority_rows(
            authority_rows,
            project_id=project_id,
            package_id=package_id,
            expected_fingerprint=authority_fingerprint,
        ):
            raise ProductProjectError(
                f"formal research handoff authority is missing: {package_id}"
            )
        research_payload = _research_result_payload_conn(conn, result_set_id)
        if research_payload["workspace_id"] != workspace_id:
            raise ProductProjectError(
                f"research result workspace identity mismatch: {result_set_id}"
            )
        if _sha256_json(research_payload) != result_set_sha256:
            raise ProductProjectError(
                f"research result integrity mismatch: {result_set_id}"
            )
        _verify_live_http_sources_conn(conn, research_payload)


@dataclass(frozen=True, slots=True)
class ResearchProductHandoffRecord:
    project_id: str
    package_id: str
    result_set_id: str
    workspace_id: str
    result_set_sha256: str
    handoff_payload_sha256: str
    option_ids: tuple[str, ...]
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "package_id": self.package_id,
            "result_set_id": self.result_set_id,
            "workspace_id": self.workspace_id,
            "result_set_sha256": self.result_set_sha256,
            "handoff_payload_sha256": self.handoff_payload_sha256,
            "option_ids": list(self.option_ids),
            "created_at": self.created_at,
        }


class ResearchProductHandoffService:
    """Formal PF1 adapter from durable Universal Research results into ProductProject."""

    def __init__(
        self,
        *,
        store: Any,
        network_repository: NetworkResearchRepository,
    ) -> None:
        self.store = store
        self.network = network_repository
        self.projects = ProductProjectRepository(store)

    def handoff(
        self,
        *,
        project_id: str,
        result_set_id: str,
        package_id: str,
        options: tuple[ProductOption, ...],
    ) -> ResearchProductHandoffRecord:
        if not project_id.strip() or not result_set_id.strip() or not package_id.strip():
            raise ProductProjectError(
                "project_id, result_set_id and package_id are required"
            )
        if not options:
            raise ProductProjectError(
                "research-to-product handoff requires explicit product options"
            )

        result_set = self.network.get_result_set(result_set_id)
        self._validate_result_set(result_set)
        with self.store.connection() as conn:
            _verify_live_http_sources_conn(conn, _result_set_payload(result_set))
        self._validate_options(options, package_id)
        package = self._package_from_result_set(result_set, package_id)
        expected_payload = _handoff_payload(package, options)
        result_set_sha256 = _sha256_json(_result_set_payload(result_set))
        handoff_payload_sha256 = _sha256_json(expected_payload)
        option_ids = tuple(option.option_id for option in options)

        existing = self._read_handoff_payload(project_id, package_id)
        if existing is not None and existing != expected_payload:
            raise ProductProjectError(
                "evidence package id already exists with different handoff payload"
            )

        self._ensure_formal_authority(
            project_id=project_id,
            package_id=package_id,
            result_set_id=result_set.result_set_id,
            workspace_id=result_set.workspace_id,
            result_set_sha256=result_set_sha256,
            handoff_payload_sha256=handoff_payload_sha256,
        )
        if existing is None:
            try:
                self.projects.record_research_handoff(project_id, package, options)
            except sqlite3.IntegrityError:
                # Concurrent identical handoffs converge; conflicting payloads fail closed.
                existing = self._read_handoff_payload(project_id, package_id)
                if existing != expected_payload:
                    raise ProductProjectError(
                        "concurrent research handoff used the evidence package id "
                        "with different payload"
                    ) from None

        self._ensure_seal(
            project_id=project_id,
            package_id=package_id,
            result_set_id=result_set.result_set_id,
            workspace_id=result_set.workspace_id,
            result_set_sha256=result_set_sha256,
            handoff_payload_sha256=handoff_payload_sha256,
            option_ids=option_ids,
        )
        self._verify_option_packages(project_id, options)
        return self.get(project_id, package_id)

    def get(self, project_id: str, package_id: str) -> ResearchProductHandoffRecord:
        with self.store.connection() as conn:
            row = conn.execute(
                "SELECT payload_json FROM product_research_handoffs "
                "WHERE project_id=? AND package_id=?",
                (project_id, package_id),
            ).fetchone()
            if row is None:
                raise KeyError(package_id)
            verify_sealed_handoffs_conn(conn, project_id, (package_id,))
            seals = _seal_rows_conn(conn, project_id, package_id)
            if not seals:
                raise ProductProjectError(
                    f"research-to-product handoff is not formally sealed: {package_id}"
                )
            seal_row, seal = seals[0]
            raw_option_ids = seal.get("option_ids")
            if not isinstance(raw_option_ids, list):
                raise ProductProjectError(
                    f"research handoff integrity seal has invalid option ids: {package_id}"
                )
            option_ids = tuple(
                _required_text(value, label="integrity seal option id")
                for value in raw_option_ids
            )
            if not option_ids or len(set(option_ids)) != len(option_ids):
                raise ProductProjectError(
                    f"research handoff integrity seal has invalid option ids: {package_id}"
                )
            record = ResearchProductHandoffRecord(
                project_id=project_id,
                package_id=package_id,
                result_set_id=_required_text(
                    seal.get("result_set_id"),
                    label="integrity seal result_set_id",
                ),
                workspace_id=_required_text(
                    seal.get("workspace_id"),
                    label="integrity seal workspace_id",
                ),
                result_set_sha256=_required_text(
                    seal.get("result_set_sha256"),
                    label="integrity seal result_set_sha256",
                ),
                handoff_payload_sha256=_required_text(
                    seal.get("handoff_payload_sha256"),
                    label="integrity seal handoff_payload_sha256",
                ),
                option_ids=option_ids,
                created_at=str(seal_row["created_at"]),
            )

        result_set = self.network.get_result_set(record.result_set_id)
        if result_set.workspace_id != record.workspace_id:
            raise ProductProjectError(
                f"research result workspace identity mismatch: {record.result_set_id}"
            )
        self._validate_result_set(result_set)
        current_result_set_sha256 = _sha256_json(_result_set_payload(result_set))
        if current_result_set_sha256 != record.result_set_sha256:
            raise ProductProjectError(
                f"research result integrity mismatch: {record.result_set_id}"
            )
        return record

    def _read_handoff_payload(
        self,
        project_id: str,
        package_id: str,
    ) -> dict[str, Any] | None:
        with self.store.connection() as conn:
            row = conn.execute(
                "SELECT payload_json FROM product_research_handoffs "
                "WHERE project_id=? AND package_id=?",
                (project_id, package_id),
            ).fetchone()
        if row is None:
            return None
        payload = _parse_object(
            row["payload_json"],
            label=f"research handoff {package_id}",
        )
        if payload.get("package_id") != package_id:
            raise ProductProjectError(
                f"research handoff package identity mismatch: {package_id}"
            )
        return payload

    def _ensure_formal_authority(
        self,
        *,
        project_id: str,
        package_id: str,
        result_set_id: str,
        workspace_id: str,
        result_set_sha256: str,
        handoff_payload_sha256: str,
    ) -> None:
        operation_key = _formal_authority_key(project_id, package_id)
        fingerprint = _formal_authority_fingerprint(
            project_id=project_id,
            package_id=package_id,
            result_set_id=result_set_id,
            workspace_id=workspace_id,
            result_set_sha256=result_set_sha256,
            handoff_payload_sha256=handoff_payload_sha256,
        )
        with self.store.connection() as conn:
            if not conn.execute(
                "SELECT 1 FROM product_projects WHERE project_id=?",
                (project_id,),
            ).fetchone():
                raise KeyError(project_id)

            rows = _formal_authority_rows_conn(conn, project_id, package_id)
            if rows:
                _validate_formal_authority_rows(
                    rows,
                    project_id=project_id,
                    package_id=package_id,
                    expected_fingerprint=fingerprint,
                )
                return

            conn.execute(
                "INSERT OR IGNORE INTO product_project_mutation_idempotency("
                "operation_key,project_id,operation_kind,entity_id,entity_version,"
                "input_fingerprint,created_at"
                ") VALUES (?,?,?,?,?,?,?)",
                (
                    operation_key,
                    project_id,
                    _FORMAL_AUTH_OPERATION_KIND,
                    package_id,
                    1,
                    fingerprint,
                    _now(),
                ),
            )
            rows = _formal_authority_rows_conn(conn, project_id, package_id)
            if _validate_formal_authority_rows(
                rows,
                project_id=project_id,
                package_id=package_id,
                expected_fingerprint=fingerprint,
            ):
                return

            collision = conn.execute(
                "SELECT project_id,operation_kind,entity_id,entity_version,"
                "input_fingerprint FROM product_project_mutation_idempotency "
                "WHERE operation_key=?",
                (operation_key,),
            ).fetchone()
            if collision is not None:
                raise ProductProjectError(
                    f"formal research handoff authority key conflict: {package_id}"
                )
            raise ProductProjectError(
                f"formal research handoff authority was not persisted: {package_id}"
            )

    def _ensure_seal(
        self,
        *,
        project_id: str,
        package_id: str,
        result_set_id: str,
        workspace_id: str,
        result_set_sha256: str,
        handoff_payload_sha256: str,
        option_ids: tuple[str, ...],
    ) -> None:
        expected = {
            "package_id": package_id,
            "result_set_id": result_set_id,
            "workspace_id": workspace_id,
            "result_set_sha256": result_set_sha256,
            "handoff_payload_sha256": handoff_payload_sha256,
            "option_ids": list(option_ids),
        }
        with self.store.connection() as conn:
            seals = _seal_rows_conn(conn, project_id, package_id)
            if seals:
                verify_sealed_handoffs_conn(conn, project_id, (package_id,))
                for _, seal in seals:
                    if seal != expected:
                        raise ProductProjectError(
                            f"conflicting research handoff integrity seal: {package_id}"
                        )
                return
            conn.execute(
                "INSERT INTO audit_events("
                "event_type,entity_type,entity_id,payload_json,created_at"
                ") VALUES (?,?,?,?,?)",
                (
                    _SEAL_EVENT_TYPE,
                    _SEAL_ENTITY_TYPE,
                    project_id,
                    _canonical(expected),
                    _now(),
                ),
            )
            verify_sealed_handoffs_conn(conn, project_id, (package_id,))

    def _verify_option_packages(
        self,
        project_id: str,
        options: tuple[ProductOption, ...],
    ) -> None:
        package_ids = tuple(
            dict.fromkeys(
                package_id
                for option in options
                for package_id in option.evidence_package_ids
            )
        )
        with self.store.connection() as conn:
            for package_id in package_ids:
                if not conn.execute(
                    "SELECT 1 FROM product_research_handoffs "
                    "WHERE project_id=? AND package_id=?",
                    (project_id, package_id),
                ).fetchone():
                    raise ProductProjectError(
                        f"product option references unknown evidence package: {package_id}"
                    )
            verify_sealed_handoffs_conn(conn, project_id, package_ids)

    @staticmethod
    def _validate_options(
        options: tuple[ProductOption, ...],
        package_id: str,
    ) -> None:
        option_ids = tuple(option.option_id for option in options)
        if any(not option_id.strip() for option_id in option_ids):
            raise ProductProjectError("product option id is required")
        if len(set(option_ids)) != len(option_ids):
            raise ProductProjectError("research handoff option ids must be unique")
        for option in options:
            package_ids = option.evidence_package_ids
            if package_id not in package_ids:
                raise ProductProjectError(
                    f"product option {option.option_id} must reference supplied evidence package"
                )
            if len(set(package_ids)) != len(package_ids):
                raise ProductProjectError(
                    f"product option {option.option_id} has duplicate evidence package ids"
                )

    @staticmethod
    def _validate_result_set(result_set: ResearchResultSet) -> None:
        if (
            not result_set.result_set_id.strip()
            or not result_set.workspace_id.strip()
            or not result_set.query.strip()
            or not result_set.created_at.strip()
        ):
            raise ProductProjectError(
                "research result set requires stable identity, workspace, query and timestamp"
            )
        if not result_set.items:
            raise ProductProjectError(
                "research-to-product handoff requires non-empty research evidence"
            )
        ordinals = tuple(item.ordinal for item in result_set.items)
        if ordinals != tuple(range(len(result_set.items))):
            raise ProductProjectError(
                "research result item ordinals must be contiguous and deterministic"
            )
        for item in result_set.items:
            if not item.document_id.strip() or not item.why_matched.strip():
                raise ProductProjectError(
                    "research result item requires document identity and match provenance"
                )
            if not item.evidence:
                raise ProductProjectError(
                    f"research result item {item.ordinal} has no provenance evidence"
                )
            for evidence in item.evidence:
                if (
                    not evidence.source_id.strip()
                    or not evidence.locator.strip()
                    or not evidence.observed_at.strip()
                ):
                    raise ProductProjectError(
                        f"research result item {item.ordinal} has incomplete provenance"
                    )
                if evidence.source_kind is SourceKind.HTTP:
                    if evidence.freshness is not FreshnessState.CURRENT:
                        raise ProductProjectError(
                            "remote research evidence must be current before product handoff"
                        )
                elif evidence.source_kind is SourceKind.LOCAL_FILE:
                    if evidence.freshness is not None:
                        raise ProductProjectError(
                            "local research evidence must not carry remote freshness state"
                        )
                else:
                    raise ProductProjectError(
                        f"unsupported research evidence source kind: {evidence.source_kind}"
                    )

    @staticmethod
    def _package_from_result_set(
        result_set: ResearchResultSet,
        package_id: str,
    ) -> ResearchEvidencePackage:
        evidence_refs = tuple(
            EvidenceRef(
                evidence_id=f"{result_set.result_set_id}:{item.ordinal}:{evidence_index}",
                provenance_ref=(
                    "research-result-set://"
                    f"{result_set.workspace_id}/{result_set.result_set_id}/"
                    f"items/{item.ordinal}/evidence/{evidence_index}"
                ),
            )
            for item in result_set.items
            for evidence_index, _ in enumerate(item.evidence)
        )
        return ResearchEvidencePackage(
            package_id=package_id,
            evidence=evidence_refs,
            research_artifact_ref=(
                f"research-result-set://{result_set.workspace_id}/{result_set.result_set_id}"
            ),
        )
