from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_factory_coordinator import ComponentWorkRequest

_SCHEMA_VERSION = 1


class ProductFactoryToolsmithBindingError(RuntimeError):
    """Raised when durable Product Factory↔Toolsmith binding cannot be trusted."""


class ComponentCapabilityBindingState(StrEnum):
    RESERVED = "reserved"
    BEGUN = "begun"
    RESUME_PREPARED = "resume_prepared"
    CONSUMED = "consumed"


@dataclass(frozen=True, slots=True)
class ComponentCapabilityBinding:
    host_task_id: str
    project_id: str
    component_id: str
    work_id: str
    capability_id: str
    reason: str
    attempted_methods: tuple[str, ...]
    permission_ceiling: frozenset[str]
    state: ComponentCapabilityBindingState
    row_version: int
    escalation_row_version: int | None = None
    candidate_state: str | None = None
    next_work_id: str | None = None
    pinned_version: str | None = None
    pinned_digest: str | None = None

    def __post_init__(self) -> None:
        identities = (
            self.host_task_id,
            self.project_id,
            self.component_id,
            self.work_id,
            self.capability_id,
            self.reason,
        )
        if not all(value.strip() for value in identities):
            raise ProductFactoryToolsmithBindingError(
                "durable capability binding identity is incomplete"
            )
        if self.row_version < 0:
            raise ProductFactoryToolsmithBindingError("binding row version must be non-negative")
        if self.escalation_row_version is not None and self.escalation_row_version < 0:
            raise ProductFactoryToolsmithBindingError(
                "escalation row version must be non-negative"
            )
        if not self.permission_ceiling:
            raise ProductFactoryToolsmithBindingError(
                "durable capability binding requires a permission ceiling"
            )
        if self.state is ComponentCapabilityBindingState.RESERVED:
            if any(
                value is not None
                for value in (
                    self.escalation_row_version,
                    self.candidate_state,
                    self.next_work_id,
                    self.pinned_version,
                    self.pinned_digest,
                )
            ):
                raise ProductFactoryToolsmithBindingError(
                    "reserved binding contains later-stage evidence"
                )
        if self.state is ComponentCapabilityBindingState.BEGUN:
            if self.escalation_row_version is None or not self.candidate_state:
                raise ProductFactoryToolsmithBindingError(
                    "begun binding is missing escalation identity"
                )
            if any(
                value is not None
                for value in (self.next_work_id, self.pinned_version, self.pinned_digest)
            ):
                raise ProductFactoryToolsmithBindingError(
                    "begun binding contains resume evidence"
                )
        if self.state in {
            ComponentCapabilityBindingState.RESUME_PREPARED,
            ComponentCapabilityBindingState.CONSUMED,
        }:
            if (
                self.escalation_row_version is None
                or not self.candidate_state
                or not self.next_work_id
                or not self.pinned_version
                or not self.pinned_digest
            ):
                raise ProductFactoryToolsmithBindingError(
                    "prepared/consumed binding is missing exact resume evidence"
                )


