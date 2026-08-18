from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime

from nika_core.builder.spec import ToolGrant
from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog
from nika_core.multi_agent.contracts import (
    AgentHandoff,
    MemberState,
    TeamMember,
    TeamQuota,
    TeamState,
    attenuate_grants,
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
                "INSERT INTO multi_agent_teams(team_id, root_member_id, state, quota_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
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
            row = conn.execute(
                "SELECT team_id, member_id, parent_id, depth, agent_id, agent_version, thread_id, "
                "tool_grants_json, state, resume_token FROM multi_agent_members "
                "WHERE team_id = ? AND member_id = ?",
                (team_id, member_id),
            ).fetchone()
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
    ) -> TeamMember:
        now = datetime.now(UTC).isoformat()
        with self._store.connection() as conn:
            team = conn.execute(
                "SELECT state, quota_json FROM multi_agent_teams WHERE team_id = ?",
                (team_id,),
            ).fetchone()
            if team is None:
                raise KeyError(f"unknown team: {team_id}")
            if team["state"] != TeamState.ACTIVE.value:
                raise RuntimeError("team is not active")
            quota = TeamQuota(**json.loads(team["quota_json"]))
            parent_row = conn.execute(
                "SELECT team_id, member_id, parent_id, depth, agent_id, agent_version, thread_id, "
                "tool_grants_json, state, resume_token FROM multi_agent_members "
                "WHERE team_id = ? AND member_id = ?",
                (team_id, parent_id),
            ).fetchone()
            if parent_row is None:
                raise KeyError(f"unknown parent: {parent_id}")
            parent = self._member_from_row(parent_row)
            if parent.depth + 1 > quota.max_depth:
                raise RuntimeError("spawn depth quota exceeded")
            child_count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM multi_agent_members WHERE team_id = ? AND parent_id = ?",
                    (team_id, parent_id),
                ).fetchone()[0]
            )
            if child_count >= quota.max_children_per_parent:
                raise RuntimeError("children-per-parent quota exceeded")
            total_count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM multi_agent_members WHERE team_id = ?",
                    (team_id,),
                ).fetchone()[0]
            )
            if total_count >= quota.max_total_agents:
                raise RuntimeError("total-agent quota exceeded")
            grants = attenuate_grants(parent.tool_grants, requested_grants)
            child = TeamMember(
                team_id=team_id,
                member_id=child_id,
                parent_id=parent_id,
                depth=parent.depth + 1,
                agent_id=agent_id,
                agent_version=agent_version,
                thread_id=thread_id,
                tool_grants=grants,
            )
            self._insert_member(conn, child, now)
            self._audit.append_with_connection(
                conn,
                event_type="multi_agent.child_spawned",
                entity_type="multi_agent_team",
                entity_id=team_id,
                payload={"parent_id": parent_id, "child_id": child_id, "depth": child.depth},
            )
        return child

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
                    json.dumps(handoff.payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    now,
                ),
            )

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
                "INSERT INTO multi_agent_results(team_id, member_id, outcome, payload_json, error, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    team_id,
                    member_id,
                    outcome,
                    json.dumps(payload or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    error,
                    now,
                ),
            )

    def recoverable_members(self, team_id: str) -> tuple[TeamMember, ...]:
        return tuple(
            member
            for member in self.members(team_id)
            if member.state
            in {MemberState.SPAWNED, MemberState.RUNNING, MemberState.WAITING_APPROVAL}
        )

    def cancel_team(self, team_id: str) -> tuple[TeamMember, ...]:
        now = datetime.now(UTC).isoformat()
        with self._store.connection() as conn:
            row = conn.execute(
                "SELECT state FROM multi_agent_teams WHERE team_id = ?",
                (team_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown team: {team_id}")
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

    def _insert_member(self, conn: object, member: TeamMember, now: str) -> None:
        conn.execute(
            "INSERT INTO multi_agent_members(team_id, member_id, parent_id, depth, agent_id, "
            "agent_version, thread_id, tool_grants_json, state, resume_token, created_at, updated_at) "
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
            tool_grants=tuple(ToolGrant.model_validate(item) for item in json.loads(row["tool_grants_json"])),
            state=MemberState(row["state"]),
            resume_token=row["resume_token"],
        )
