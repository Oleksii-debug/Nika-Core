from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from nika_core.builder.compiler import CompilationResult, RiskTier
from nika_core.builder.spec import AgentDefinition
from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog


def _expected_risk_evidence(definition: AgentDefinition) -> tuple[int, tuple[str, ...]]:
    highest = max((grant.max_risk for grant in definition.tool_grants), default=0)
    approvals = tuple(
        sorted(
            grant.tool_id
            for grant in definition.tool_grants
            if grant.max_risk == RiskTier.R4_HIGH_IMPACT
        )
    )
    return highest, approvals


def _validate_persisted_risk_evidence(
    definition: AgentDefinition, *, highest_risk: object, approvals_json: object
) -> tuple[str, ...]:
    try:
        approvals = json.loads(approvals_json)
    except (TypeError, ValueError) as exc:
        raise ValueError("persisted agent risk/approval evidence is invalid") from exc
    expected_risk, expected_approvals = _expected_risk_evidence(definition)
    if (
        type(highest_risk) is not int
        or highest_risk != expected_risk
        or type(approvals) is not list
        or any(type(item) is not str for item in approvals)
        or tuple(approvals) != expected_approvals
    ):
        raise ValueError("persisted agent risk/approval evidence is inconsistent")
    return expected_approvals


def _require_active_receipt(value: object) -> None:
    """Admit only the canonical UTC receipt written by an approved activation."""
    if type(value) is not str:
        raise ValueError("active agent definition lacks valid activation evidence")
    try:
        instant = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("active agent definition lacks valid activation evidence") from exc
    # A nonempty but forged/legacy-garbled date, a naive local clock or a
    # non-UTC offset cannot stand in for the durable UTC activation receipt.
    if instant.tzinfo != UTC or instant.isoformat() != value:
        raise ValueError("active agent definition lacks valid activation evidence")


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

    def save_draft(self, compilation: CompilationResult) -> None:
        # A subclass may override accessors and substitute approval evidence
        # between validation and persistence. Admit only the inert port result.
        if type(compilation) is not CompilationResult:
            raise TypeError("compilation must be a plain CompilationResult")
        # A frozen CompilationResult is not an authority boundary: callers may construct
        # or mutate it after compilation. Fail closed before persisting risk/approval
        # metadata that activation later treats as durable authorization evidence.
        # Hold one inert definition reference: behavioral model subclasses can
        # override serialization and launder caller-controlled approval data.
        source_definition = compilation.definition
        if type(source_definition) is not AgentDefinition:
            raise TypeError("compiled definition must be a plain AgentDefinition")
        definition = AgentDefinition.model_validate(
            source_definition.model_dump(mode="python")
        )
        highest, approvals = _expected_risk_evidence(definition)
        if (
            type(compilation.highest_risk) is not RiskTier
            or compilation.highest_risk.value != highest
            or type(compilation.required_human_approvals) is not tuple
            or any(type(tool_id) is not str for tool_id in compilation.required_human_approvals)
            or compilation.required_human_approvals != approvals
        ):
            raise ValueError("compiled agent risk/approval evidence is inconsistent")
        # Snapshot the recomputed trusted evidence once. A caller sharing the
        # shallow-frozen compilation may still mutate it via object.__setattr__
        # after the check, so no subsequent SQLite/audit field may reread it.
        admitted_risk, admitted_approvals = highest, approvals
        now = datetime.now(UTC).isoformat()
        payload = definition.model_dump_json()
        # Durable drafts must pass the same bounded, unambiguous JSON ingress as
        # restart/activation; otherwise save succeeds but no readback is possible.
        AgentDefinition.import_json(payload)
        approvals_json = json.dumps(
            admitted_approvals,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        with self._store.connection() as conn:
            # Serialize version admission before inspecting the latest durable version.
            # A concurrent caller must see the committed winner, not race the INSERT.
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
                    admitted_risk,
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
                    "highest_risk": admitted_risk,
                    "required_approvals": list(admitted_approvals),
                },
            )

    def activate(
        self,
        definition: AgentDefinition,
        *,
        approved_tool_ids: frozenset[str] = frozenset(),
    ) -> None:
        # Caller-owned approval carriers must not supply custom hash/equality hooks
        # that can impersonate a distinct high-impact tool during set subtraction.
        if type(approved_tool_ids) is not frozenset or any(
            type(tool_id) is not str for tool_id in approved_tool_ids
        ):
            raise TypeError("approved tool IDs must be a frozenset of plain strings")
        # A model_copy(update=...) or frozen-object mutation can bypass Pydantic
        # until we re-admit the incoming definition before comparing it to SQLite.
        if type(definition) is not AgentDefinition:
            raise TypeError("activation definition must be a plain AgentDefinition")
        definition = AgentDefinition.model_validate(definition.model_dump(mode="python"))
        if not definition.enabled:
            raise ValueError("disabled agent definition cannot be activated")
        now = datetime.now(UTC).isoformat()
        with self._store.connection() as conn:
            # Serialize activation/retirement across processes and SQLite connections.
            # Verification, approval admission and the active-version swap are atomic.
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT definition_json, required_approvals_json, highest_risk, status, activated_at "
                "FROM agent_definitions "
                "WHERE agent_id = ? AND version = ?",
                (definition.agent_id, definition.version),
            ).fetchone()
            if row is None:
                raise KeyError("agent definition draft does not exist")
            persisted = AgentDefinition.import_json(row["definition_json"])
            if persisted != definition:
                raise ValueError("activation definition differs from persisted immutable draft")
            required = _validate_persisted_risk_evidence(
                persisted,
                highest_risk=row["highest_risk"],
                approvals_json=row["required_approvals_json"],
            )
            # Approval tokens are scoped to this exact immutable definition.
            # Extra IDs must not become durable audit evidence of approvals for
            # capabilities the reviewed definition never requested. This also
            # applies to lost-ACK retries, without requiring the original token.
            unexpected = sorted(approved_tool_ids.difference(required))
            if unexpected:
                raise PermissionError(
                    "unrequested high-impact tool approvals: " + ", ".join(unexpected)
                )
            # A verified active record is the durable result of an earlier authorized
            # activation. A lost acknowledgement may cause a caller to retry without
            # resending its one-time approval: do not reauthorize or repeat the effect.
            # The persisted document and risk evidence are checked above first.
            if row["status"] == "active":
                # An active marker without its durable activation receipt is not
                # proof of a committed, approved transition after restart.
                _require_active_receipt(row["activated_at"])
                return
            missing = sorted(set(required) - set(approved_tool_ids))
            if missing:
                raise PermissionError(
                    "explicit human approval required for high-impact tools: " + ", ".join(missing)
                )
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
                "SELECT definition_json, status, required_approvals_json, highest_risk, "
                "created_at, activated_at FROM agent_definitions "
                "WHERE agent_id = ? AND status = 'active' ORDER BY version DESC LIMIT 1",
                (agent_id,),
            ).fetchone()
        return self._decode(row) if row is not None else None

    def require_active(self, agent_id: str, version: int) -> StoredAgentDefinition:
        """Return the exact active definition or fail closed.

        Multi-agent execution must never be able to name an arbitrary draft, retired,
        disabled or nonexistent Agent Builder document and have it treated as runnable.
        """
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
        payload = AgentDefinition.import_json(row["definition_json"])
        required = _validate_persisted_risk_evidence(
            payload,
            highest_risk=row["highest_risk"],
            approvals_json=row["required_approvals_json"],
        )
        if row["status"] == "active":
            _require_active_receipt(row["activated_at"])
        return StoredAgentDefinition(
            definition=payload,
            status=str(row["status"]),
            required_human_approvals=required,
            highest_risk=int(row["highest_risk"]),
            created_at=str(row["created_at"]),
            activated_at=str(row["activated_at"]) if row["activated_at"] is not None else None,
        )