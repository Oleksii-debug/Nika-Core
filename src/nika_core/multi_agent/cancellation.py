from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog
from nika_core.multi_agent.contracts import (
    CancellationEffect,
    CancellationEffectState,
    CancellationOperation,
    CancellationOperationState,
)

if TYPE_CHECKING:
    from nika_core.multi_agent.store import MultiAgentStore


MULTI_AGENT_CANCELLATION_SCHEMA_VERSION = 1

_CANCELLATION_MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: (
        """CREATE TABLE IF NOT EXISTS multi_agent_cancellations (
            operation_id TEXT PRIMARY KEY,
            team_id TEXT NOT NULL UNIQUE,
            state TEXT NOT NULL CHECK(state IN ('cancelling','reconcile_required','completed')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(team_id) REFERENCES multi_agent_teams(team_id)
        )""",
        """CREATE TABLE IF NOT EXISTS multi_agent_cancellation_effects (
            operation_id TEXT NOT NULL,
            team_id TEXT NOT NULL,
            member_id TEXT NOT NULL,
            task_id TEXT NOT NULL,
            thread_id TEXT NOT NULL,
            sequence INTEGER NOT NULL CHECK(sequence >= 0),
            state TEXT NOT NULL CHECK(state IN (
                'planned','dispatching','confirmed','reconcile_required'
            )),
            error_type TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(operation_id, member_id),
            UNIQUE(operation_id, sequence),
            FOREIGN KEY(operation_id) REFERENCES multi_agent_cancellations(operation_id),
            FOREIGN KEY(team_id, member_id) REFERENCES multi_agent_members(team_id, member_id)
        )""",
        """CREATE INDEX IF NOT EXISTS idx_multi_agent_cancel_effect_state
            ON multi_agent_cancellation_effects(operation_id, state, sequence)""",
    ),
}


