from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from nika_core.activation_authority import ActivationAuthorityPort, ActivationSubject
from nika_core.builder.compiler import CompilationResult
from nika_core.builder.spec import AgentDefinition
from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog


@dataclass(frozen=True, slots=True)
class StoredAgentDefinition:
    definition: AgentDefinition
    status: str
    required_human_approvals: tuple[str, ...]
    highest_risk: int
    created_at: str
    activated_at: str | None


class AgentDefinitionRepository:
    """Versioned durable storage and atomic activation for Agent Builder definitions."""

    def __init__(
        self,
        store: SQLiteStore,
        *,
        audit_log: AuditLog | None = None,
        activation_authority: ActivationAuthorityPort | None = None,
    ) -> None:
        self._store = store
        self._audit_log = audit_log or AuditLog(store)
        self._activation_authority = activation_authority

    def next_version(self, agent_id: str) -> int:
        with self._store.connection() as conn:
            row = conn.execute(
                "SELECT MAX(version) AS version FROM agent_definitions WHERE agent_id = ?",
                (agent_id,),
            ).fetchone()
        return int(row["version"] or 0) + 1

    def save_draft(self, compilation: CompilationResult) -> None:
        definition = compilation.definition
        now = datetime.now(UTC).isoformat()
        payload = definition.model_dump_json()
        approvals_json = json.dumps(
            compilation.required_human_approvals,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        with self._store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            latest = conn.execute(
                "SELECT MAX(version) AS version FROM agent_definitions WHERE agent_id = ?",
                (definition.agent_id,),
            ).fetchone()
            expected = int(latest["version"] or 0) + 1
            if definition.version != expected:
                raise ValueError(
                    f"definition version must be the next immutable version: expected {expected}"
                )
            conn.execute(
                "INSERT INTO agent_definitions("
                "agent_id, version, definition_json, required_approvals_json, highest_risk, "
                "status, created_at, activated_at"
                ") VALUES (?, ?, ?, ?, ?, 'draft', ?, NULL)",
                (
                    definition.agent_id,
                    definition.version,
                    payload,
                    approvals_json,
                    int(compilation.highest_risk),
                    now,
                ),
            )
            self._audit_log.append_with_connection(
                conn,
                event_type="agent_definition.draft_saved",
                entity_type="agent_definition",
                entity_id=f"{definition.agent_id}:{definition.version}",
                payload={
                    "agent_id": definition.agent_id,
                    "version": definition.version,
                    "highest_risk": int(compilation.highest_risk),
                    "required_approvals": list(compilation.required_human_approvals),
                },
            )

    def activate(
        self,
        definition: AgentDefinition,
        *,
        approval_refs: tuple[str, ...] = (),
    ) -> None:
        if not definition.enabled:
            raise ValueError("disabled agent definition cannot be activated")
        with self._store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT definition_json, required_approvals_json, highest_risk, status "
                "FROM agent_definitions WHERE agent_id = ? AND version = ?",
                (definition.agent_id, definition.version),
            ).fetchone()
            if row is None:
                raise KeyError("agent definition draft does not exist")
            persisted = AgentDefinition.model_validate_json(row["definition_json"])
            if persisted != definition:
                raise ValueError("activation definition differs from persisted immutable draft")

            active_row = conn.execute(
                "SELECT version FROM agent_definitions "
                "WHERE agent_id = ? AND status = 'active'",
                (definition.agent_id,),
            ).fetchone()
            if active_row is not None:
                active_version = int(active_row["version"])
                if active_version == definition.version:
                    return
                if active_version > definition.version:
                    raise PermissionError(
                        "stale agent definition cannot replace a newer active version"
                    )

            if row["status"] != "draft":
                raise ValueError(f"cannot activate definition in status {row['status']}")

            required = tuple(
                str(item) for item in json.loads(row["required_approvals_json"])
            )
            declared_highest = max(
                (grant.max_risk for grant in definition.tool_grants),
                default=0,
            )
            if int(row["highest_risk"]) != declared_highest:
                raise PermissionError(
                    "persisted risk metadata does not match immutable agent definition"
                )
            declared_high_impact = tuple(
                sorted(
                    grant.tool_id
                    for grant in definition.tool_grants
                    if grant.max_risk == 4
                )
            )
            if required != declared_high_impact:
                raise PermissionError(
                    "persisted high-impact approval metadata does not match definition"
                )
            if required:
                if self._activation_authority is None:
                    raise PermissionError(
                        "trusted activation authority is required for high-impact tools"
                    )
                subject = ActivationSubject.from_payload(
                    kind="agent",
                    subject_id=definition.agent_id,
                    version=str(definition.version),
                    payload=definition.model_dump(mode="json"),
                    high_impact_ids=required,
                )
                self._activation_authority.verify(subject, approval_refs)

            now = datetime.now(UTC).isoformat()
            conn.execute(
                "UPDATE agent_definitions SET status = 'retired' "
                "WHERE agent_id = ? AND status = 'active'",
                (definition.agent_id,),
            )
            conn.execute(
                "UPDATE agent_definitions SET status = 'active', activated_at = ? "
                "WHERE agent_id = ? AND version = ?",
                (now, definition.agent_id, definition.version),
            )
            self._audit_log.append_with_connection(
                conn,
                event_type="agent_definition.activated",
                entity_type="agent_definition",
                entity_id=f"{definition.agent_id}:{definition.version}",
                payload={
                    "agent_id": definition.agent_id,
                    "version": definition.version,
                    "approval_reference_count": len(approval_refs),
                    "highest_risk": int(row["highest_risk"]),
                },
            )

    def active(self, agent_id: str) -> StoredAgentDefinition | None:
        with self._store.connection() as conn:
            row = conn.execute(
                "SELECT definition_json, status, required_approvals_json, highest_risk, "
                "created_at, activated_at FROM agent_definitions "
                "WHERE agent_id = ? AND status = 'active' ORDER BY version DESC LIMIT 1",
                (agent_id,),
            ).fetchone()
        return self._decode(row) if row is not None else None

    def require_active(self, agent_id: str, version: int) -> StoredAgentDefinition:
        """Return the exact active definition or fail closed."""
        stored = self.get(agent_id, version)
        if stored is None:
            raise KeyError(f"unknown agent definition: {agent_id}:{version}")
        if stored.status != "active":
            raise PermissionError(f"agent definition is not active: {agent_id}:{version}")
        if not stored.definition.enabled:
            raise PermissionError(f"agent definition is disabled: {agent_id}:{version}")
        return stored

    def get(self, agent_id: str, version: int) -> StoredAgentDefinition | None:
        with self._store.connection() as conn:
            row = conn.execute(
                "SELECT definition_json, status, required_approvals_json, highest_risk, "
                "created_at, activated_at FROM agent_definitions "
                "WHERE agent_id = ? AND version = ?",
                (agent_id, version),
            ).fetchone()
        return self._decode(row) if row is not None else None

    @staticmethod
    def _decode(row) -> StoredAgentDefinition:
        payload = json.loads(row["definition_json"])
        required = tuple(
            str(item) for item in json.loads(row["required_approvals_json"])
        )
        return StoredAgentDefinition(
            definition=AgentDefinition.model_validate(payload),
            status=str(row["status"]),
            required_human_approvals=required,
            highest_risk=int(row["highest_risk"]),
            created_at=str(row["created_at"]),
            activated_at=(
                str(row["activated_at"]) if row["activated_at"] is not None else None
            ),
        )
