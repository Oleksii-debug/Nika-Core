from __future__ import annotations

import hashlib
from typing import Any

from nika_core import _research_product_handoff_base as _base
from nika_core.product_project import ProductProjectError
from nika_core.research.models import FreshnessState, SourceKind

_SEAL_SCHEMA_VERSION = 2

ResearchProductHandoffRecord = _base.ResearchProductHandoffRecord


def _snapshot_id(source_id: str, raw_sha256: str) -> str:
    return hashlib.sha256(f"{source_id}\0{raw_sha256}".encode()).hexdigest()


def _source_content_bindings_conn(
    conn: Any,
    research_payload: dict[str, Any],
) -> list[dict[str, Any]]:
    workspace_id = _base._required_text(
        research_payload.get("workspace_id"),
        label="sealed research result workspace_id",
    )
    bindings: list[dict[str, Any]] = []
    items = research_payload.get("items")
    if not isinstance(items, list):
        raise ProductProjectError("sealed research result items must be a list")

    for item in items:
        if not isinstance(item, dict):
            raise ProductProjectError("sealed research result contains invalid item data")
        ordinal = item.get("ordinal")
        if type(ordinal) is not int or ordinal < 0:
            raise ProductProjectError("sealed research result has invalid item ordinal")
        document_id = _base._required_text(
            item.get("document_id"),
            label="sealed research result document_id",
        )
        evidence_values = item.get("evidence")
        if not isinstance(evidence_values, list):
            raise ProductProjectError("sealed research result evidence must be a list")

        for evidence_index, evidence in enumerate(evidence_values):
            if not isinstance(evidence, dict):
                raise ProductProjectError("sealed research result contains invalid evidence")
            if evidence.get("source_kind") != SourceKind.HTTP.value:
                continue
            source_id = _base._required_text(
                evidence.get("source_id"),
                label="remote research evidence source_id",
            )
            locator = _base._required_text(
                evidence.get("locator"),
                label="remote research evidence locator",
            )
            observed_at = _base._required_text(
                evidence.get("observed_at"),
                label="remote research evidence observed_at",
            )
            if evidence.get("freshness") != FreshnessState.CURRENT.value:
                raise ProductProjectError(
                    f"remote research evidence is stale at handoff: {source_id}"
                )

            rows = conn.execute(
                "SELECT o.snapshot_id,s.raw_sha256,s.artifact_id,"
                "a.raw_sha256 AS artifact_raw_sha256,h.workspace_id,"
                "h.current_raw_sha256,h.freshness "
                "FROM corpus_http_origins o "
                "JOIN research_http_snapshots s ON s.snapshot_id=o.snapshot_id "
                "JOIN corpus_artifacts a ON a.artifact_id=s.artifact_id "
                "JOIN research_http_sources h ON h.source_id=o.source_id "
                "WHERE o.document_id=? AND o.source_id=? "
                "AND o.locator=? AND o.observed_at=? ORDER BY o.snapshot_id",
                (document_id, source_id, locator, observed_at),
            ).fetchall()
            if len(rows) != 1:
                raise ProductProjectError(
                    f"remote research evidence snapshot binding is not exact: {source_id}"
                )
            row = rows[0]
            snapshot_id = _base._required_text(
                row["snapshot_id"],
                label="remote research snapshot_id",
            )
            raw_sha256 = _base._required_text(
                row["raw_sha256"],
                label="remote research snapshot raw_sha256",
            )
            artifact_raw_sha256 = _base._required_text(
                row["artifact_raw_sha256"],
                label="remote research artifact raw_sha256",
            )
            if snapshot_id != _snapshot_id(source_id, raw_sha256):
                raise ProductProjectError(
                    f"remote research snapshot identity mismatch: {source_id}"
                )
            if artifact_raw_sha256 != raw_sha256:
                raise ProductProjectError(
                    f"remote research snapshot artifact mismatch: {source_id}"
                )
            if row["workspace_id"] != workspace_id:
                raise ProductProjectError(
                    f"remote research source crosses workspace boundary: {source_id}"
                )
            if row["freshness"] != FreshnessState.CURRENT.value:
                raise ProductProjectError(
                    f"remote research source is not current: {source_id}"
                )
            current_raw_sha256 = _base._required_text(
                row["current_raw_sha256"],
                label="remote research source current_raw_sha256",
            )
            if current_raw_sha256 != raw_sha256:
                raise ProductProjectError(
                    f"remote research source content changed after result capture: {source_id}"
                )

            bindings.append(
                {
                    "item_ordinal": ordinal,
                    "evidence_index": evidence_index,
                    "document_id": document_id,
                    "source_id": source_id,
                    "snapshot_id": snapshot_id,
                    "raw_sha256": raw_sha256,
                }
            )
    return bindings