class TeamCancellationJournal:
    """Durable M7 cancellation intent/effect ledger in Nika's canonical SQLite database."""

    def __init__(self, team_store: MultiAgentStore) -> None:
        sqlite = getattr(team_store, "_store", None)
        if not isinstance(sqlite, SQLiteStore):
            raise TypeError("MultiAgentStore must use the canonical SQLiteStore")
        self._store = sqlite
        self._audit = AuditLog(sqlite)
        self._initialize_schema()

    def get(self, team_id: str) -> CancellationOperation | None:
        with self._store.connection() as conn:
            return self._load_with_connection(conn, team_id)

    def has_unfinished(self, team_id: str) -> bool:
        operation = self.get(team_id)
        return operation is not None and operation.state is not CancellationOperationState.COMPLETED

    def begin(self, *, team_id: str) -> CancellationOperation:
        """Atomically persist cancellation authority and terminal local member state."""
        operation_id = self._operation_id(team_id)
        now = datetime.now(UTC).isoformat()
        with self._store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = self._load_with_connection(conn, team_id)
            if current is not None:
                return current
            team = conn.execute(
                "SELECT state FROM multi_agent_teams WHERE team_id = ?",
                (team_id,),
            ).fetchone()
            if team is None:
                raise KeyError(f"unknown team: {team_id}")
            if team["state"] != "active":
                raise RuntimeError("new cancellation intent requires an active team")
            target_rows = conn.execute(
                "SELECT member_id, thread_id FROM multi_agent_members "
                "WHERE team_id = ? AND state IN ('spawned','running','waiting_approval') "
                "ORDER BY depth, created_at, member_id",
                (team_id,),
            ).fetchall()
            conn.execute(
                "INSERT INTO multi_agent_cancellations("
                "operation_id, team_id, state, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (
                    operation_id,
                    team_id,
                    CancellationOperationState.CANCELLING.value,
                    now,
                    now,
                ),
            )
            for sequence, member in enumerate(target_rows):
                member_id = str(member["member_id"])
                thread_id = str(member["thread_id"])
                conn.execute(
                    "INSERT INTO multi_agent_cancellation_effects("
                    "operation_id, team_id, member_id, task_id, thread_id, sequence, state, "
                    "error_type, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        operation_id,
                        team_id,
                        member_id,
                        self._task_id(team_id, member_id),
                        thread_id,
                        sequence,
                        CancellationEffectState.PLANNED.value,
                        None,
                        now,
                    ),
                )
            conn.execute(
                "UPDATE multi_agent_teams SET state = ?, updated_at = ? WHERE team_id = ?",
                ("cancelled", now, team_id),
            )
            conn.execute(
                "UPDATE multi_agent_members SET state = ?, updated_at = ? WHERE team_id = ? "
                "AND state IN ('spawned','running','waiting_approval')",
                ("cancelled", now, team_id),
            )
            self._audit.append_with_connection(
                conn,
                event_type="multi_agent.team_cancel_requested",
                entity_type="multi_agent_team",
                entity_id=team_id,
                payload={"operation_id": operation_id, "target_count": len(target_rows)},
            )
        operation = self.get(team_id)
        if operation is None:
            raise RuntimeError("cancellation operation disappeared after commit")
        return operation

    def mark_dispatching(self, operation_id: str, member_id: str) -> None:
        self._transition_effect(
            operation_id=operation_id,
            member_id=member_id,
            allowed=(CancellationEffectState.PLANNED,),
            state=CancellationEffectState.DISPATCHING,
            error_type=None,
            event_type="multi_agent.cancel_effect_dispatching",
        )

    def mark_confirmed(self, operation_id: str, member_id: str) -> None:
        self._transition_effect(
            operation_id=operation_id,
            member_id=member_id,
            allowed=(
                CancellationEffectState.DISPATCHING,
                CancellationEffectState.RECONCILE_REQUIRED,
            ),
            state=CancellationEffectState.CONFIRMED,
            error_type=None,
            event_type="multi_agent.cancel_effect_confirmed",
        )

    def mark_reconcile_required(
        self,
        operation_id: str,
        member_id: str,
        *,
        error_type: str,
    ) -> None:
        if not error_type.strip():
            raise ValueError("cancellation reconciliation error type must not be empty")
        self._transition_effect(
            operation_id=operation_id,
            member_id=member_id,
            allowed=(CancellationEffectState.DISPATCHING,),
            state=CancellationEffectState.RECONCILE_REQUIRED,
            error_type=error_type,
            event_type="multi_agent.cancel_effect_reconcile_required",
        )

    def mark_not_cancelled(self, operation_id: str, member_id: str) -> None:
        self._transition_effect(
            operation_id=operation_id,
            member_id=member_id,
            allowed=(
                CancellationEffectState.DISPATCHING,
                CancellationEffectState.RECONCILE_REQUIRED,
            ),
            state=CancellationEffectState.PLANNED,
            error_type=None,
            event_type="multi_agent.cancel_effect_not_cancelled",
        )

    def complete(self, operation_id: str) -> CancellationOperation:
        now = datetime.now(UTC).isoformat()
        with self._store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT team_id, state FROM multi_agent_cancellations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown cancellation operation: {operation_id}")
            team_id = str(row["team_id"])
            states = conn.execute(
                "SELECT state FROM multi_agent_cancellation_effects WHERE operation_id = ?",
                (operation_id,),
            ).fetchall()
            if any(item["state"] != CancellationEffectState.CONFIRMED.value for item in states):
                raise RuntimeError("cancellation operation still has unconfirmed external effects")
            conn.execute(
                "UPDATE multi_agent_cancellations SET state = ?, updated_at = ? "
                "WHERE operation_id = ?",
                (CancellationOperationState.COMPLETED.value, now, operation_id),
            )
            self._audit.append_with_connection(
                conn,
                event_type="multi_agent.team_cancel_effects_completed",
                entity_type="multi_agent_team",
                entity_id=team_id,
                payload={"operation_id": operation_id},
            )
        operation = self.get(team_id)
        if operation is None:
            raise RuntimeError("completed cancellation operation disappeared")
        return operation

    def _transition_effect(
        self,
        *,
        operation_id: str,
        member_id: str,
        allowed: tuple[CancellationEffectState, ...],
        state: CancellationEffectState,
        error_type: str | None,
        event_type: str,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        with self._store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT team_id, state FROM multi_agent_cancellation_effects "
                "WHERE operation_id = ? AND member_id = ?",
                (operation_id, member_id),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown cancellation effect: {operation_id}/{member_id}")
            current = CancellationEffectState(row["state"])
            if current not in allowed:
                raise RuntimeError(
                    f"cancellation effect cannot move from {current.value} to {state.value}"
                )
            team_id = str(row["team_id"])
            conn.execute(
                "UPDATE multi_agent_cancellation_effects SET state = ?, error_type = ?, "
                "updated_at = ? WHERE operation_id = ? AND member_id = ?",
                (state.value, error_type, now, operation_id, member_id),
            )
            self._refresh_operation_state(conn, operation_id, now)
            self._audit.append_with_connection(
                conn,
                event_type=event_type,
                entity_type="multi_agent_team",
                entity_id=team_id,
                payload={
                    "operation_id": operation_id,
                    "member_id": member_id,
                    "state": state.value,
                    "error_type": error_type,
                },
            )

    @staticmethod
    def _refresh_operation_state(
        conn: sqlite3.Connection,
        operation_id: str,
        now: str,
    ) -> None:
        rows = conn.execute(
            "SELECT state FROM multi_agent_cancellation_effects WHERE operation_id = ?",
            (operation_id,),
        ).fetchall()
        states = {CancellationEffectState(row["state"]) for row in rows}
        if CancellationEffectState.RECONCILE_REQUIRED in states:
            state = CancellationOperationState.RECONCILE_REQUIRED
        else:
            state = CancellationOperationState.CANCELLING
        conn.execute(
            "UPDATE multi_agent_cancellations SET state = ?, updated_at = ? "
            "WHERE operation_id = ? AND state != ?",
            (
                state.value,
                now,
                operation_id,
                CancellationOperationState.COMPLETED.value,
            ),
        )

    def _load_with_connection(
        self,
        conn: sqlite3.Connection,
        team_id: str,
    ) -> CancellationOperation | None:
        row = conn.execute(
            "SELECT operation_id, team_id, state FROM multi_agent_cancellations "
            "WHERE team_id = ?",
            (team_id,),
        ).fetchone()
        if row is None:
            return None
        operation_id = str(row["operation_id"])
        if operation_id != self._operation_id(team_id):
            raise RuntimeError("cancellation operation identity is corrupt")
        effect_rows = conn.execute(
            "SELECT operation_id, team_id, member_id, task_id, thread_id, sequence, state, "
            "error_type FROM multi_agent_cancellation_effects WHERE operation_id = ? "
            "ORDER BY sequence",
            (operation_id,),
        ).fetchall()
        effects = tuple(
            CancellationEffect(
                operation_id=str(item["operation_id"]),
                team_id=str(item["team_id"]),
                member_id=str(item["member_id"]),
                task_id=str(item["task_id"]),
                thread_id=str(item["thread_id"]),
                sequence=int(item["sequence"]),
                state=CancellationEffectState(item["state"]),
                error_type=item["error_type"],
            )
            for item in effect_rows
        )
        operation = CancellationOperation(
            operation_id=operation_id,
            team_id=str(row["team_id"]),
            state=CancellationOperationState(row["state"]),
            effects=effects,
        )
        self._validate_operation(conn, operation)
        return operation

    def _validate_operation(
        self,
        conn: sqlite3.Connection,
        operation: CancellationOperation,
    ) -> None:
        if operation.team_id.strip() == "":
            raise RuntimeError("cancellation team identity is corrupt")
        seen_sequences: set[int] = set()
        for effect in operation.effects:
            if effect.team_id != operation.team_id or effect.operation_id != operation.operation_id:
                raise RuntimeError("cancellation effect lineage is corrupt")
            if effect.sequence in seen_sequences:
                raise RuntimeError("cancellation effect sequence is duplicated")
            seen_sequences.add(effect.sequence)
            if effect.task_id != self._task_id(effect.team_id, effect.member_id):
                raise RuntimeError("cancellation task identity is corrupt")
            member = conn.execute(
                "SELECT thread_id FROM multi_agent_members WHERE team_id = ? AND member_id = ?",
                (effect.team_id, effect.member_id),
            ).fetchone()
            if member is None or str(member["thread_id"]) != effect.thread_id:
                raise RuntimeError("cancellation member/thread identity is corrupt")
        states = {effect.state for effect in operation.effects}
        if operation.state is CancellationOperationState.COMPLETED:
            if any(state is not CancellationEffectState.CONFIRMED for state in states):
                raise RuntimeError("completed cancellation contains unconfirmed effects")
        if operation.state is CancellationOperationState.RECONCILE_REQUIRED:
            if CancellationEffectState.RECONCILE_REQUIRED not in states:
                raise RuntimeError("reconcile-required cancellation has no uncertain effect")
        if operation.state is CancellationOperationState.CANCELLING:
            if CancellationEffectState.RECONCILE_REQUIRED in states:
                raise RuntimeError("cancelling operation hides a reconcile-required effect")

    def _initialize_schema(self) -> None:
        with self._store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS multi_agent_cancellation_schema_migrations ("
                "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            row = conn.execute(
                "SELECT MAX(version) AS version FROM multi_agent_cancellation_schema_migrations"
            ).fetchone()
            current = int(row["version"] or 0)
            if current > MULTI_AGENT_CANCELLATION_SCHEMA_VERSION:
                raise RuntimeError(
                    "multi-agent cancellation schema "
                    f"{current} is newer than supported schema "
                    f"{MULTI_AGENT_CANCELLATION_SCHEMA_VERSION}"
                )
            for version in range(current + 1, MULTI_AGENT_CANCELLATION_SCHEMA_VERSION + 1):
                statements = _CANCELLATION_MIGRATIONS.get(version)
                if statements is None:
                    raise RuntimeError(f"missing multi-agent cancellation migration {version}")
                for statement in statements:
                    conn.execute(statement)
                conn.execute(
                    "INSERT INTO multi_agent_cancellation_schema_migrations(version, applied_at) "
                    "VALUES (?, ?)",
                    (version, datetime.now(UTC).isoformat()),
                )

    @staticmethod
    def _operation_id(team_id: str) -> str:
        return f"cancel:{team_id}"

    @staticmethod
    def _task_id(team_id: str, member_id: str) -> str:
        return f"team:{team_id}:{member_id}"
