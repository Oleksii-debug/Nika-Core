from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime

from nika_core.builder.spec import ToolGrant
from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog
from nika_core.multi_agent.contracts import (
    AgentHandoff,
    ChildRequest,
    HandoffKind,
    MemberState,
    TeamMember,
    TeamQuota,
    TeamState,
    attenuate_grants,
)

_NONTERMINAL_MEMBER_STATES = frozenset(
    {
        MemberState.SPAWNED,
        MemberState.RUNNING,
        MemberState.WAITING_APPROVAL,
    }
)
_TERMINAL_MEMBER_STATES = frozenset(
    {
        MemberState.COMPLETED,
        MemberState.FAILED,
        MemberState.CANCELLED,
    }
)


class MultiAgentStore:
    """Durable Nika-owned team identity, lineage and evidence store."""

    def __init__(self, store: SQLiteStore) -> None:
        self._store = store
        self._audit = AuditLog(store)

    def create_team(
        self,
        *,
        team_id: str,
        root_member_id: str,
        root_agent_id: str,
        root_agent_version: int,
        root_thread_id: str,
        root_grants: tuple[ToolGrant, ...],
        quota: TeamQuota,
    ) -> TeamMember:
        now = datetime.now(UTC).isoformat()
        root = TeamMember(
            team_id=team_id,
            member_id=root_member_id,
            parent_id=None,
            depth=0,
            agent_id=root_agent_id,
            agent_version=root_agent_version,
            thread_id=root_thread_id,
            tool_grants=root_grants,
        )
        with self._store.connection() as conn:
            conn.execute(
                "INSERT INTO multi_agent_teams(team_id, root_member_id, state, quota_json, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    team_id,
                    root_member_id,
                    TeamState.ACTIVE.value,
                    json.dumps(asdict(quota), sort_keys=True, separators=(",", ":")),
                    now,
                    now,
                ),
            )
            self._insert_member(conn, root, now)
            self._audit.append_with_connection(
                conn,
                event_type="multi_agent.team_created",
                entity_type="multi_agent_team",
                entity_id=team_id,
                payload={"root_member_id": root_member_id},
            )
        return root

    def team_state(self, team_id: str) -> TeamState:
        with self._store.connection() as conn:
            row = conn.execute(
                "SELECT state FROM multi_agent_teams WHERE team_id = ?",
                (team_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown team: {team_id}")
        return TeamState(row["state"])

    def quota(self, team_id: str) -> TeamQuota:
        with self._store.connection() as conn:
            row = conn.execute(
                "SELECT quota_json FROM multi_agent_teams WHERE team_id = ?",
                (team_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown team: {team_id}")
        return TeamQuota(**json.loads(row["quota_json"]))

    def member(self, team_id: str, member_id: str) -> TeamMember:
        with self._store.connection() as conn:
            row = self._member_row(conn, team_id=team_id, member_id=member_id)
        if row is None:
            raise KeyError(f"unknown team member: {team_id}/{member_id}")
        return self._member_from_row(row)

    def members(self, team_id: str) -> tuple[TeamMember, ...]:
        with self._store.connection() as conn:
            rows = conn.execute(
                "SELECT team_id, member_id, parent_id, depth, agent_id, agent_version, thread_id, "
                "tool_grants_json, state, resume_token FROM multi_agent_members "
                "WHERE team_id = ? ORDER BY depth, created_at, member_id",
                (team_id,),
            ).fetchall()
        return tuple(self._member_from_row(row) for row in rows)

    def spawn_child(
        self,
        *,
        team_id: str,
        parent_id: str,
        child_id: str,
        agent_id: str,
        agent_version: int,
        thread_id: str,
        requested_grants: tuple[ToolGrant, ...],
        task_handoff: AgentHandoff | None = None,
    ) -> TeamMember:
        return self.spawn_children(
            team_id=team_id,
            parent_id=parent_id,
            requests=(
                ChildRequest(
                    member_id=child_id,
                    agent_id=agent_id,
                    agent_version=agent_version,
                    thread_id=thread_id,
                    requested_grants=requested_grants,
                ),
            ),
            task_handoffs=(task_handoff,),
        )[0]

    def spawn_children(
        self,
        *,
        team_id: str,
        parent_id: str,
        requests: tuple[ChildRequest, ...],
        task_handoffs: tuple[AgentHandoff | None, ...] | None = None,
    ) -> tuple[TeamMember, ...]:
        """Atomically admit and persist one fan-out wave.

        Aggregate depth/parent/total quotas are checked under the same SQLite writer lock used
        for every child, TASK handoff and audit event. Any late constraint/handoff failure rolls
        back the whole wave, so restart can never observe a partially admitted fan-out batch.
        """
        if not requests:
            return ()
        handoffs = task_handoffs
        if handoffs is None:
            handoffs = (None,) * len(requests)
        if len(handoffs) != len(requests):
            raise ValueError("task handoff count must match child request count")
        member_ids = [request.member_id for request in requests]
        if len(member_ids) != len(set(member_ids)):
            raise ValueError("child member IDs must be unique within one fan-out batch")
        thread_ids = [request.thread_id for request in requests]
        if len(thread_ids) != len(set(thread_ids)):
            raise ValueError("child thread IDs must be unique within one fan-out batch")

        with self._store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            now = datetime.now(UTC).isoformat()
            team = conn.execute(
                "SELECT state, quota_json FROM multi_agent_teams WHERE team_id = ?",
                (team_id,),
            ).fetchone()
            if team is None:
                raise KeyError(f"unknown team: {team_id}")
            if team["state"] != TeamState.ACTIVE.value:
                raise RuntimeError("team is not active")
            quota = TeamQuota(**json.loads(team["quota_json"]))
            parent_row = self._member_row(conn, team_id=team_id, member_id=parent_id)
            if parent_row is None:
                raise KeyError(f"unknown parent: {parent_id}")
            parent = self._member_from_row(parent_row)
            if parent.depth + 1 > quota.max_depth:
                raise RuntimeError("spawn depth quota exceeded")
            for thread_id in thread_ids:
                existing_thread = conn.execute(
                    "SELECT 1 FROM multi_agent_members WHERE team_id = ? AND thread_id = ?",
                    (team_id, thread_id),
                ).fetchone()
                if existing_thread is not None:
                    raise ValueError(f"child thread_id already exists in team: {thread_id}")

            batch_size = len(requests)
            child_count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM multi_agent_members "
                    "WHERE team_id = ? AND parent_id = ?",
                    (team_id, parent_id),
                ).fetchone()[0]
            )
            if child_count + batch_size > quota.max_children_per_parent:
                raise RuntimeError("fan-out batch exceeds remaining children-per-parent quota")
            total_count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM multi_agent_members WHERE team_id = ?",
                    (team_id,),
                ).fetchone()[0]
            )
            if total_count + batch_size > quota.max_total_agents:
                raise RuntimeError("fan-out batch exceeds remaining total-agent quota")

            children: list[TeamMember] = []
            for request, handoff in zip(requests, handoffs, strict=True):
                grants = attenuate_grants(parent.tool_grants, request.requested_grants)
                child = TeamMember(
                    team_id=team_id,
                    member_id=request.member_id,
                    parent_id=parent_id,
                    depth=parent.depth + 1,
                    agent_id=request.agent_id,
                    agent_version=request.agent_version,
                    thread_id=request.thread_id,
                    tool_grants=grants,
                )
                if handoff is not None:
                    self._validate_task_handoff(
                        handoff,
                        team_id=team_id,
                        parent_id=parent_id,
                        child_id=request.member_id,
                    )
                children.append(child)

            for child, handoff in zip(children, handoffs, strict=True):
                self._insert_member(conn, child, now)
                if handoff is not None:
                    self._insert_handoff_with_connection(conn, handoff, now)
                self._audit.append_with_connection(
                    conn,
                    event_type="multi_agent.child_spawned",
                    entity_type="multi_agent_team",
                    entity_id=team_id,
                    payload={
                        "parent_id": parent_id,
                        "child_id": child.member_id,
                        "depth": child.depth,
                    },
                )
        return tuple(children)

    def task_payload(self, team_id: str, member_id: str) -> dict[str, object]:
        with self._store.connection() as conn:
            rows = conn.execute(
                "SELECT payload_json FROM multi_agent_handoffs "
                "WHERE team_id = ? AND recipient_id = ? AND kind = ? "
                "ORDER BY created_at, handoff_id LIMIT 2",
                (team_id, member_id, HandoffKind.TASK.value),
            ).fetchall()
        if not rows:
            raise KeyError(f"no persisted task handoff for {team_id}/{member_id}")
        if len(rows) > 1:
            raise RuntimeError(f"ambiguous task handoff for {team_id}/{member_id}")
        payload = json.loads(rows[0]["payload_json"])
        if not isinstance(payload, dict):
            raise TypeError("persisted task handoff payload must be an object")
        return payload

    def prepare_member_execution(
        self,
        *,
        team_id: str,
        member_id: str,
        resume_token: str | None,
    ) -> TeamMember:
        """Commit RUNNING state and the pre-execution recovery cursor atomically."""
        now = datetime.now(UTC).isoformat()
        with self._store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            team = conn.execute(
                "SELECT state FROM multi_agent_teams WHERE team_id = ?",
                (team_id,),
            ).fetchone()
            if team is None:
                raise KeyError(f"unknown team: {team_id}")
            if team["state"] != TeamState.ACTIVE.value:
                raise RuntimeError("team is not active")
            row = self._member_row(conn, team_id=team_id, member_id=member_id)
            if row is None:
                raise KeyError(f"unknown team member: {team_id}/{member_id}")
            member = self._member_from_row(row)
            if member.parent_id is None:
                raise ValueError("team root is not a child execution")
            if member.state is not MemberState.SPAWNED:
                raise RuntimeError(
                    f"child execution can start only from spawned state, got {member.state.value}"
                )
            conn.execute(
                "UPDATE multi_agent_members SET state = ?, resume_token = ?, updated_at = ? "
                "WHERE team_id = ? AND member_id = ?",
                (MemberState.RUNNING.value, resume_token, now, team_id, member_id),
            )
            self._audit.append_with_connection(
                conn,
                event_type="multi_agent.child_execution_started",
                entity_type="multi_agent_team",
                entity_id=team_id,
                payload={
                    "member_id": member_id,
                    "thread_id": member.thread_id,
                    "resume_bound": resume_token is not None,
                },
            )
        return self.member(team_id, member_id)

    def finish_member_execution(
        self,
        *,
        team_id: str,
        member_id: str,
        state: MemberState,
        outcome: str,
        payload: dict[str, object] | None = None,
        error: str | None = None,
        resume_token: str | None = None,
        result_handoff: AgentHandoff | None = None,
    ) -> TeamMember:
        """Commit member state, result evidence and result/error handoff as one unit."""
        if state is MemberState.SPAWNED:
            raise ValueError("execution result cannot return to spawned state")
        now = datetime.now(UTC).isoformat()
        with self._store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            team = conn.execute(
                "SELECT state FROM multi_agent_teams WHERE team_id = ?",
                (team_id,),
            ).fetchone()
            if team is None:
                raise KeyError(f"unknown team: {team_id}")
            row = self._member_row(conn, team_id=team_id, member_id=member_id)
            if row is None:
                raise KeyError(f"unknown team member: {team_id}/{member_id}")
            member = self._member_from_row(row)

            if team["state"] == TeamState.CANCELLED.value:
                if member.state is not MemberState.CANCELLED:
                    conn.execute(
                        "UPDATE multi_agent_members SET state = ?, updated_at = ? "
                        "WHERE team_id = ? AND member_id = ?",
                        (MemberState.CANCELLED.value, now, team_id, member_id),
                    )
                return self._member_from_row(
                    self._member_row(conn, team_id=team_id, member_id=member_id)
                )
            if team["state"] != TeamState.ACTIVE.value:
                raise RuntimeError("team is not active")
            if member.state in _TERMINAL_MEMBER_STATES:
                raise RuntimeError(f"cannot overwrite terminal child state {member.state.value}")
            if result_handoff is not None:
                self._validate_result_handoff(
                    result_handoff,
                    team_id=team_id,
                    member=member,
                )

            conn.execute(
                "INSERT INTO multi_agent_results(team_id, member_id, outcome, payload_json, "
                "error, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    team_id,
                    member_id,
                    outcome,
                    json.dumps(
                        payload or {},
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    error,
                    now,
                ),
            )
            if result_handoff is not None:
                self._insert_handoff_with_connection(conn, result_handoff, now)
            conn.execute(
                "UPDATE multi_agent_members SET state = ?, resume_token = ?, updated_at = ? "
                "WHERE team_id = ? AND member_id = ?",
                (state.value, resume_token, now, team_id, member_id),
            )
            self._audit.append_with_connection(
                conn,
                event_type="multi_agent.child_execution_finished",
                entity_type="multi_agent_team",
                entity_id=team_id,
                payload={
                    "member_id": member_id,
                    "state": state.value,
                    "outcome": outcome,
                    "resumable": resume_token is not None,
                },
            )
        return self.member(team_id, member_id)

    def set_member_state(
        self,
        *,
        team_id: str,
        member_id: str,
        state: MemberState,
        resume_token: str | None = None,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        with self._store.connection() as conn:
            cursor = conn.execute(
                "UPDATE multi_agent_members SET state = ?, resume_token = ?, updated_at = ? "
                "WHERE team_id = ? AND member_id = ?",
                (state.value, resume_token, now, team_id, member_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"unknown team member: {team_id}/{member_id}")

    def record_handoff(self, handoff: AgentHandoff) -> None:
        now = datetime.now(UTC).isoformat()
        with self._store.connection() as conn:
            self._insert_handoff_with_connection(conn, handoff, now)

    def record_result(
        self,
        *,
        team_id: str,
        member_id: str,
        outcome: str,
        payload: dict[str, object] | None = None,
        error: str | None = None,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        with self._store.connection() as conn:
            exists = conn.execute(
                "SELECT 1 FROM multi_agent_members WHERE team_id = ? AND member_id = ?",
                (team_id, member_id),
            ).fetchone()
            if exists is None:
                raise KeyError(f"unknown team member: {team_id}/{member_id}")
            conn.execute(
                "INSERT INTO multi_agent_results(team_id, member_id, outcome, payload_json, "
                "error, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    team_id,
                    member_id,
                    outcome,
                    json.dumps(
                        payload or {},
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    error,
                    now,
                ),
            )

    def recoverable_members(self, team_id: str) -> tuple[TeamMember, ...]:
        return tuple(
            member for member in self.members(team_id) if member.state in _NONTERMINAL_MEMBER_STATES
        )

    def recoverable_children(self, team_id: str) -> tuple[TeamMember, ...]:
        return tuple(
            member
            for member in self.recoverable_members(team_id)
            if member.parent_id is not None
        )

    def finalize_team(self, team_id: str) -> TeamState:
        """Explicitly close a team only after all child executions are terminal."""
        now = datetime.now(UTC).isoformat()
        with self._store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT state FROM multi_agent_teams WHERE team_id = ?",
                (team_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown team: {team_id}")
            current = TeamState(row["state"])
            if current is not TeamState.ACTIVE:
                return current
            child_rows = conn.execute(
                "SELECT state FROM multi_agent_members "
                "WHERE team_id = ? AND parent_id IS NOT NULL",
                (team_id,),
            ).fetchall()
            states = tuple(MemberState(item["state"]) for item in child_rows)
            active = [state for state in states if state in _NONTERMINAL_MEMBER_STATES]
            if active:
                values = ", ".join(sorted({state.value for state in active}))
                raise RuntimeError(f"team has nonterminal child executions: {values}")

            completed = sum(state is MemberState.COMPLETED for state in states)
            failed = sum(state is MemberState.FAILED for state in states)
            cancelled = sum(state is MemberState.CANCELLED for state in states)
            if completed:
                final = TeamState.COMPLETED
            elif failed:
                final = TeamState.FAILED
            elif cancelled:
                final = TeamState.CANCELLED
            else:
                final = TeamState.COMPLETED
            conn.execute(
                "UPDATE multi_agent_teams SET state = ?, updated_at = ? WHERE team_id = ?",
                (final.value, now, team_id),
            )
            self._audit.append_with_connection(
                conn,
                event_type="multi_agent.team_finalized",
                entity_type="multi_agent_team",
                entity_id=team_id,
                payload={
                    "state": final.value,
                    "completed_children": completed,
                    "failed_children": failed,
                    "cancelled_children": cancelled,
                },
            )
        return final

    def cancel_team(self, team_id: str) -> tuple[TeamMember, ...]:
        now = datetime.now(UTC).isoformat()
        with self._store.connection() as conn:
            row = conn.execute(
                "SELECT state FROM multi_agent_teams WHERE team_id = ?",
                (team_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown team: {team_id}")
            current = TeamState(row["state"])
            if current is TeamState.CANCELLED:
                return self.members(team_id)
            if current is not TeamState.ACTIVE:
                raise RuntimeError(f"team cannot be cancelled from terminal state: {current.value}")
            conn.execute(
                "UPDATE multi_agent_teams SET state = ?, updated_at = ? WHERE team_id = ?",
                (TeamState.CANCELLED.value, now, team_id),
            )
            conn.execute(
                "UPDATE multi_agent_members SET state = ?, updated_at = ? WHERE team_id = ? "
                "AND state IN (?, ?, ?)",
                (
                    MemberState.CANCELLED.value,
                    now,
                    team_id,
                    MemberState.SPAWNED.value,
                    MemberState.RUNNING.value,
                    MemberState.WAITING_APPROVAL.value,
                ),
            )
            self._audit.append_with_connection(
                conn,
                event_type="multi_agent.team_cancelled",
                entity_type="multi_agent_team",
                entity_id=team_id,
            )
        return self.members(team_id)

    @staticmethod
    def _grant_payload(grants: tuple[ToolGrant, ...]) -> str:
        return json.dumps(
            [grant.model_dump(mode="json") for grant in grants],
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def _member_row(conn: object, *, team_id: str, member_id: str):
        return conn.execute(
            "SELECT team_id, member_id, parent_id, depth, agent_id, agent_version, thread_id, "
            "tool_grants_json, state, resume_token FROM multi_agent_members "
            "WHERE team_id = ? AND member_id = ?",
            (team_id, member_id),
        ).fetchone()

    def _insert_member(self, conn: object, member: TeamMember, now: str) -> None:
        conn.execute(
            "INSERT INTO multi_agent_members(team_id, member_id, parent_id, depth, agent_id, "
            "agent_version, thread_id, tool_grants_json, state, resume_token, created_at, "
            "updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                member.team_id,
                member.member_id,
                member.parent_id,
                member.depth,
                member.agent_id,
                member.agent_version,
                member.thread_id,
                self._grant_payload(member.tool_grants),
                member.state.value,
                member.resume_token,
                now,
                now,
            ),
        )

    def _insert_handoff_with_connection(
        self,
        conn: object,
        handoff: AgentHandoff,
        now: str,
    ) -> None:
        participants = int(
            conn.execute(
                "SELECT COUNT(*) FROM multi_agent_members WHERE team_id = ? "
                "AND member_id IN (?, ?)",
                (handoff.team_id, handoff.sender_id, handoff.recipient_id),
            ).fetchone()[0]
        )
        expected = 1 if handoff.sender_id == handoff.recipient_id else 2
        if participants != expected:
            raise KeyError("handoff references a member outside the team")
        conn.execute(
            "INSERT INTO multi_agent_handoffs(handoff_id, team_id, sender_id, recipient_id, "
            "kind, correlation_id, payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                handoff.handoff_id,
                handoff.team_id,
                handoff.sender_id,
                handoff.recipient_id,
                handoff.kind.value,
                handoff.correlation_id,
                json.dumps(
                    handoff.payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                now,
            ),
        )

    @staticmethod
    def _validate_task_handoff(
        handoff: AgentHandoff,
        *,
        team_id: str,
        parent_id: str,
        child_id: str,
    ) -> None:
        if handoff.team_id != team_id:
            raise ValueError("task handoff team does not match spawned child")
        if handoff.sender_id != parent_id or handoff.recipient_id != child_id:
            raise ValueError("task handoff participants do not match spawned child")
        if handoff.kind is not HandoffKind.TASK:
            raise ValueError("spawned child handoff must be TASK")

    @staticmethod
    def _validate_result_handoff(
        handoff: AgentHandoff,
        *,
        team_id: str,
        member: TeamMember,
    ) -> None:
        if handoff.team_id != team_id:
            raise ValueError("result handoff team does not match member")
        if handoff.sender_id != member.member_id or handoff.recipient_id != member.parent_id:
            raise ValueError("result handoff participants do not match member lineage")
        if handoff.kind not in {HandoffKind.RESULT, HandoffKind.ERROR}:
            raise ValueError("execution completion handoff must be RESULT or ERROR")

    @staticmethod
    def _member_from_row(row: object) -> TeamMember:
        return TeamMember(
            team_id=row["team_id"],
            member_id=row["member_id"],
            parent_id=row["parent_id"],
            depth=int(row["depth"]),
            agent_id=row["agent_id"],
            agent_version=int(row["agent_version"]),
            thread_id=row["thread_id"],
            tool_grants=tuple(
                ToolGrant.model_validate(item) for item in json.loads(row["tool_grants_json"])
            ),
            state=MemberState(row["state"]),
            resume_token=row["resume_token"],
        )