def _formal_authority_fingerprint_v2(
    *,
    project_id: str,
    package_id: str,
    result_set_id: str,
    workspace_id: str,
    result_set_sha256: str,
    handoff_payload_sha256: str,
    source_content_sha256: str,
) -> str:
    return _base._sha256_json(
        {
            "formal_authority_version": _SEAL_SCHEMA_VERSION,
            "project_id": project_id,
            "package_id": package_id,
            "result_set_id": result_set_id,
            "workspace_id": workspace_id,
            "result_set_sha256": result_set_sha256,
            "handoff_payload_sha256": handoff_payload_sha256,
            "source_content_sha256": source_content_sha256,
        }
    )


def _seal_identity(
    seal: dict[str, Any],
    *,
    package_id: str,
) -> tuple[str, str, str, str, str, list[dict[str, Any]]]:
    if seal.get("seal_version") != _SEAL_SCHEMA_VERSION:
        raise ProductProjectError(
            f"formal research handoff seal version is unsupported: {package_id}"
        )
    raw_bindings = seal.get("source_content_bindings")
    if not isinstance(raw_bindings, list) or any(
        not isinstance(binding, dict) for binding in raw_bindings
    ):
        raise ProductProjectError(
            f"formal research handoff source content binding is malformed: {package_id}"
        )
    bindings = [dict(binding) for binding in raw_bindings]
    source_content_sha256 = _base._required_text(
        seal.get("source_content_sha256"),
        label="integrity seal source_content_sha256",
    )
    if _base._sha256_json(bindings) != source_content_sha256:
        raise ProductProjectError(
            f"formal research handoff source content digest mismatch: {package_id}"
        )
    return (
        _base._required_text(
            seal.get("result_set_id"),
            label="integrity seal result_set_id",
        ),
        _base._required_text(
            seal.get("workspace_id"),
            label="integrity seal workspace_id",
        ),
        _base._required_text(
            seal.get("result_set_sha256"),
            label="integrity seal result_set_sha256",
        ),
        _base._required_text(
            seal.get("handoff_payload_sha256"),
            label="integrity seal handoff_payload_sha256",
        ),
        source_content_sha256,
        bindings,
    )


def verify_sealed_handoffs_conn(
    conn: Any,
    project_id: str,
    package_ids: tuple[str, ...],
) -> None:
    """Verify formal PF1 authority, exact result data and captured source bytes."""

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
        payload = _base._parse_object(
            row["payload_json"],
            label=f"research handoff {package_id}",
        )
        if payload.get("package_id") != package_id:
            raise ProductProjectError(
                f"research handoff package identity mismatch: {package_id}"
            )

        authority_rows = _base._formal_authority_rows_conn(conn, project_id, package_id)
        authority_present = _base._validate_formal_authority_rows(
            authority_rows,
            project_id=project_id,
            package_id=package_id,
        )
        seals = _base._seal_rows_conn(conn, project_id, package_id)
        if not seals:
            if authority_present or _base._is_formal_handoff_payload(payload):
                raise ProductProjectError(
                    f"formal research handoff integrity seal is missing: {package_id}"
                )
            # Genuine direct ProductProject handoffs predate the formal PF1 adapter.
            continue

        current_handoff_digest = _base._sha256_json(payload)
        expected_identity: tuple[str, str, str, str, str] | None = None
        expected_bindings: list[dict[str, Any]] | None = None
        for _, seal in seals:
            (
                result_set_id,
                workspace_id,
                result_set_sha256,
                handoff_payload_sha256,
                source_content_sha256,
                bindings,
            ) = _seal_identity(seal, package_id=package_id)
            identity = (
                result_set_id,
                workspace_id,
                result_set_sha256,
                handoff_payload_sha256,
                source_content_sha256,
            )
            if expected_identity is None:
                expected_identity = identity
                expected_bindings = bindings
            elif identity != expected_identity or bindings != expected_bindings:
                raise ProductProjectError(
                    f"conflicting research handoff integrity seals: {package_id}"
                )
            if handoff_payload_sha256 != current_handoff_digest:
                raise ProductProjectError(
                    f"research handoff integrity mismatch: {package_id}"
                )

        if expected_identity is None or expected_bindings is None:
            raise ProductProjectError(
                f"research handoff integrity seal is missing: {package_id}"
            )
        (
            result_set_id,
            workspace_id,
            result_set_sha256,
            handoff_payload_sha256,
            source_content_sha256,
        ) = expected_identity
        authority_fingerprint = _formal_authority_fingerprint_v2(
            project_id=project_id,
            package_id=package_id,
            result_set_id=result_set_id,
            workspace_id=workspace_id,
            result_set_sha256=result_set_sha256,
            handoff_payload_sha256=handoff_payload_sha256,
            source_content_sha256=source_content_sha256,
        )
        if not _base._validate_formal_authority_rows(
            authority_rows,
            project_id=project_id,
            package_id=package_id,
            expected_fingerprint=authority_fingerprint,
        ):
            raise ProductProjectError(
                f"formal research handoff authority is missing: {package_id}"
            )

        research_payload = _base._research_result_payload_conn(conn, result_set_id)
        if research_payload["workspace_id"] != workspace_id:
            raise ProductProjectError(
                f"research result workspace identity mismatch: {result_set_id}"
            )
        if _base._sha256_json(research_payload) != result_set_sha256:
            raise ProductProjectError(
                f"research result integrity mismatch: {result_set_id}"
            )
        current_bindings = _source_content_bindings_conn(conn, research_payload)
        if (
            _base._sha256_json(current_bindings) != source_content_sha256
            or current_bindings != expected_bindings
        ):
            raise ProductProjectError(
                f"research source content binding mismatch: {result_set_id}"
            )