class ProductFactoryToolsmithBindingRepository:
    """Thin durable mapping between one PF worker attempt and one Toolsmith escalation."""

    def __init__(self, store: SQLiteStore) -> None:
        self._store = store
        self._initialize_schema()

    def reserve(
        self,
        *,
        host_task_id: str,
        request: ComponentWorkRequest,
        capability_id: str,
        reason: str,
        attempted_methods: tuple[str, ...],
    ) -> ComponentCapabilityBinding:
        if not host_task_id.strip() or not capability_id.strip() or not reason.strip():
            raise ProductFactoryToolsmithBindingError(
                "host task, capability id and reason must not be empty"
            )
        if any(not method.strip() for method in attempted_methods):
            raise ProductFactoryToolsmithBindingError("attempted methods must not be empty")

        with self._store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._validate_host_task(conn, host_task_id, request.project_id)
            existing = conn.execute(
                """
                SELECT *
                FROM product_factory_toolsmith_bindings
                WHERE host_task_id = ? AND work_id = ?
                """,
                (host_task_id, request.work_id),
            ).fetchone()
            if existing is not None:
                binding = _binding_from_row(existing)
                self._validate_replay(
                    binding=binding,
                    request=request,
                    capability_id=capability_id,
                    reason=reason,
                    attempted_methods=attempted_methods,
                )
                return binding

            active = conn.execute(
                """
                SELECT *
                FROM product_factory_toolsmith_bindings
                WHERE host_task_id = ? AND component_id = ? AND state != ?
                ORDER BY rowid DESC
                LIMIT 1
                """,
                (
                    host_task_id,
                    request.component_id,
                    ComponentCapabilityBindingState.CONSUMED.value,
                ),
            ).fetchone()
            if active is not None:
                raise ProductFactoryToolsmithBindingError(
                    "component already has an unconsumed durable capability gap"
                )

            now = _now()
            conn.execute(
                """
                INSERT INTO product_factory_toolsmith_bindings(
                    host_task_id, project_id, component_id, work_id, capability_id,
                    reason, attempted_methods_json, permission_ceiling_json, state,
                    row_version, escalation_row_version, candidate_state, next_work_id,
                    pinned_version, pinned_digest, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, NULL, NULL, NULL, NULL, ?, ?)
                """,
                (
                    host_task_id,
                    request.project_id,
                    request.component_id,
                    request.work_id,
                    capability_id,
                    reason,
                    _json(attempted_methods),
                    _json(sorted(request.permission_ceiling)),
                    ComponentCapabilityBindingState.RESERVED.value,
                    now,
                    now,
                ),
            )
        return self.require(host_task_id=host_task_id, work_id=request.work_id)

    def mark_begun(
        self,
        binding: ComponentCapabilityBinding,
        *,
        escalation_row_version: int,
        candidate_state: str,
    ) -> ComponentCapabilityBinding:
        if escalation_row_version < 0 or not candidate_state.strip():
            raise ProductFactoryToolsmithBindingError("invalid Toolsmith begin evidence")
        current = self.require(
            host_task_id=binding.host_task_id,
            work_id=binding.work_id,
        )
        if current.state is ComponentCapabilityBindingState.BEGUN:
            if (
                current.escalation_row_version != escalation_row_version
                or current.candidate_state != candidate_state
            ):
                raise ProductFactoryToolsmithBindingError(
                    "Toolsmith begin replay conflicts with durable binding"
                )
            return current
        if current.state is not ComponentCapabilityBindingState.RESERVED:
            raise ProductFactoryToolsmithBindingError(
                "Toolsmith begin can only advance a reserved binding"
            )
        self._cas_update(
            current,
            """
            UPDATE product_factory_toolsmith_bindings
            SET state = ?, escalation_row_version = ?, candidate_state = ?,
                row_version = row_version + 1, updated_at = ?
            WHERE host_task_id = ? AND work_id = ? AND row_version = ?
            """,
            (
                ComponentCapabilityBindingState.BEGUN.value,
                escalation_row_version,
                candidate_state,
                _now(),
                current.host_task_id,
                current.work_id,
                current.row_version,
            ),
        )
        return self.require(host_task_id=current.host_task_id, work_id=current.work_id)

    def prepare_resume(
        self,
        binding: ComponentCapabilityBinding,
        *,
        next_work_id: str,
        pinned_version: str,
        pinned_digest: str,
    ) -> ComponentCapabilityBinding:
        if not all(
            value.strip() for value in (next_work_id, pinned_version, pinned_digest)
        ):
            raise ProductFactoryToolsmithBindingError("resume evidence must be complete")
        current = self.require(
            host_task_id=binding.host_task_id,
            work_id=binding.work_id,
        )
        if current.state is ComponentCapabilityBindingState.RESUME_PREPARED:
            if (
                current.next_work_id != next_work_id
                or current.pinned_version != pinned_version
                or current.pinned_digest != pinned_digest
            ):
                raise ProductFactoryToolsmithBindingError(
                    "prepared resume replay conflicts with durable evidence"
                )
            return current
        if current.state is not ComponentCapabilityBindingState.BEGUN:
            raise ProductFactoryToolsmithBindingError(
                "resume can only be prepared from a begun binding"
            )
        self._cas_update(
            current,
            """
            UPDATE product_factory_toolsmith_bindings
            SET state = ?, next_work_id = ?, pinned_version = ?, pinned_digest = ?,
                row_version = row_version + 1, updated_at = ?
            WHERE host_task_id = ? AND work_id = ? AND row_version = ?
            """,
            (
                ComponentCapabilityBindingState.RESUME_PREPARED.value,
                next_work_id,
                pinned_version,
                pinned_digest,
                _now(),
                current.host_task_id,
                current.work_id,
                current.row_version,
            ),
        )
        return self.require(host_task_id=current.host_task_id, work_id=current.work_id)

    def mark_consumed(
        self,
        binding: ComponentCapabilityBinding,
    ) -> ComponentCapabilityBinding:
        current = self.require(
            host_task_id=binding.host_task_id,
            work_id=binding.work_id,
        )
        if current.state is ComponentCapabilityBindingState.CONSUMED:
            return current
        if current.state is not ComponentCapabilityBindingState.RESUME_PREPARED:
            raise ProductFactoryToolsmithBindingError(
                "only a prepared resume can be consumed"
            )
        self._cas_update(
            current,
            """
            UPDATE product_factory_toolsmith_bindings
            SET state = ?, row_version = row_version + 1, updated_at = ?
            WHERE host_task_id = ? AND work_id = ? AND row_version = ?
            """,
            (
                ComponentCapabilityBindingState.CONSUMED.value,
                _now(),
                current.host_task_id,
                current.work_id,
                current.row_version,
            ),
        )
        return self.require(host_task_id=current.host_task_id, work_id=current.work_id)

    def find_for_request(
        self,
        *,
        host_task_id: str,
        request: ComponentWorkRequest,
    ) -> ComponentCapabilityBinding | None:
        with self._store.connection() as conn:
            self._validate_host_task(conn, host_task_id, request.project_id)
            rows = conn.execute(
                """
                SELECT *
                FROM product_factory_toolsmith_bindings
                WHERE host_task_id = ? AND component_id = ?
                ORDER BY rowid DESC
                """,
                (host_task_id, request.component_id),
            ).fetchall()
        bindings = tuple(_binding_from_row(row) for row in rows)
        matching = tuple(
            item
            for item in bindings
            if item.work_id == request.work_id or item.next_work_id == request.work_id
        )
        if len(matching) > 1:
            raise ProductFactoryToolsmithBindingError(
                "multiple durable capability bindings match one component attempt"
            )
        if matching:
            return matching[0]
        active = tuple(
            item
            for item in bindings
            if item.state is not ComponentCapabilityBindingState.CONSUMED
        )
        if active:
            raise ProductFactoryToolsmithBindingError(
                "active capability gap belongs to a stale component attempt"
            )
        return None

    def require(self, *, host_task_id: str, work_id: str) -> ComponentCapabilityBinding:
        with self._store.connection() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM product_factory_toolsmith_bindings
                WHERE host_task_id = ? AND work_id = ?
                """,
                (host_task_id, work_id),
            ).fetchone()
        if row is None:
            raise ProductFactoryToolsmithBindingError(
                "durable component capability binding is missing"
            )
        return _binding_from_row(row)

    def _initialize_schema(self) -> None:
        with self._store.connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS product_factory_toolsmith_schema_migrations(
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                )
                """
            )
            row = conn.execute(
                "SELECT MAX(version) AS version "
                "FROM product_factory_toolsmith_schema_migrations"
            ).fetchone()
            current = _exact_int(row["version"], "schema version", allow_none=True) or 0
            if current > _SCHEMA_VERSION:
                raise ProductFactoryToolsmithBindingError(
                    "Product Factory Toolsmith binding schema is newer than supported"
                )
            if current < 1:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS product_factory_toolsmith_bindings(
                        host_task_id TEXT NOT NULL,
                        project_id TEXT NOT NULL,
                        component_id TEXT NOT NULL,
                        work_id TEXT NOT NULL,
                        capability_id TEXT NOT NULL,
                        reason TEXT NOT NULL,
                        attempted_methods_json TEXT NOT NULL,
                        permission_ceiling_json TEXT NOT NULL,
                        state TEXT NOT NULL CHECK(
                            state IN ('reserved','begun','resume_prepared','consumed')
                        ),
                        row_version INTEGER NOT NULL CHECK(row_version >= 0),
                        escalation_row_version INTEGER,
                        candidate_state TEXT,
                        next_work_id TEXT,
                        pinned_version TEXT,
                        pinned_digest TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY(host_task_id, work_id),
                        FOREIGN KEY(host_task_id) REFERENCES tasks(task_id)
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_pf_toolsmith_component_state
                    ON product_factory_toolsmith_bindings(
                        host_task_id, component_id, state, updated_at
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO product_factory_toolsmith_schema_migrations(version, applied_at)
                    VALUES (1, ?)
                    """,
                    (_now(),),
                )

    @staticmethod
    def _validate_host_task(
        conn: sqlite3.Connection,
        host_task_id: str,
        project_id: str,
    ) -> None:
        row = conn.execute(
            "SELECT payload_json FROM tasks WHERE task_id = ?",
            (host_task_id,),
        ).fetchone()
        if row is None:
            raise ProductFactoryToolsmithBindingError(
                "Product Factory host task does not exist"
            )
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise ProductFactoryToolsmithBindingError(
                "Product Factory host task payload is corrupt"
            ) from exc
        if not isinstance(payload, dict):
            raise ProductFactoryToolsmithBindingError(
                "Product Factory host task payload is not an object"
            )
        if (
            payload.get("kind") != "product_factory"
            or payload.get("product_project_id") != project_id
        ):
            raise ProductFactoryToolsmithBindingError(
                "host task is not authoritative for this ProductProject"
            )

    @staticmethod
    def _validate_replay(
        *,
        binding: ComponentCapabilityBinding,
        request: ComponentWorkRequest,
        capability_id: str,
        reason: str,
        attempted_methods: tuple[str, ...],
    ) -> None:
        expected = (
            request.project_id,
            request.component_id,
            request.work_id,
            capability_id,
            reason,
            attempted_methods,
            request.permission_ceiling,
        )
        observed = (
            binding.project_id,
            binding.component_id,
            binding.work_id,
            binding.capability_id,
            binding.reason,
            binding.attempted_methods,
            binding.permission_ceiling,
        )
        if observed != expected:
            raise ProductFactoryToolsmithBindingError(
                "durable capability-gap replay conflicts with original binding"
            )

    def _cas_update(
        self,
        binding: ComponentCapabilityBinding,
        statement: str,
        parameters: tuple[object, ...],
    ) -> None:
        with self._store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(statement, parameters)
            if cursor.rowcount != 1:
                raise ProductFactoryToolsmithBindingError(
                    "durable capability binding changed concurrently"
                )


def _binding_from_row(row: sqlite3.Row) -> ComponentCapabilityBinding:
    attempted = _string_tuple(row["attempted_methods_json"], "attempted methods")
    ceiling = frozenset(_string_tuple(row["permission_ceiling_json"], "permission ceiling"))
    return ComponentCapabilityBinding(
        host_task_id=_text(row["host_task_id"], "host task id"),
        project_id=_text(row["project_id"], "project id"),
        component_id=_text(row["component_id"], "component id"),
        work_id=_text(row["work_id"], "work id"),
        capability_id=_text(row["capability_id"], "capability id"),
        reason=_text(row["reason"], "reason"),
        attempted_methods=attempted,
        permission_ceiling=ceiling,
        state=ComponentCapabilityBindingState(_text(row["state"], "state")),
        row_version=_exact_int(row["row_version"], "row version"),
        escalation_row_version=_exact_int(
            row["escalation_row_version"],
            "escalation row version",
            allow_none=True,
        ),
        candidate_state=_optional_text(row["candidate_state"]),
        next_work_id=_optional_text(row["next_work_id"]),
        pinned_version=_optional_text(row["pinned_version"]),
        pinned_digest=_optional_text(row["pinned_digest"]),
    )


def _string_tuple(raw: object, label: str) -> tuple[str, ...]:
    if not isinstance(raw, str):
        raise ProductFactoryToolsmithBindingError(f"{label} JSON is not text")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProductFactoryToolsmithBindingError(f"{label} JSON is corrupt") from exc
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ProductFactoryToolsmithBindingError(f"{label} JSON is invalid")
    return tuple(value)


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProductFactoryToolsmithBindingError(f"{label} is invalid")
    return value


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ProductFactoryToolsmithBindingError("optional durable text is invalid")
    return value


def _exact_int(value: object, label: str, *, allow_none: bool = False) -> int | None:
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProductFactoryToolsmithBindingError(f"{label} is not an exact integer")
    if value < 0:
        raise ProductFactoryToolsmithBindingError(f"{label} must be non-negative")
    return value


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _now() -> str:
    return datetime.now(UTC).isoformat()
