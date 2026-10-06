from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import unicodedata
from contextlib import nullcontext
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from nika_core.product_project import (
    ProductDecision,
    ProductDecisionState,
    ProductProject,
    ProductProjectError,
    ProductProjectRepository,
    StaleProjectVersionError,
)
from nika_core.research_product_handoff import verify_sealed_handoffs_conn
from nika_core.security import ActionIntent, ApprovalEvidence, ApprovalVerifier
from nika_core.tools import ToolRisk

_MAX_STORED_HANDOFF_BYTES = 1024 * 1024
_SQLITE_INTEGER_MAX = (1 << 63) - 1
_MAX_DECISION_ID_CHARS = 160


def _reject_nonfinite_evidence(_value: str) -> None:
    raise ValueError("non-finite stored research evidence")


def _finite_evidence_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("non-finite stored research evidence")
    return parsed


def _unique_evidence_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate stored research evidence key")
        value[key] = item
    return value


def _valid_stored_text(value: object) -> bool:
    if type(value) is not str or not value.strip():
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _decode_stored_utf8(value: object) -> str:
    if type(value) is not bytes:
        raise ProductProjectError("stored research handoff is malformed")
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProductProjectError("stored research handoff is malformed") from exc


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _approval_time(value: object) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if type(value) is not datetime:
        raise ValueError("product decision approval time must be an exact datetime")
    if value.tzinfo is None:
        raise ValueError("product decision approval time must be timezone-aware")
    return value


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _is_lock_contention(exc: sqlite3.OperationalError) -> bool:
    error_code = getattr(exc, "sqlite_errorcode", None)
    return isinstance(error_code, int) and (error_code & 0xFF) in {
        sqlite3.SQLITE_BUSY,
        sqlite3.SQLITE_LOCKED,
    }


def _strict_int(value: Any, *, label: str, minimum: int) -> int:
    if (
        type(value) is not int
        or value < minimum
        or value > _SQLITE_INTEGER_MAX
    ):
        raise ProductProjectError(
            f"{label} must be an exact integer in range "
            f"{minimum}..{_SQLITE_INTEGER_MAX}"
        )
    return value


def _validated_text(value: object, *, label: str) -> str:
    if type(value) is not str or not value.strip():
        raise ProductProjectError(f"{label} must be non-empty text")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ProductProjectError(f"{label} must be valid UTF-8 text") from exc
    return value


def _validated_decision_id(value: object) -> str:
    decision_id = _validated_text(value, label="product decision decision_id")
    if (
        len(decision_id) > _MAX_DECISION_ID_CHARS
        or decision_id != decision_id.strip()
        or any(
            unicodedata.category(character) in {"Cc", "Cf", "Zl", "Zp"}
            for character in decision_id
        )
    ):
        raise ProductProjectError(
            "product decision decision_id must be a safe presentation identity "
            f"of at most {_MAX_DECISION_ID_CHARS} characters"
        )
    return decision_id


def _snapshot_decision(decision: object) -> ProductDecision:
    if type(decision) is not ProductDecision:
        raise ProductProjectError("product decision must be an exact ProductDecision")
    try:
        decision_id = decision.decision_id
        option_id = decision.option_id
        state = decision.state
        rationale = decision.rationale
        decided_by_ref = decision.decided_by_ref
    except AttributeError as exc:
        raise ProductProjectError("product decision is incomplete") from exc
    if type(state) is not ProductDecisionState:
        raise ProductProjectError("product decision state must be ProductDecisionState")
    return ProductDecision(
        decision_id=_validated_decision_id(decision_id),
        option_id=_validated_text(
            option_id,
            label="product decision option_id",
        ),
        state=state,
        rationale=_validated_text(
            rationale,
            label="product decision rationale",
        ),
        decided_by_ref=_validated_text(
            decided_by_ref,
            label="product decision decided_by_ref",
        ),
    )