class ResearchProductHandoffService(_base.ResearchProductHandoffService):
    """PF1 adapter with formal source-snapshot binding at every authority boundary."""

    def _source_binding_state(
        self,
        result_set_id: str,
    ) -> tuple[list[dict[str, Any]], str]:
        with self.store.connection() as conn:
            research_payload = _base._research_result_payload_conn(conn, result_set_id)
            bindings = _source_content_bindings_conn(conn, research_payload)
        return bindings, _base._sha256_json(bindings)

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
        _, source_content_sha256 = self._source_binding_state(result_set_id)
        operation_key = _base._formal_authority_key(project_id, package_id)
        fingerprint = _formal_authority_fingerprint_v2(
            project_id=project_id,
            package_id=package_id,
            result_set_id=result_set_id,
            workspace_id=workspace_id,
            result_set_sha256=result_set_sha256,
            handoff_payload_sha256=handoff_payload_sha256,
            source_content_sha256=source_content_sha256,
        )
        with self.store.connection() as conn:
            if not conn.execute(
                "SELECT 1 FROM product_projects WHERE project_id=?",
                (project_id,),
            ).fetchone():
                raise KeyError(project_id)
            rows = _base._formal_authority_rows_conn(conn, project_id, package_id)
            if rows:
                _base._validate_formal_authority_rows(
                    rows,
                    project_id=project_id,
                    package_id=package_id,
                    expected_fingerprint=fingerprint,
                )
                return
            conn.execute(
                "INSERT OR IGNORE INTO product_project_mutation_idempotency("
                "operation_key,project_id,operation_kind,entity_id,entity_version,"
                "input_fingerprint,created_at) VALUES (?,?,?,?,?,?,?)",
                (
                    operation_key,
                    project_id,
                    _base._FORMAL_AUTH_OPERATION_KIND,
                    package_id,
                    1,
                    fingerprint,
                    _base._now(),
                ),
            )
            rows = _base._formal_authority_rows_conn(conn, project_id, package_id)
            if _base._validate_formal_authority_rows(
                rows,
                project_id=project_id,
                package_id=package_id,
                expected_fingerprint=fingerprint,
            ):
                return
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
        bindings, source_content_sha256 = self._source_binding_state(result_set_id)
        expected = {
            "seal_version": _SEAL_SCHEMA_VERSION,
            "package_id": package_id,
            "result_set_id": result_set_id,
            "workspace_id": workspace_id,
            "result_set_sha256": result_set_sha256,
            "handoff_payload_sha256": handoff_payload_sha256,
            "source_content_sha256": source_content_sha256,
            "source_content_bindings": bindings,
            "option_ids": list(option_ids),
        }
        with self.store.connection() as conn:
            seals = _base._seal_rows_conn(conn, project_id, package_id)
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
                "event_type,entity_type,entity_id,payload_json,created_at) "
                "VALUES (?,?,?,?,?)",
                (
                    _base._SEAL_EVENT_TYPE,
                    _base._SEAL_ENTITY_TYPE,
                    project_id,
                    _base._canonical(expected),
                    _base._now(),
                ),
            )
            verify_sealed_handoffs_conn(conn, project_id, (package_id,))

    def _verify_option_packages(
        self,
        project_id: str,
        options: tuple[Any, ...],
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
            seals = _base._seal_rows_conn(conn, project_id, package_id)
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
                _base._required_text(value, label="integrity seal option id")
                for value in raw_option_ids
            )
            if not option_ids or len(set(option_ids)) != len(option_ids):
                raise ProductProjectError(
                    f"research handoff integrity seal has invalid option ids: {package_id}"
                )
            record = ResearchProductHandoffRecord(
                project_id=project_id,
                package_id=package_id,
                result_set_id=_base._required_text(
                    seal.get("result_set_id"),
                    label="integrity seal result_set_id",
                ),
                workspace_id=_base._required_text(
                    seal.get("workspace_id"),
                    label="integrity seal workspace_id",
                ),
                result_set_sha256=_base._required_text(
                    seal.get("result_set_sha256"),
                    label="integrity seal result_set_sha256",
                ),
                handoff_payload_sha256=_base._required_text(
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
        if _base._sha256_json(_base._result_set_payload(result_set)) != record.result_set_sha256:
            raise ProductProjectError(
                f"research result integrity mismatch: {record.result_set_id}"
            )
        return record
