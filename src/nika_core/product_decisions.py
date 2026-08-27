from __future__ import annotations

import hashlib
import json
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


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _strict_int(value: Any, *, label: str, minimum: int) -> int:
    if type(value) is not int or value < minimum:
        raise ProductProjectError(
            f"{label} must be an integer greater than or equal to {minimum}"
        )
    return value


def _decode_id_list(raw: Any, *, label: str) -> tuple[str, ...]:
    try:
        values = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ProductProjectError(f"{label} contains invalid JSON") from exc
    if not isinstance(values, list) or not values:
        raise ProductProjectError(f"{label} must be a non-empty list")
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise ProductProjectError(f"{label} must contain non-empty string identifiers")
    if len(values) != len(set(values)):
        raise ProductProjectError(f"{label} must not contain duplicates")
    return tuple(values)


@dataclass(frozen=True, slots=True)
class StoredProductDecision:
    project_id: str
    decision: ProductDecision
    decision_version: int
    evidence_package_ids: tuple[str, ...]
    created_at: str


class ProductDecisionRepository:
    """Durable PF1 decision lifecycle over the canonical ProductProject SQLite store."""

    def __init__(self, store: Any) -> None:
        self.store = store
        self.projects = ProductProjectRepository(store)

    def record(
        self,
        project_id: str,
        decision: ProductDecision,
        *,
        expected_row_version: int,
        idempotency_key: str,
    ) -> StoredProductDecision:
        self._validate_input(decision, idempotency_key)
        expected_row_version = _strict_int(
            expected_row_version,
            label="expected ProductProject row_version",
            minimum=0,
        )
        fingerprint = hashlib.sha256(
            _canonical(
                {
                    "project_id": project_id,
                    "decision_id": decision.decision_id,
                    "option_id": decision.option_id,
                    "state": decision.state.value,
                    "rationale": decision.rationale,
                    "decided_by_ref": decision.decided_by_ref,
                }
            ).encode()
        ).hexdigest()
        with self.store.connection() as conn:
            replay = conn.execute(
                "SELECT project_id,operation_kind,entity_id,entity_version,input_fingerprint "
                "FROM product_project_mutation_idempotency WHERE operation_key=?",
                (idempotency_key,),
            ).fetchone()
            if replay is not None:
                if (
                    replay["project_id"] != project_id
                    or replay["operation_kind"] != "product_decision.record"
                    or replay["entity_id"] != decision.decision_id
                    or replay["input_fingerprint"] != fingerprint
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
                conn, project_id, decision.option_id
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

            version = 1 if current is None else current.decision_version + 1
            now = _now()
            cursor = conn.execute(
                "UPDATE product_projects SET row_version=row_version+1, updated_at=? "
                "WHERE project_id=? AND row_version=?",
                (now, project_id, expected_row_version),
            )
            if cursor.rowcount != 1:
                raise StaleProjectVersionError("concurrent ProductProject decision update")
            conn.execute(
                "INSERT INTO product_decisions(project_id,decision_id,decision_version,option_id,"
                "state,rationale,decided_by_ref,evidence_package_ids_json,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    project_id,
                    decision.decision_id,
                    version,
                    decision.option_id,
                    decision.state.value,
                    decision.rationale,
                    decision.decided_by_ref,
                    _canonical(list(evidence_package_ids)),
                    now,
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
                    now,
                ),
            )
            self._audit(
                conn,
                project_id,
                {
                    "decision_id": decision.decision_id,
                    "decision_version": version,
                    "option_id": decision.option_id,
                    "state": decision.state.value,
                    "evidence_package_ids": list(evidence_package_ids),
                },
            )
            return self._get_version_conn(
                conn,
                project_id,
                decision.decision_id,
                version,
            )

    def get(self, project_id: str, decision_id: str) -> StoredProductDecision:
        with self.store.connection() as conn:
            decision = self._latest_conn(conn, project_id, decision_id)
            if decision is None:
                raise KeyError(decision_id)
            return decision

    def list(self, project_id: str) -> tuple[StoredProductDecision, ...]:
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
        return self.projects.update_spec(
            project_id,
            spec,
            expected_row_version=expected_row_version,
            change_reason=(
                f"link approved decision {decision_id} and evidence "
                f"to requirement {requirement_id}"
            ),
        )

    @staticmethod
    def _validate_input(decision: ProductDecision, idempotency_key: str) -> None:
        if not idempotency_key.strip():
            raise ProductProjectError("idempotency_key is required")
        if not decision.decision_id.strip() or not decision.option_id.strip():
            raise ProductProjectError("product decision requires decision_id and option_id")
        if not isinstance(decision.state, ProductDecisionState):
            raise ProductProjectError("product decision state must be ProductDecisionState")
        if not decision.rationale.strip() or not decision.decided_by_ref.strip():
            raise ProductProjectError("product decision requires rationale and decided_by_ref")

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
        return StoredProductDecision(
            project_id=row["project_id"],
            decision=ProductDecision(
                decision_id=row["decision_id"],
                option_id=row["option_id"],
                state=ProductDecisionState(row["state"]),
                rationale=row["rationale"],
                decided_by_ref=row["decided_by_ref"],
            ),
            decision_version=_strict_int(
                row["decision_version"],
                label="persisted product decision version",
                minimum=1,
            ),
            evidence_package_ids=_decode_id_list(
                row["evidence_package_ids_json"],
                label="persisted product decision evidence package ids",
            ),
            created_at=row["created_at"],
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
            "SELECT payload_json FROM product_research_handoffs WHERE project_id=?",
            (project_id,),
        ).fetchall()
        matches: list[tuple[str, ...]] = []
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
            except (TypeError, json.JSONDecodeError) as exc:
                raise ProductProjectError(
                    "product research handoff contains invalid JSON"
                ) from exc
            if not isinstance(payload, dict):
                raise ProductProjectError(
                    "product research handoff must contain a JSON object"
                )
            raw_options = payload.get("options", [])
            if not isinstance(raw_options, list):
                raise ProductProjectError(
                    "product research handoff options must be a list"
                )
            for option in raw_options:
                if not isinstance(option, dict):
                    raise ProductProjectError(
                        "product research handoff contains invalid option data"
                    )
                if option.get("option_id") == option_id:
                    raw_package_ids = option.get("evidence_package_ids", ())
                    if not isinstance(raw_package_ids, list):
                        raise ProductProjectError(
                            "product option evidence_package_ids must be a list"
                        )
                    if not raw_package_ids or any(
                        not isinstance(value, str) or not value.strip()
                        for value in raw_package_ids
                    ):
                        raise ProductProjectError(
                            "product option evidence package ids must be non-empty strings"
                        )
                    package_ids = tuple(raw_package_ids)
                    if len(set(package_ids)) != len(package_ids):
                        raise ProductProjectError(
                            "product option contains duplicate evidence package ids"
                        )
                    matches.append(package_ids)
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