def _snapshot_approval(approval: object) -> ApprovalEvidence:
    if type(approval) is not ApprovalEvidence:
        raise PermissionError(
            "trusted product-owner approval evidence must be an exact ApprovalEvidence"
        )
    try:
        text_fields: dict[str, str] = {}
        for field_name in (
            "approval_id",
            "request_id",
            "issuer_id",
            "authority_version",
            "action_fingerprint",
            "effect_fingerprint",
            "signature",
        ):
            value = _validated_text(
                getattr(approval, field_name),
                label=f"approval evidence {field_name}",
            )
            if value != value.strip():
                raise ProductProjectError(
                    f"approval evidence {field_name} must not contain surrounding whitespace"
                )
            text_fields[field_name] = value
        approved_at = approval.approved_at
        expires_at = approval.expires_at
        if type(approved_at) is not datetime or type(expires_at) is not datetime:
            raise ProductProjectError(
                "approval evidence timestamps must be exact datetime values"
            )
        return ApprovalEvidence(
            approval_id=text_fields["approval_id"],
            request_id=text_fields["request_id"],
            issuer_id=text_fields["issuer_id"],
            authority_version=text_fields["authority_version"],
            action_fingerprint=text_fields["action_fingerprint"],
            effect_fingerprint=text_fields["effect_fingerprint"],
            approved_at=approved_at,
            expires_at=expires_at,
            signature=text_fields["signature"],
        )
    except (AttributeError, ProductProjectError, ValueError) as exc:
        raise PermissionError(
            "trusted product-owner approval evidence is malformed"
        ) from exc


def _decode_id_list(raw: Any, *, label: str) -> tuple[str, ...]:
    raw_text = _validated_text(raw, label=f"{label} JSON")
    try:
        values = json.loads(raw_text)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ProductProjectError(f"{label} contains invalid JSON") from exc
    if type(values) is not list or not values:
        raise ProductProjectError(f"{label} must be a non-empty list")
    if any(type(value) is not str or not value.strip() for value in values):
        raise ProductProjectError(
            f"{label} must contain non-empty string identifiers"
        )
    items = tuple(
        _validated_text(value, label=f"{label} item")
        for value in values
    )
    if len(items) != len(set(items)):
        raise ProductProjectError(f"{label} must not contain duplicates")
    return items


def _decision_fingerprint(project_id: str, decision: ProductDecision) -> str:
    return hashlib.sha256(
        _canonical(
            {
                "project_id": project_id,
                "decision_id": decision.decision_id,
                "option_id": decision.option_id,
                "state": decision.state.value,
                "rationale": decision.rationale,
                "decided_by_ref": (
                    None
                    if decision.state is ProductDecisionState.APPROVED
                    else decision.decided_by_ref
                ),
            }
        ).encode()
    ).hexdigest()


def _trusted_decided_by_ref(approval: ApprovalEvidence) -> str:
    digest = hashlib.sha256(
        _canonical(
            {
                "approval_id": approval.approval_id,
                "issuer_id": approval.issuer_id,
                "authority_version": approval.authority_version,
            }
        ).encode()
    ).hexdigest()
    return f"approval://{digest}"


@dataclass(frozen=True, slots=True)
class StoredProductDecision:
    project_id: str
    decision: ProductDecision
    decision_version: int
    evidence_package_ids: tuple[str, ...]
    created_at: str


