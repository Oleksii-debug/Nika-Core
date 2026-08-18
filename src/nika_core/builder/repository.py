from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from nika_core.builder.spec import AgentDefinition
from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog


@dataclass(frozen=True, slots=True)
class StoredAgentDefinition:
    definition: AgentDefinition
    status: str
    created_at: str
    activated_at: str | None


class AgentDefinitionRepository:
    """Versioned durable storage and atomic activation for Agent Builder definitions."""

    def __init__(self, store: SQLiteStore, *, audit_log: AuditLog | None = None) -> None:
        self._store = store
        self._audit_log = audit_log or AuditLog(store)

    def next_version(self, agent_id: str) -> int:
        with self._store.connection() as conn:
            row = conn.execute(
                "SELECT MAX(version) AS version FROM agent_definitions WHERE agent_id = ?",
                (agent_id,),
            ).fetchone()
        return int(row["version"] or 0) + 1

    def save_draft(self, definition: AgentDefinition) -> None:
        now = datetime.now(UTC).isoformat()
        payload = definition.model_dump_json()
        with self._store.connection() as conn:
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
                "agent_id, version, definition_json, status, created_at, activated_at"
                ") VALUES (?, ?, ?, 'draft', ?, NULL)",
                (definition.agent_id, definition.version, payload, now),
            )
            self._audit_log.append_with_connection(
                conn,
                event_type="agent_definition.draft_saved",
                entity_type="agent_definition",
                entity_id=f"{definition.agent_id}:{definition.version}",
                payload={"agent_id": definition.agent_id, "version": definition.version},
            )

    def activate(
        self,
        definition: AgentDefinition,
        *,
        required_human_approvals: tuple[str, ...] = (),
        approved_tool_ids: frozenset[str] = frozenset(),
    ) -> None:
        missing = sorted(set(required_human_approvals) - set(approved_tool_ids))
        if missing:
            raise PermissionError(
                "explicit human approval required for high-impact tools: " + ", ".join(missing)
            )
        now = datetime.now(UTC).isoformat()
        with self._store.connection() as conn:
            row = conn.execute(
                "SELECT definition_json, status FROM agent_definitions "
                "WHERE agent_id = ? AND version = ?",
                (definition.agent_id, definition.version),
            ).fetchone()
            if row is None:
                raise KeyError("agent definition draft does not exist")
            persisted = AgentDefinition.model_validate_json(row["definition_json"])
            if persisted != definition:
                raise ValueError("activation definition differs from persisted immutable draft")
            if row["status"] == "active":
                return
            if row["status"] != "draft":
                raise ValueError(f"cannot activate definition in status {row['status']}")
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
                    "approved_high_impact_tools": sorted(approved_tool_ids),
                },
            )

    def active(self, agent_id: str) -> StoredAgentDefinition | None:
        with self._store.connection() as conn:
            row = conn.execute(
                "SELECT definition_json, status, created_at, activated_at "
                "FROM agent_definitions WHERE agent_id = ? AND status = 'active' "
                "ORDER BY version DESC LIMIT 1",
                (agent_id,),
            ).fetchone()
        return self._decode(row) if row is not None else None

    def get(self, agent_id: str, version: int) -> StoredAgentDefinition | None:
        with self._store.connection() as conn:
            row = conn.execute(
                "SELECT definition_json, status, created_at, activated_at "
                "FROM agent_definitions WHERE agent_id = ? AND version = ?",
                (agent_id, version),
            ).fetchone()
        return self._decode(row) if row is not None else None

    @staticmethod
    def _decode(row) -> StoredAgentDefinition:
        payload = json.loads(row["definition_json"])
        return StoredAgentDefinition(
            definition=AgentDefinition.model_validate(payload),
            status=str(row["status"]),
            created_at=str(row["created_at"]),
            activated_at=str(row["activated_at"]) if row["activated_at"] is not None else None,
        )
