from __future__ import annotations

import hashlib
import json
import sqlite3
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

_SQLITE_INTEGER_MAX = (1 << 63) - 1


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _is_lock_contention(exc: sqlite3.OperationalError) -> bool:
    error_code = getattr(exc, "sqlite_errorcode", None)
    return isinstance(error_code, int) and (error_code & 0xFF) in {
        sqlite3.SQLITE_BUSY,
        sqlite3.SQLITE_LOCKED,
    }


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
        project_id = self._validated_text(project_id, label="project_id")
        idempotency_key = self._validated_text(idempotency_key, label="idempotency_key")
        expected_row_version = self._validated_integer(
            expected_row_version,
            label="expected_row_version",
            minimum=0,
        )
        decision = self._snapshot_decision(decision)
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
            # Serialize the idempotency/project-version read with the mutation itself.
            # This makes concurrent identical calls wait for the winner and then
            # replay its canonical result instead of racing stale authority reads.
            try:
                conn.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                if _is_lock_contention(exc):
                    raise ProductProjectError(
                        "product decision write is temporarily busy"
                    ) from exc
                raise
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
                return self._get_version_conn(
                    conn,
                    project_id,
                    decision.decision_id,
                    self._validated_integer(
                        replay["entity_version"],
                        label="stored idempotency entity_version",
                        minimum=1,
                    ),
                )

            project = conn.execute(
                "SELECT row_version FROM product_projects WHERE project_id=?",
                (project_id,),
            ).fetchone()
            if project is None:
                raise KeyError(project_id)
            stored_row_version = self._validated_integer(
                project["row_version"],
                label="stored ProductProject row_version",
                minimum=0,
            )
            if stored_row_version != expected_row_version:
                raise StaleProjectVersionError(
                    f"stale ProductProject write: expected {expected_row_version}, "
                    f"current {stored_row_version}"
                )

            evidence_package_ids = self._option_evidence_conn(
                conn, project_id, decision.option_id
            )
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
            return stored

    def get(self, project_id: str, decision_id: str) -> StoredProductDecision:
        project_id = self._validated_text(project_id, label="project_id")
        decision_id = self._validated_text(decision_id, label="decision_id")
        with self.store.connection() as conn:
            decision = self._latest_conn(conn, project_id, decision_id)
            if decision is None:
                raise KeyError(decision_id)
            return decision

    def list(self, project_id: str) -> tuple[StoredProductDecision, ...]:
        project_id = self._validated_text(project_id, label="project_id")
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
        project_id = self._validated_text(project_id, label="project_id")
        decision_id = self._validated_text(decision_id, label="decision_id")
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
        project_id = self._validated_text(project_id, label="project_id")
        requirement_id = self._validated_text(requirement_id, label="requirement_id")
        decision_id = self._validated_text(decision_id, label="decision_id")
        expected_row_version = self._validated_integer(
            expected_row_version,
            label="expected_row_version",
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
        if decision_id in requirement.decision_ids:
            return project
        if project.row_version != expected_row_version:
            raise StaleProjectVersionError(
                f"stale ProductProject write: expected {expected_row_version}, "
                f"current {project.row_version}"
            )
        decision = self.get(project_id, decision_id)
        if decision.decision.state is not ProductDecisionState.APPROVED:
            raise ProductProjectError(
                f"requirement requires approved product decision: {decision_id}"
            )
        requirements = list(project.spec.requirements)
        requirements[index] = replace(
            requirement,
            decision_ids=(*requirement.decision_ids, decision_id),
        )
        spec = replace(project.spec, requirements=tuple(requirements))
        return self.projects.update_spec(
            project_id,
            spec,
            expected_row_version=expected_row_version,
        )

    @staticmethod
    def _validated_text(value: object, *, label: str) -> str:
        if type(value) is not str or not value.strip():
            raise ProductProjectError(f"{label} must be non-empty text")
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ProductProjectError(f"{label} must be valid UTF-8 text") from exc
        return value

    @staticmethod
    def _validated_integer(value: object, *, label: str, minimum: int) -> int:
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

    @classmethod
    def _snapshot_decision(cls, decision: object) -> ProductDecision:
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
            decision_id=cls._validated_text(decision_id, label="product decision decision_id"),
            option_id=cls._validated_text(option_id, label="product decision option_id"),
            state=state,
            rationale=cls._validated_text(rationale, label="product decision rationale"),
            decided_by_ref=cls._validated_text(
                decided_by_ref,
                label="product decision decided_by_ref",
            ),
        )

    @classmethod
    def _validated_string_list(
        cls,
        value: object,
        *,
        label: str,
        require_nonempty: bool,
    ) -> tuple[str, ...]:
        if type(value) is not list:
            raise ProductProjectError(f"{label} must be a JSON array")
        items = tuple(
            cls._validated_text(item, label=f"{label} item")
            for item in value
        )
        if require_nonempty and not items:
            raise ProductProjectError(f"{label} must not be empty")
        if len(items) != len(set(items)):
            raise ProductProjectError(f"{label} must not contain duplicates")
        return items

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

    @classmethod
    def _from_row(cls, row: Any) -> StoredProductDecision:
        project_id = cls._validated_text(
            row["project_id"],
            label="stored product decision project_id",
        )
        raw_state = cls._validated_text(
            row["state"],
            label="stored product decision state",
        )
        try:
            state = ProductDecisionState(raw_state)
        except ValueError as exc:
            raise ProductProjectError("stored product decision state is invalid") from exc
        raw_evidence = cls._validated_text(
            row["evidence_package_ids_json"],
            label="stored product decision evidence_package_ids_json",
        )
        try:
            decoded_evidence = json.loads(raw_evidence)
        except json.JSONDecodeError as exc:
            raise ProductProjectError(
                "stored product decision evidence_package_ids_json is invalid JSON"
            ) from exc
        evidence_package_ids = cls._validated_string_list(
            decoded_evidence,
            label="stored product decision evidence_package_ids",
            require_nonempty=True,
        )
        return StoredProductDecision(
            project_id=project_id,
            decision=ProductDecision(
                decision_id=cls._validated_text(
                    row["decision_id"],
                    label="stored product decision decision_id",
                ),
                option_id=cls._validated_text(
                    row["option_id"],
                    label="stored product decision option_id",
                ),
                state=state,
                rationale=cls._validated_text(
                    row["rationale"],
                    label="stored product decision rationale",
                ),
                decided_by_ref=cls._validated_text(
                    row["decided_by_ref"],
                    label="stored product decision decided_by_ref",
                ),
            ),
            decision_version=cls._validated_integer(
                row["decision_version"],
                label="stored product decision decision_version",
                minimum=1,
            ),
            evidence_package_ids=evidence_package_ids,
            created_at=cls._validated_text(
                row["created_at"],
                label="stored product decision created_at",
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

    @classmethod
    def _option_evidence_conn(
        cls,
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
            raw_payload = cls._validated_text(
                row["payload_json"],
                label="stored research handoff payload_json",
            )
            try:
                payload = json.loads(raw_payload)
            except json.JSONDecodeError as exc:
                raise ProductProjectError("stored research handoff payload is invalid JSON") from exc
            if type(payload) is not dict:
                raise ProductProjectError("stored research handoff payload must be a JSON object")
            options = payload.get("options")
            if type(options) is not list:
                raise ProductProjectError("stored research handoff options must be a JSON array")
            for option in options:
                if type(option) is not dict:
                    raise ProductProjectError(
                        "stored research handoff option must be a JSON object"
                    )
                stored_option_id = cls._validated_text(
                    option.get("option_id"),
                    label="stored research handoff option_id",
                )
                evidence_package_ids = cls._validated_string_list(
                    option.get("evidence_package_ids"),
                    label="stored research handoff evidence_package_ids",
                    require_nonempty=True,
                )
                if stored_option_id == option_id:
                    matches.append(evidence_package_ids)
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
