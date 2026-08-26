from __future__ import annotations

import json
import sqlite3
from collections import Counter
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from nika_core.agent_lab_status_models import (
    AgentLabExperimentView,
    AgentLabOperationalSnapshot,
    AgentLabTeamView,
)
from nika_core.data.schema import SCHEMA_VERSION
from nika_core.data.sqlite import SQLiteStore
from nika_core.experiments.contracts import ExperimentStatus
from nika_core.multi_agent.contracts import MemberState, TeamQuota, TeamState

_MIN_SCHEMA = 7
_DEFAULT_LIMIT = 50
_MAX_LIMIT = 200
_QUOTA_KEYS = {
    "max_depth",
    "max_children_per_parent",
    "max_total_agents",
    "max_parallel",
}
_TERMINAL = {MemberState.COMPLETED, MemberState.FAILED, MemberState.CANCELLED}
_REQUIRED_TABLES = {
    "multi_agent_teams",
    "multi_agent_members",
    "experiments",
    "experiment_observations",
    "experiment_events",
}


class AgentLabStatusReader:
    """Bounded read-only projection over canonical M7/M8 durable state."""

    def __init__(self, store: SQLiteStore, *, limit: int = _DEFAULT_LIMIT) -> None:
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise TypeError("limit must be an integer")
        if not 1 <= limit <= _MAX_LIMIT:
            raise ValueError(f"limit must be between 1 and {_MAX_LIMIT}")
        self._store = store
        self._limit = limit

    def snapshot(self) -> AgentLabOperationalSnapshot:
        if not self._store.path.is_file():
            raise FileNotFoundError(f"Nika database does not exist: {self._store.path}")
        try:
            with self._store.connection() as conn:
                conn.execute("PRAGMA query_only = ON")
                conn.execute("BEGIN")
                schema_version = self._schema_version(conn)
                self._verify_integrity(conn)
                team_count = self._scalar(conn, "SELECT COUNT(*) FROM multi_agent_teams")
                active_team_count = self._scalar(
                    conn,
                    "SELECT COUNT(*) FROM multi_agent_teams WHERE state = ?",
                    (TeamState.ACTIVE.value,),
                )
                waiting_count = self._scalar(
                    conn,
                    "SELECT COUNT(DISTINCT team_id) FROM multi_agent_members "
                    "WHERE parent_id IS NOT NULL AND state = ?",
                    (MemberState.WAITING_APPROVAL.value,),
                )
                experiment_count = self._scalar(conn, "SELECT COUNT(*) FROM experiments")
                running_count = self._scalar(
                    conn,
                    "SELECT COUNT(*) FROM experiments WHERE status = ?",
                    (ExperimentStatus.RUNNING.value,),
                )
                teams = self._team_views(conn)
                experiments = self._experiment_views(conn)
        except sqlite3.Error as exc:
            raise RuntimeError("Agent Lab durable state could not be read safely") from exc
        return AgentLabOperationalSnapshot(
            schema_version=schema_version,
            team_count=team_count,
            active_team_count=active_team_count,
            waiting_approval_team_count=waiting_count,
            experiment_count=experiment_count,
            running_experiment_count=running_count,
            teams=teams,
            experiments=experiments,
        )

    @staticmethod
    def _schema_version(conn: Any) -> int:
        row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        value = None if row is None else row[0]
        if isinstance(value, bool) or not isinstance(value, int):
            raise RuntimeError("Nika database schema version is invalid")
        if value < _MIN_SCHEMA:
            raise RuntimeError(
                "Nika database predates the Agent Lab durable schema; "
                "run the normal Nika migration path first"
            )
        if value > SCHEMA_VERSION:
            raise RuntimeError(
                f"Nika database schema {value} is newer than supported schema {SCHEMA_VERSION}"
            )
        return value

    @staticmethod
    def _verify_integrity(conn: Any) -> None:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name IN (?, ?, ?, ?, ?)",
            tuple(sorted(_REQUIRED_TABLES)),
        ).fetchall()
        present = {row[0] for row in rows if isinstance(row[0], str)}
        missing = sorted(_REQUIRED_TABLES - present)
        if missing:
            raise RuntimeError(f"Agent Lab durable tables are missing: {', '.join(missing)}")
        orphan_member = conn.execute(
            "SELECT 1 FROM multi_agent_members AS member "
            "LEFT JOIN multi_agent_teams AS team ON team.team_id = member.team_id "
            "WHERE team.team_id IS NULL LIMIT 1"
        ).fetchone()
        if orphan_member is not None:
            raise RuntimeError("Agent Lab durable state contains an orphan team member")
        orphan_parent = conn.execute(
            "SELECT 1 FROM multi_agent_members AS child "
            "LEFT JOIN multi_agent_members AS parent "
            "ON parent.team_id = child.team_id AND parent.member_id = child.parent_id "
            "WHERE child.parent_id IS NOT NULL AND parent.member_id IS NULL LIMIT 1"
        ).fetchone()
        if orphan_parent is not None:
            raise RuntimeError("Agent Lab durable state contains an orphan parent reference")
        orphan_evidence = conn.execute(
            "SELECT 1 FROM experiment_observations AS item "
            "LEFT JOIN experiments AS exp ON exp.experiment_id = item.experiment_id "
            "WHERE exp.experiment_id IS NULL LIMIT 1"
        ).fetchone()
        orphan_event = conn.execute(
            "SELECT 1 FROM experiment_events AS item "
            "LEFT JOIN experiments AS exp ON exp.experiment_id = item.experiment_id "
            "WHERE exp.experiment_id IS NULL LIMIT 1"
        ).fetchone()
        if orphan_evidence is not None or orphan_event is not None:
            raise RuntimeError("Agent Lab durable state contains orphan experiment evidence")

    def _team_views(self, conn: Any) -> tuple[AgentLabTeamView, ...]:
        rows = conn.execute(
            "SELECT team_id, root_member_id, state, quota_json, updated_at "
            "FROM multi_agent_teams ORDER BY updated_at DESC, team_id LIMIT ?",
            (self._limit,),
        ).fetchall()
        return tuple(self._team_view(conn, row) for row in rows)

    def _team_view(self, conn: Any, row: Any) -> AgentLabTeamView:
        team_id = self._identifier(row[0], "team_id")
        root_id = self._identifier(row[1], "root_member_id")
        try:
            team_state = TeamState(self._text(row[2], "team state"))
        except ValueError as exc:
            raise RuntimeError(f"Agent Lab team {team_id} has an invalid state") from exc
        quota = self._quota(row[3], team_id)
        updated_at = self._timestamp(row[4], f"team {team_id} updated_at")
        members = conn.execute(
            "SELECT member_id, parent_id, depth, state FROM multi_agent_members "
            "WHERE team_id = ?",
            (team_id,),
        ).fetchall()
        by_id = {self._text(item[0], "member_id"): item for item in members}
        if len(by_id) != len(members) or root_id not in by_id:
            raise RuntimeError(f"Agent Lab team {team_id} has invalid member identity")
        root = by_id[root_id]
        if root[1] is not None or root[2] != 0:
            raise RuntimeError(f"Agent Lab team {team_id} has an invalid root member")
        if not 1 <= len(members) <= quota.max_total_agents:
            raise RuntimeError(f"Agent Lab team {team_id} violates its member quota")

        child_states: Counter[MemberState] = Counter()
        child_counts: Counter[str] = Counter()
        for member_id, member in by_id.items():
            if member_id == root_id:
                continue
            parent_id = member[1]
            if not isinstance(parent_id, str) or parent_id not in by_id:
                raise RuntimeError(f"Agent Lab team {team_id} has invalid parent lineage")
            depth = member[2]
            parent_depth = by_id[parent_id][2]
            if (
                isinstance(depth, bool)
                or not isinstance(depth, int)
                or isinstance(parent_depth, bool)
                or not isinstance(parent_depth, int)
                or depth != parent_depth + 1
                or depth > quota.max_depth
            ):
                raise RuntimeError(f"Agent Lab team {team_id} has invalid depth lineage")
            child_counts[parent_id] += 1
            try:
                child_states[MemberState(self._text(member[3], "member state"))] += 1
            except ValueError as exc:
                raise RuntimeError(
                    f"Agent Lab team {team_id} has an invalid member state"
                ) from exc
        if child_counts and max(child_counts.values()) > quota.max_children_per_parent:
            raise RuntimeError(f"Agent Lab team {team_id} violates its child quota")
        child_count = sum(child_states.values())
        terminal_count = sum(child_states[state] for state in _TERMINAL)
        nonterminal_count = child_count - terminal_count
        if team_state is not TeamState.ACTIVE and nonterminal_count:
            raise RuntimeError(
                f"Agent Lab terminal team {team_id} still has nonterminal children"
            )
        return AgentLabTeamView(
            team_id=team_id,
            state=team_state.value,
            member_count=len(members),
            child_count=child_count,
            nonterminal_child_count=nonterminal_count,
            waiting_approval_count=child_states[MemberState.WAITING_APPROVAL],
            completed_member_count=child_states[MemberState.COMPLETED],
            failed_member_count=child_states[MemberState.FAILED],
            cancelled_member_count=child_states[MemberState.CANCELLED],
            max_total_agents=quota.max_total_agents,
            max_parallel=quota.max_parallel,
            updated_at=updated_at,
        )

    @staticmethod
    def _quota(raw: object, team_id: str) -> TeamQuota:
        if not isinstance(raw, str):
            raise RuntimeError(f"Agent Lab team {team_id} quota is not text")
        try:
            payload = json.loads(raw)
        except ValueError as exc:
            raise RuntimeError(f"Agent Lab team {team_id} quota is invalid JSON") from exc
        if not isinstance(payload, dict) or set(payload) != _QUOTA_KEYS:
            raise RuntimeError(f"Agent Lab team {team_id} quota shape is invalid")
        if any(isinstance(value, bool) or not isinstance(value, int) for value in payload.values()):
            raise RuntimeError(f"Agent Lab team {team_id} quota types are invalid")
        try:
            return TeamQuota(**payload)
        except ValueError as exc:
            raise RuntimeError(f"Agent Lab team {team_id} quota values are invalid") from exc

    def _experiment_views(self, conn: Any) -> tuple[AgentLabExperimentView, ...]:
        rows = conn.execute(
            "SELECT experiment_id, definition_json, status, updated_at FROM experiments "
            "ORDER BY updated_at DESC, experiment_id LIMIT ?",
            (self._limit,),
        ).fetchall()
        return tuple(self._experiment_view(conn, row) for row in rows)

    def _experiment_view(self, conn: Any, row: Any) -> AgentLabExperimentView:
        experiment_id = self._identifier(row[0], "experiment_id")
        try:
            payload = json.loads(self._text(row[1], "experiment definition"))
        except ValueError as exc:
            raise RuntimeError(
                f"Agent Lab experiment {experiment_id} definition is invalid JSON"
            ) from exc
        if not isinstance(payload, dict) or payload.get("experiment_id") != experiment_id:
            raise RuntimeError(
                f"Agent Lab experiment {experiment_id} definition identity is invalid"
            )
        try:
            status = ExperimentStatus(self._text(row[2], "experiment status"))
        except ValueError as exc:
            raise RuntimeError(
                f"Agent Lab experiment {experiment_id} has an invalid status"
            ) from exc
        updated_at = self._timestamp(row[3], f"experiment {experiment_id} updated_at")
        observation_count = self._scalar(
            conn,
            "SELECT COUNT(*) FROM experiment_observations WHERE experiment_id = ?",
            (experiment_id,),
        )
        event_count = self._scalar(
            conn,
            "SELECT COUNT(*) FROM experiment_events WHERE experiment_id = ?",
            (experiment_id,),
        )
        latest = conn.execute(
            "SELECT new_status FROM experiment_events WHERE experiment_id = ? "
            "ORDER BY event_id DESC LIMIT 1",
            (experiment_id,),
        ).fetchone()
        if event_count < 1 or latest is None or latest[0] != status.value:
            raise RuntimeError(
                f"Agent Lab experiment {experiment_id} lifecycle tail does not match status"
            )
        return AgentLabExperimentView(
            experiment_id=experiment_id,
            status=status.value,
            observation_count=observation_count,
            event_count=event_count,
            updated_at=updated_at,
        )

    @staticmethod
    def _scalar(conn: Any, sql: str, params: tuple[object, ...] = ()) -> int:
        row = conn.execute(sql, params).fetchone()
        value = None if row is None else row[0]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise RuntimeError("Agent Lab durable count is invalid")
        return value

    @staticmethod
    def _text(value: object, field: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise RuntimeError(f"Agent Lab durable {field} is invalid")
        return value

    @classmethod
    def _identifier(cls, value: object, field: str) -> str:
        text = cls._text(value, field)
        unsafe = any(ord(char) < 32 or ord(char) == 127 for char in text)
        if len(text) > 256 or unsafe:
            raise RuntimeError(f"Agent Lab durable {field} is unsafe for operational output")
        return text

    @classmethod
    def _timestamp(cls, value: object, field: str) -> str:
        text = cls._text(value, field)
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise RuntimeError(f"Agent Lab durable {field} is not an ISO timestamp") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise RuntimeError(f"Agent Lab durable {field} is not timezone-aware")
        return text


class AgentLabStateProvider:
    """Compose Agent Lab status without replacing existing product state authority."""

    def __init__(
        self,
        *,
        base_state: Callable[[], Mapping[str, Any]],
        status_reader: AgentLabStatusReader,
    ) -> None:
        self._base_state = base_state
        self._status_reader = status_reader

    def __call__(self) -> dict[str, Any]:
        base = self._base_state()
        if not isinstance(base, Mapping):
            raise TypeError("base state provider must return a mapping")
        if "agent_lab" in base:
            raise RuntimeError("base state already owns the agent_lab projection")
        return {**base, "agent_lab": self._status_reader.snapshot().as_dict()}
