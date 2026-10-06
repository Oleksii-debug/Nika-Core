from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from nika_core.product_project import (
    ProductProject,
    ProductProjectError,
    ProductProjectRepository,
    StaleProjectVersionError,
    _reject_secret_material,
)


class ProductProjectState(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    ARCHIVED = "archived"


_ALLOWED_TRANSITIONS: dict[ProductProjectState, frozenset[ProductProjectState]] = {
    ProductProjectState.ACTIVE: frozenset(
        {
            ProductProjectState.PAUSED,
            ProductProjectState.BLOCKED,
            ProductProjectState.COMPLETED,
            ProductProjectState.CANCELLED,
            ProductProjectState.ARCHIVED,
        }
    ),
    ProductProjectState.PAUSED: frozenset(
        {
            ProductProjectState.ACTIVE,
            ProductProjectState.BLOCKED,
            ProductProjectState.CANCELLED,
            ProductProjectState.ARCHIVED,
        }
    ),
    ProductProjectState.BLOCKED: frozenset(
        {
            ProductProjectState.ACTIVE,
            ProductProjectState.PAUSED,
            ProductProjectState.CANCELLED,
            ProductProjectState.ARCHIVED,
        }
    ),
    ProductProjectState.COMPLETED: frozenset(
        {ProductProjectState.ACTIVE, ProductProjectState.ARCHIVED}
    ),
    ProductProjectState.CANCELLED: frozenset(
        {ProductProjectState.ACTIVE, ProductProjectState.ARCHIVED}
    ),
    ProductProjectState.ARCHIVED: frozenset({ProductProjectState.ACTIVE}),
}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class ProductProjectStatusTransition:
    project_id: str
    row_version: int
    previous_state: ProductProjectState | None
    new_state: ProductProjectState
    reason: str
    changed_by_ref: str
    created_at: str


class ProductProjectLifecycleService:
    """Durable PF0/PF12 ProductProject lifecycle over the canonical PF1 store."""

    def __init__(self, store: Any) -> None:
        self.store = store
        self.projects = ProductProjectRepository(store)

    def transition(
        self,
        project_id: str,
        new_state: ProductProjectState,
        *,
        expected_row_version: int,
        idempotency_key: str,
        reason: str,
        changed_by_ref: str,
    ) -> ProductProjectStatusTransition:
        if not isinstance(new_state, ProductProjectState):
            raise ProductProjectError("new_state must be ProductProjectState")
        if not idempotency_key.strip():
            raise ProductProjectError("idempotency_key is required")
        if not reason.strip() or not changed_by_ref.strip():
            raise ProductProjectError("status transition requires reason and changed_by_ref")
        _reject_secret_material(
            {"reason": reason, "changed_by_ref": changed_by_ref},
            path="product_project.status_transition",
        )
        fingerprint = hashlib.sha256(
            _canonical(
                {
                    "project_id": project_id,
                    "new_state": new_state.value,
                    "reason": reason,
                    "changed_by_ref": changed_by_ref,
                }
            ).encode()
        ).hexdigest()

        with self.store.connection() as conn:
            # Reserve the SQLite writer before the first durable idempotency/state read.
            # This makes same-key replay and row-version validation one serialized authority
            # interval instead of a read-then-write race across two concurrent connections.
            conn.execute("BEGIN IMMEDIATE")
            replay = conn.execute(
                "SELECT project_id,operation_kind,entity_id,entity_version,"
                "input_fingerprint FROM product_project_mutation_idempotency "
                "WHERE operation_key=?",
                (idempotency_key,),
            ).fetchone()
            if replay is not None:
                if (
                    replay["project_id"] != project_id
                    or replay["operation_kind"] != "product_project.status_transition"
                    or replay["entity_id"] != project_id
                    or replay["input_fingerprint"] != fingerprint
                ):
                    raise ProductProjectError(
                        "idempotency key was already used with different mutation input"
                    )
                return self._transition_for_row_version(
                    conn,
                    project_id,
                    int(replay["entity_version"]),
                )

            row = conn.execute(
                "SELECT status,row_version FROM product_projects WHERE project_id=?",
                (project_id,),
            ).fetchone()
            if row is None:
                raise KeyError(project_id)
            if int(row["row_version"]) != expected_row_version:
                raise StaleProjectVersionError(
                    f"stale ProductProject write: expected {expected_row_version}, "
                    f"current {row['row_version']}"
                )
            try:
                previous_state = ProductProjectState(row["status"])
            except ValueError as exc:
                raise ProductProjectError(
                    f"unsupported durable ProductProject status: {row['status']}"
                ) from exc
            if new_state is previous_state:
                raise ProductProjectError(
                    "status transition must change state; use idempotent replay for retries"
                )
            if new_state not in _ALLOWED_TRANSITIONS[previous_state]:
                raise ProductProjectError(
                    "invalid ProductProject status transition: "
                    f"{previous_state.value}->{new_state.value}"
                )

            now = _now()
            next_row_version = expected_row_version + 1
            cursor = conn.execute(
                "UPDATE product_projects SET status=?,row_version=row_version+1,updated_at=? "
                "WHERE project_id=? AND row_version=?",
                (new_state.value, now, project_id, expected_row_version),
            )
            if cursor.rowcount != 1:
                raise StaleProjectVersionError("concurrent ProductProject status update")
            conn.execute(
                "INSERT INTO product_project_mutation_idempotency(operation_key,project_id,"
                "operation_kind,entity_id,entity_version,input_fingerprint,created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    idempotency_key,
                    project_id,
                    "product_project.status_transition",
                    project_id,
                    next_row_version,
                    fingerprint,
                    now,
                ),
            )
            self._audit(
                conn,
                project_id,
                {
                    "row_version": next_row_version,
                    "previous_state": previous_state.value,
                    "new_state": new_state.value,
                    "reason": reason,
                    "changed_by_ref": changed_by_ref,
                },
                now,
            )
            return ProductProjectStatusTransition(
                project_id=project_id,
                row_version=next_row_version,
                previous_state=previous_state,
                new_state=new_state,
                reason=reason,
                changed_by_ref=changed_by_ref,
                created_at=now,
            )

    def history(self, project_id: str) -> tuple[ProductProjectStatusTransition, ...]:
        project = self.projects.get(project_id)
        baseline = ProductProjectStatusTransition(
            project_id=project_id,
            row_version=0,
            previous_state=None,
            new_state=ProductProjectState.ACTIVE,
            reason="initial project state",
            changed_by_ref="system://product-project-create",
            created_at=project.created_at,
        )
        with self.store.connection() as conn:
            rows = conn.execute(
                "SELECT payload_json,created_at FROM audit_events "
                "WHERE event_type='product_project.status_changed' "
                "AND entity_type='product_project' AND entity_id=? ORDER BY event_id",
                (project_id,),
            ).fetchall()
        return (baseline, *(self._from_audit_row(project_id, row) for row in rows))

    def current_state(self, project_id: str) -> ProductProjectState:
        project = self.projects.get(project_id)
        try:
            return ProductProjectState(project.status)
        except ValueError as exc:
            raise ProductProjectError(
                f"unsupported durable ProductProject status: {project.status}"
            ) from exc

    @staticmethod
    def is_runnable(project: ProductProject) -> bool:
        try:
            return ProductProjectState(project.status) is ProductProjectState.ACTIVE
        except ValueError as exc:
            raise ProductProjectError(
                f"unsupported durable ProductProject status: {project.status}"
            ) from exc

    def _transition_for_row_version(
        self,
        conn: Any,
        project_id: str,
        row_version: int,
    ) -> ProductProjectStatusTransition:
        rows = conn.execute(
            "SELECT payload_json,created_at FROM audit_events "
            "WHERE event_type='product_project.status_changed' "
            "AND entity_type='product_project' AND entity_id=? ORDER BY event_id",
            (project_id,),
        ).fetchall()
        for row in rows:
            transition = self._from_audit_row(project_id, row)
            if transition.row_version == row_version:
                return transition
        raise ProductProjectError(
            "status idempotency record has no matching durable audit evidence"
        )

    @staticmethod
    def _from_audit_row(
        project_id: str,
        row: Any,
    ) -> ProductProjectStatusTransition:
        payload = json.loads(row["payload_json"])
        try:
            previous_state = ProductProjectState(payload["previous_state"])
            new_state = ProductProjectState(payload["new_state"])
            row_version = int(payload["row_version"])
            reason = str(payload["reason"])
            changed_by_ref = str(payload["changed_by_ref"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ProductProjectError("invalid ProductProject status audit evidence") from exc
        if row_version < 1 or not reason.strip() or not changed_by_ref.strip():
            raise ProductProjectError("invalid ProductProject status audit evidence")
        return ProductProjectStatusTransition(
            project_id=project_id,
            row_version=row_version,
            previous_state=previous_state,
            new_state=new_state,
            reason=reason,
            changed_by_ref=changed_by_ref,
            created_at=row["created_at"],
        )

    @staticmethod
    def _audit(
        conn: Any,
        project_id: str,
        payload: dict[str, Any],
        created_at: str,
    ) -> None:
        conn.execute(
            "INSERT INTO audit_events(event_type,entity_type,entity_id,payload_json,created_at) "
            "VALUES (?,?,?,?,?)",
            (
                "product_project.status_changed",
                "product_project",
                project_id,
                _canonical(payload),
                created_at,
            ),
        )