class ProductDecisionRepository:
    """Durable PF1 decision lifecycle over the canonical ProductProject SQLite store."""

    def __init__(
        self,
        store: Any,
        *,
        approval_verifier: ApprovalVerifier | None = None,
    ) -> None:
        self.store = store
        self.projects = ProductProjectRepository(store)
        self.approval_verifier = approval_verifier

    def approval_intent(
        self,
        project_id: str,
        decision: ProductDecision,
        *,
        expected_row_version: int,
        idempotency_key: str,
    ) -> ActionIntent:
        """Build the exact trusted-host intent required for an APPROVED decision."""
        project_id = _validated_text(project_id, label="project_id")
        idempotency_key = _validated_text(idempotency_key, label="idempotency_key")
        decision = _snapshot_decision(decision)
        if decision.state is not ProductDecisionState.APPROVED:
            raise ProductProjectError("approval intent requires an APPROVED product decision")
        expected_row_version = _strict_int(
            expected_row_version,
            label="expected ProductProject row_version",
            minimum=0,
        )
        fingerprint = _decision_fingerprint(project_id, decision)
        with self.store.connection() as conn:
            # Keep size/type preflight and hydrated evidence on one read snapshot.
            # Otherwise a concurrent writer could replace a bounded handoff between
            # the metadata SELECT and the later BLOB hydration SELECT.
            conn.execute("BEGIN")
            replay = self._replay_conn(
                conn,
                project_id,
                decision,
                idempotency_key,
                fingerprint,
            )
            if replay is not None:
                raise ProductProjectError("product decision is already durably recorded")
            evidence_package_ids = self._prepare_new_decision_conn(
                conn,
                project_id,
                decision,
                expected_row_version=expected_row_version,
            )
            evidence_fingerprint = self._evidence_authority_fingerprint_conn(
                conn,
                project_id,
                evidence_package_ids,
            )
        return self._build_approval_intent(
            project_id,
            decision,
            expected_row_version=expected_row_version,
            idempotency_key=idempotency_key,
            mutation_fingerprint=fingerprint,
            evidence_fingerprint=evidence_fingerprint,
        )

    def record(
        self,
        project_id: str,
        decision: ProductDecision,
        *,
        expected_row_version: int,
        idempotency_key: str,
        approval: ApprovalEvidence | None = None,
        now: datetime | None = None,
    ) -> StoredProductDecision:
        project_id = _validated_text(project_id, label="project_id")
        idempotency_key = _validated_text(idempotency_key, label="idempotency_key")
        decision = _snapshot_decision(decision)
        expected_row_version = _strict_int(
            expected_row_version,
            label="expected ProductProject row_version",
            minimum=0,
        )
        fingerprint = _decision_fingerprint(project_id, decision)

        # Exact durable replay is not a new privileged effect and must remain restart-safe
        # without asking the owner to approve the same already-committed effect again.
        with self.store.connection() as conn:
            # Replay verification spans multiple durable authority reads. Keep them
            # on one SQLite snapshot so a concurrent research refresh cannot make
            # the fast path certify a torn evidence view.
            conn.execute("BEGIN")
            replay = self._replay_conn(
                conn,
                project_id,
                decision,
                idempotency_key,
                fingerprint,
            )
            if replay is not None:
                return replay

        verifier: ApprovalVerifier | None = None
        approval_snapshot: ApprovalEvidence | None = None
        current_time: datetime | None = None
        if decision.state is ProductDecisionState.APPROVED:
            verifier = self.approval_verifier
            if verifier is None:
                raise PermissionError("trusted product-owner approval verifier is required")
            if approval is None:
                raise PermissionError("trusted product-owner approval evidence is required")
            approval_snapshot = _snapshot_approval(approval)
            current_time = _approval_time(now)
        elif approval is not None:
            raise ProductProjectError(
                "approval evidence is only valid for an APPROVED product decision"
            )

        authority_context = (
            verifier.authorization_lock if verifier is not None else nullcontext()
        )
        with authority_context:
            with self.store.connection() as conn:
                try:
                    conn.execute("BEGIN IMMEDIATE")
                except sqlite3.OperationalError as exc:
                    if _is_lock_contention(exc):
                        raise ProductProjectError(
                            "product decision write is temporarily busy"
                        ) from exc
                    raise

                replay = self._replay_conn(
                    conn,
                    project_id,
                    decision,
                    idempotency_key,
                    fingerprint,
                )
                if replay is not None:
                    return replay

                evidence_package_ids = self._prepare_new_decision_conn(
                    conn,
                    project_id,
                    decision,
                    expected_row_version=expected_row_version,
                )
                if verifier is not None:
                    assert approval_snapshot is not None
                    assert current_time is not None
                    evidence_fingerprint = self._evidence_authority_fingerprint_conn(
                        conn,
                        project_id,
                        evidence_package_ids,
                    )
                    intent = self._build_approval_intent(
                        project_id,
                        decision,
                        expected_row_version=expected_row_version,
                        idempotency_key=idempotency_key,
                        mutation_fingerprint=fingerprint,
                        evidence_fingerprint=evidence_fingerprint,
                    )
                    verifier.validate_locked(
                        intent,
                        approval_snapshot,
                        now=current_time,
                    )

                persisted_decision = (
                    replace(
                        decision,
                        decided_by_ref=_trusted_decided_by_ref(approval_snapshot),
                    )
                    if approval_snapshot is not None
                    else decision
                )
                current = self._latest_conn(conn, project_id, decision.decision_id)
                version = 1 if current is None else current.decision_version + 1
                now_text = _now()
                cursor = conn.execute(
                    "UPDATE product_projects SET row_version=row_version+1, updated_at=? "
                    "WHERE project_id=? AND row_version=?",
                    (now_text, project_id, expected_row_version),
                )
                if cursor.rowcount != 1:
                    raise StaleProjectVersionError(
                        "concurrent ProductProject decision update"
                    )
                conn.execute(
                    "INSERT INTO product_decisions("
                    "project_id,decision_id,decision_version,option_id,"
                    "state,rationale,decided_by_ref,evidence_package_ids_json,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        project_id,
                        persisted_decision.decision_id,
                        version,
                        persisted_decision.option_id,
                        persisted_decision.state.value,
                        persisted_decision.rationale,
                        persisted_decision.decided_by_ref,
                        _canonical(list(evidence_package_ids)),
                        now_text,
                    ),
                )
                conn.execute(
                    "INSERT INTO product_project_mutation_idempotency(operation_key,project_id,"
                    "operation_kind,entity_id,entity_version,input_fingerprint,created_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (
                        idempotency_key,
                        project_id,
                        "product_decision.record",
                        decision.decision_id,
                        version,
                        fingerprint,
                        now_text,
                    ),
                )
                audit_payload: dict[str, Any] = {
                    "decision_id": decision.decision_id,
                    "decision_version": version,
                    "option_id": decision.option_id,
                    "state": decision.state.value,
                    "decided_by_ref": persisted_decision.decided_by_ref,
                    "evidence_package_ids": list(evidence_package_ids),
                }
                if approval_snapshot is not None:
                    audit_payload["approval_authority"] = {
                        "approval_id": approval_snapshot.approval_id,
                        "request_id": approval_snapshot.request_id,
                        "issuer_id": approval_snapshot.issuer_id,
                        "authority_version": approval_snapshot.authority_version,
                        "action_fingerprint": approval_snapshot.action_fingerprint,
                        "effect_fingerprint": approval_snapshot.effect_fingerprint,
                    }
                self._audit(conn, project_id, audit_payload)
                stored = self._get_version_conn(
                    conn,
                    project_id,
                    decision.decision_id,
                    version,
                )
                try:
                    conn.commit()
                except sqlite3.OperationalError as exc:
                    if _is_lock_contention(exc):
                        conn.rollback()
                        raise ProductProjectError(
                            "product decision write is temporarily busy"
                        ) from exc
                    raise

            if verifier is not None:
                assert approval_snapshot is not None
                verifier.commit_locked(approval_snapshot)
            return stored

    def get(self, project_id: str, decision_id: str) -> StoredProductDecision:
        project_id = _validated_text(project_id, label="project_id")
        decision_id = _validated_decision_id(decision_id)
        with self.store.connection() as conn:
            decision = self._latest_conn(conn, project_id, decision_id)
            if decision is None:
                raise KeyError(decision_id)
            return decision

    def list(self, project_id: str) -> tuple[StoredProductDecision, ...]:
        project_id = _validated_text(project_id, label="project_id")
        with self.store.connection() as conn:
            if not conn.execute(
                "SELECT 1 FROM product_projects WHERE project_id=?",
                (project_id,),
            ).fetchone():
                raise KeyError(project_id)
            rows = conn.execute(
                "SELECT d.* FROM product_decisions d JOIN ("
                "SELECT project_id,decision_id,MAX(decision_version) AS decision_version "
                "FROM product_decisions WHERE project_id=? GROUP BY project_id,decision_id"
                ") latest ON latest.project_id=d.project_id "
                "AND latest.decision_id=d.decision_id "
                "AND latest.decision_version=d.decision_version "
                "ORDER BY d.decision_id",
                (project_id,),
            ).fetchall()
            return tuple(self._from_row(row) for row in rows)

    def history(
        self,
        project_id: str,
        decision_id: str,
    ) -> tuple[StoredProductDecision, ...]:
        project_id = _validated_text(project_id, label="project_id")
        decision_id = _validated_decision_id(decision_id)
        with self.store.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM product_decisions WHERE project_id=? AND decision_id=? "
                "ORDER BY decision_version",
                (project_id, decision_id),
            ).fetchall()
            if not rows:
                raise KeyError(decision_id)
            return tuple(self._from_row(row) for row in rows)

    def link_requirement(
        self,
        project_id: str,
        *,
        requirement_id: str,
        decision_id: str,
        expected_row_version: int,
    ) -> ProductProject:
        project_id = _validated_text(project_id, label="project_id")
        requirement_id = _validated_text(requirement_id, label="requirement_id")
        decision_id = _validated_decision_id(decision_id)
        expected_row_version = _strict_int(
            expected_row_version,
            label="expected ProductProject row_version",
            minimum=0,
        )
        project = self.projects.get(project_id)
        matching = [
            (index, requirement)
            for index, requirement in enumerate(project.spec.requirements)
            if requirement.requirement_id == requirement_id
        ]
        if not matching:
            raise ProductProjectError(f"unknown product requirement: {requirement_id}")
        if len(matching) > 1:
            raise ProductProjectError(f"ambiguous product requirement id: {requirement_id}")
        index, requirement = matching[0]

        already_linked = decision_id in requirement.decision_ids
        if not already_linked and project.row_version != expected_row_version:
            raise StaleProjectVersionError(
                f"stale ProductProject write: expected {expected_row_version}, "
                f"current {project.row_version}"
            )

        decision = self.get(project_id, decision_id)
        if decision.decision.state is not ProductDecisionState.APPROVED:
            raise ProductProjectError(
                f"requirement requires approved product decision: {decision_id}"
            )
        with self.store.connection() as conn:
            verify_sealed_handoffs_conn(
                conn,
                project_id,
                decision.evidence_package_ids,
            )

        decision_ids = tuple(
            dict.fromkeys((*requirement.decision_ids, decision_id))
        )
        evidence_package_ids = tuple(
            dict.fromkeys(
                (*requirement.evidence_package_ids, *decision.evidence_package_ids)
            )
        )
        if (
            decision_ids == requirement.decision_ids
            and evidence_package_ids == requirement.evidence_package_ids
        ):
            return project
        if project.row_version != expected_row_version:
            raise StaleProjectVersionError(
                f"stale ProductProject write: expected {expected_row_version}, "
                f"current {project.row_version}"
            )

        requirements = list(project.spec.requirements)
        requirements[index] = replace(
            requirement,
            evidence_package_ids=evidence_package_ids,
            decision_ids=decision_ids,
        )
        spec = replace(project.spec, requirements=tuple(requirements))

        def verify_current_evidence(conn: Any) -> None:
            verify_sealed_handoffs_conn(
                conn,
                project_id,
                decision.evidence_package_ids,
            )

        return self.projects.update_spec(
            project_id,
            spec,
            expected_row_version=expected_row_version,
            change_reason=(
                f"link approved decision {decision_id} and evidence "
                f"to requirement {requirement_id}"
            ),
            read_only_precondition=verify_current_evidence,
        )

    def _replay_conn(
        self,
        conn: Any,
        project_id: str,
        decision: ProductDecision,
        idempotency_key: str,
        fingerprint: str,
    ) -> StoredProductDecision | None:
        replay = conn.execute(
            "SELECT project_id,operation_kind,entity_id,entity_version,input_fingerprint "
            "FROM product_project_mutation_idempotency WHERE operation_key=?",
            (idempotency_key,),
        ).fetchone()
        if replay is None:
            return None
        stored_project_id = _validated_text(
            replay["project_id"],
            label="persisted decision replay project_id",
        )
        stored_operation_kind = _validated_text(
            replay["operation_kind"],
            label="persisted decision replay operation_kind",
        )
        stored_entity_id = _validated_decision_id(replay["entity_id"])
        stored_fingerprint = _validated_text(
            replay["input_fingerprint"],
            label="persisted decision replay input_fingerprint",
        )
        if (
            stored_project_id != project_id
            or stored_operation_kind != "product_decision.record"
            or stored_entity_id != decision.decision_id
            or stored_fingerprint != fingerprint
        ):
            raise ProductProjectError(
                "idempotency key was already used with different mutation input"
            )
        entity_version = _strict_int(
            replay["entity_version"],
            label="persisted product decision entity_version",
            minimum=1,
        )
        stored = self._get_version_conn(
            conn,
            project_id,
            decision.decision_id,
            entity_version,
        )
        verify_sealed_handoffs_conn(
            conn,
            project_id,
            stored.evidence_package_ids,
        )
        return stored

    def _prepare_new_decision_conn(
        self,
        conn: Any,
        project_id: str,
        decision: ProductDecision,
        *,
        expected_row_version: int,
    ) -> tuple[str, ...]:
        project = conn.execute(
            "SELECT row_version FROM product_projects WHERE project_id=?",
            (project_id,),
        ).fetchone()
        if project is None:
            raise KeyError(project_id)
        current_row_version = _strict_int(
            project["row_version"],
            label="persisted ProductProject row_version",
            minimum=0,
        )
        if current_row_version != expected_row_version:
            raise StaleProjectVersionError(
                f"stale ProductProject write: expected {expected_row_version}, "
                f"current {current_row_version}"
            )

        evidence_package_ids = self._option_evidence_conn(
            conn,
            project_id,
            decision.option_id,
        )
        verify_sealed_handoffs_conn(conn, project_id, evidence_package_ids)
        current = self._latest_conn(conn, project_id, decision.decision_id)
        self._validate_transition(current, decision)
        if decision.state is ProductDecisionState.APPROVED:
            approved = self._approved_conn(
                conn,
                project_id,
                excluding_decision_id=decision.decision_id,
            )
            if approved is not None:
                raise ProductProjectError(
                    f"project already has approved option {approved.decision.option_id}"
                )
        return evidence_package_ids

    @staticmethod
    def _evidence_authority_fingerprint_conn(
        conn: Any,
        project_id: str,
        evidence_package_ids: tuple[str, ...],
    ) -> str:
        exact_payloads: list[dict[str, str]] = []
        for package_id in evidence_package_ids:
            metadata = conn.execute(
                "SELECT rowid AS handoff_rowid,typeof(payload_json) AS payload_type,"
                "length(CAST(payload_json AS BLOB)) AS payload_bytes "
                "FROM product_research_handoffs WHERE project_id=? AND package_id=?",
                (project_id, package_id),
            ).fetchone()
            if metadata is None:
                raise ProductProjectError(
                    f"product option references unknown evidence package: {package_id}"
                )
            if (
                metadata["payload_type"] != "text"
                or type(metadata["payload_bytes"]) is not int
                or metadata["payload_bytes"] > _MAX_STORED_HANDOFF_BYTES
            ):
                raise ProductProjectError("stored research handoff is malformed")
            hydrated = conn.execute(
                "SELECT CAST(payload_json AS BLOB) AS payload_utf8 "
                "FROM product_research_handoffs WHERE rowid=? AND project_id=?",
                (metadata["handoff_rowid"], project_id),
            ).fetchone()
            if hydrated is None:
                raise ProductProjectError("stored research handoff is malformed")
            payload_json = _decode_stored_utf8(hydrated["payload_utf8"])
            if not _valid_stored_text(payload_json):
                raise ProductProjectError("stored research handoff is malformed")
            exact_payloads.append(
                {
                    "package_id": package_id,
                    "payload_sha256": hashlib.sha256(
                        payload_json.encode("utf-8")
                    ).hexdigest(),
                }
            )
        return hashlib.sha256(_canonical(exact_payloads).encode("utf-8")).hexdigest()

    @staticmethod
    def _build_approval_intent(
        project_id: str,
        decision: ProductDecision,
        *,
        expected_row_version: int,
        idempotency_key: str,
        mutation_fingerprint: str,
        evidence_fingerprint: str,
    ) -> ActionIntent:
        return ActionIntent(
            action_id="product_project.decision.approve",
            tool_id="product_project.owner_decision",
            risk=ToolRisk.HIGH_IMPACT,
            target=f"Approve ProductProject decision {decision.decision_id}",
            task_id=f"product-owner-decision:{mutation_fingerprint[:24]}",
            project_id=project_id,
            resource=decision.option_id,
            arguments={
                "mutation_fingerprint": mutation_fingerprint,
                "expected_row_version": expected_row_version,
                "idempotency_key_sha256": hashlib.sha256(
                    idempotency_key.encode("utf-8")
                ).hexdigest(),
                "evidence_fingerprint": evidence_fingerprint,
            },
            effect_id=f"product-decision:{mutation_fingerprint}",
            scope=(
                ("decision_id", decision.decision_id),
                ("option_id", decision.option_id),
                ("state", decision.state.value),
            ),
        )

    @staticmethod
    def _validate_transition(
        current: StoredProductDecision | None,
        decision: ProductDecision,
    ) -> None:
        if current is None:
            return
        if current.decision.state is not ProductDecisionState.PROPOSED:
            raise ProductProjectError(
                "final product decision is immutable; create a new decision id"
            )
        if current.decision.option_id != decision.option_id:
            raise ProductProjectError("product decision option cannot change across versions")
        if decision.state is ProductDecisionState.PROPOSED:
            raise ProductProjectError("proposed product decision cannot be proposed twice")

    @staticmethod
    def _from_row(row: Any) -> StoredProductDecision:
        project_id = _validated_text(
            row["project_id"],
            label="persisted product decision project_id",
        )
        raw_state = _validated_text(
            row["state"],
            label="persisted product decision state",
        )
        try:
            state = ProductDecisionState(raw_state)
        except ValueError as exc:
            raise ProductProjectError(
                "persisted product decision state is invalid"
            ) from exc
        decision = ProductDecision(
            decision_id=_validated_decision_id(row["decision_id"]),
            option_id=_validated_text(
                row["option_id"],
                label="persisted product decision option_id",
            ),
            state=state,
            rationale=_validated_text(
                row["rationale"],
                label="persisted product decision rationale",
            ),
            decided_by_ref=_validated_text(
                row["decided_by_ref"],
                label="persisted product decision decided_by_ref",
            ),
        )
        return StoredProductDecision(
            project_id=project_id,
            decision=decision,
            decision_version=_strict_int(
                row["decision_version"],
                label="persisted product decision version",
                minimum=1,
            ),
            evidence_package_ids=_decode_id_list(
                row["evidence_package_ids_json"],
                label="persisted product decision evidence package ids",
            ),
            created_at=_validated_text(
                row["created_at"],
                label="persisted product decision created_at",
            ),
        )

    def _get_version_conn(
        self,
        conn: Any,
        project_id: str,
        decision_id: str,
        version: int,
    ) -> StoredProductDecision:
        row = conn.execute(
            "SELECT * FROM product_decisions WHERE project_id=? AND decision_id=? "
            "AND decision_version=?",
            (project_id, decision_id, version),
        ).fetchone()
        if row is None:
            raise KeyError(decision_id)
        return self._from_row(row)

    def _latest_conn(
        self,
        conn: Any,
        project_id: str,
        decision_id: str,
    ) -> StoredProductDecision | None:
        row = conn.execute(
            "SELECT * FROM product_decisions WHERE project_id=? AND decision_id=? "
            "ORDER BY decision_version DESC LIMIT 1",
            (project_id, decision_id),
        ).fetchone()
        return None if row is None else self._from_row(row)

    def _approved_conn(
        self,
        conn: Any,
        project_id: str,
        *,
        excluding_decision_id: str,
    ) -> StoredProductDecision | None:
        row = conn.execute(
            "SELECT d.* FROM product_decisions d JOIN ("
            "SELECT project_id,decision_id,MAX(decision_version) AS decision_version "
            "FROM product_decisions WHERE project_id=? GROUP BY project_id,decision_id"
            ") latest ON latest.project_id=d.project_id "
            "AND latest.decision_id=d.decision_id "
            "AND latest.decision_version=d.decision_version "
            "WHERE d.state=? AND d.decision_id<>? ORDER BY d.decision_id LIMIT 1",
            (project_id, ProductDecisionState.APPROVED.value, excluding_decision_id),
        ).fetchone()
        return None if row is None else self._from_row(row)

    @staticmethod
    def _option_evidence_conn(
        conn: Any,
        project_id: str,
        option_id: str,
    ) -> tuple[str, ...]:
        rows = conn.execute(
            "SELECT rowid AS handoff_rowid,typeof(package_id) AS package_type,"
            "length(CAST(package_id AS BLOB)) AS package_bytes,"
            "typeof(payload_json) AS payload_type,"
            "length(CAST(payload_json AS BLOB)) AS payload_bytes "
            "FROM product_research_handoffs WHERE project_id=?",
            (project_id,),
        )
        matches: list[tuple[str, ...]] = []
        for row in rows:
            if (
                row["package_type"] != "text"
                or type(row["package_bytes"]) is not int
                or row["package_bytes"] > _MAX_STORED_HANDOFF_BYTES
                or row["payload_type"] != "text"
                or type(row["payload_bytes"]) is not int
                or row["payload_bytes"] > _MAX_STORED_HANDOFF_BYTES
            ):
                raise ProductProjectError("stored research handoff is malformed")
            hydrated = conn.execute(
                "SELECT CAST(package_id AS BLOB) AS package_utf8,"
                "CAST(payload_json AS BLOB) AS payload_utf8 "
                "FROM product_research_handoffs WHERE rowid=? AND project_id=?",
                (row["handoff_rowid"], project_id),
            ).fetchone()
            if hydrated is None:
                raise ProductProjectError("stored research handoff is malformed")
            row_package_id = _decode_stored_utf8(hydrated["package_utf8"])
            raw = _decode_stored_utf8(hydrated["payload_utf8"])
            if not _valid_stored_text(row_package_id):
                raise ProductProjectError("stored research handoff is malformed")
            try:
                payload = json.loads(
                    raw,
                    parse_constant=_reject_nonfinite_evidence,
                    parse_float=_finite_evidence_float,
                    object_pairs_hook=_unique_evidence_keys,
                )
            except (TypeError, ValueError, RecursionError) as exc:
                raise ProductProjectError("stored research handoff is malformed") from exc
            if (
                type(payload) is not dict
                or not _valid_stored_text(payload.get("package_id"))
                or payload["package_id"] != row_package_id
                or type(payload.get("options")) is not list
                or type(payload.get("evidence")) is not list
                or not payload["evidence"]
            ):
                raise ProductProjectError("stored research handoff is malformed")
            evidence_ids: set[str] = set()
            for evidence in payload["evidence"]:
                if (
                    type(evidence) is not dict
                    or not _valid_stored_text(evidence.get("evidence_id"))
                    or not _valid_stored_text(evidence.get("provenance_ref"))
                    or evidence["evidence_id"] in evidence_ids
                ):
                    raise ProductProjectError("stored research evidence is malformed")
                evidence_ids.add(evidence["evidence_id"])
            option_ids: set[str] = set()
            for option in payload["options"]:
                if (
                    type(option) is not dict
                    or not _valid_stored_text(option.get("option_id"))
                    or option["option_id"] in option_ids
                ):
                    raise ProductProjectError("stored product option is malformed")
                option_ids.add(option["option_id"])
                package_ids = option.get("evidence_package_ids")
                if (
                    type(package_ids) is not list
                    or not package_ids
                    or any(
                        not _valid_stored_text(package_id)
                        for package_id in package_ids
                    )
                    or len(package_ids) != len(set(package_ids))
                    or row_package_id not in package_ids
                ):
                    raise ProductProjectError(
                        "stored product option evidence is malformed: evidence package "
                        "ids must be non-empty strings, unique, and include the owning package"
                    )
                if option["option_id"] == option_id:
                    matches.append(tuple(package_ids))
        if not matches:
            raise ProductProjectError(f"unknown product option: {option_id}")
        if len(matches) > 1:
            raise ProductProjectError(f"ambiguous product option id: {option_id}")
        package_ids = matches[0]
        for package_id in package_ids:
            if not conn.execute(
                "SELECT 1 FROM product_research_handoffs "
                "WHERE project_id=? AND package_id=?",
                (project_id, package_id),
            ).fetchone():
                raise ProductProjectError(
                    f"product option references unknown evidence package: {package_id}"
                )
        return package_ids

    @staticmethod
    def _audit(conn: Any, project_id: str, payload: dict[str, Any]) -> None:
        conn.execute(
            "INSERT INTO audit_events(event_type,entity_type,entity_id,payload_json,created_at) "
            "VALUES (?,?,?,?,?)",
            (
                "product_project.decision_recorded",
                "product_project",
                project_id,
                _canonical(payload),
                _now(),
            ),
        )
