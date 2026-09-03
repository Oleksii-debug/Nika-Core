from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

from nika_core.data.sqlite import SQLiteStore

_STATE_KEY = "v01_team_task"
_UNAVAILABLE_MESSAGE = "Стан командного завдання недоступний."
_ALLOWED_STAGES = frozenset({"worker", "checker"})
_TERMINAL_TEAM_STATES = frozenset({"completed", "failed", "cancelled"})
_TERMINAL_MEMBER_STATES = frozenset({"completed", "failed", "cancelled"})


class V01PackagedTeamStateProvider:
    """Bounded read-only V0.1 team projection for the packaged pywebview shell."""

    def __init__(
        self,
        *,
        base_state: Callable[[], Mapping[str, Any]],
        store: SQLiteStore,
    ) -> None:
        self._base_state = base_state
        self._store = store

    def __call__(self) -> Mapping[str, Any]:
        state = dict(self._base_state())
        if _STATE_KEY in state:
            raise ValueError("V0.1 team projection key collision.")
        state[_STATE_KEY] = self._safe_snapshot(state)
        return state

    def _safe_snapshot(self, base_state: Mapping[str, Any]) -> dict[str, Any] | None:
        try:
            return self._snapshot(base_state)
        except Exception:  # noqa: BLE001 - presentation must fail closed without diagnostics.
            return {
                "available": False,
                "message": _UNAVAILABLE_MESSAGE,
            }

    def _snapshot(self, base_state: Mapping[str, Any]) -> dict[str, Any] | None:
        with self._store.connection() as conn:
            conn.execute("PRAGMA query_only = ON")
            conn.execute("BEGIN")
            teams = conn.execute(
                "SELECT team_id, root_member_id, state, created_at, updated_at "
                "FROM multi_agent_teams "
                "ORDER BY updated_at DESC, created_at DESC, team_id DESC LIMIT 50"
            ).fetchall()
            for team in teams:
                candidate = self._candidate_for_team(conn, team, base_state)
                if candidate is not None:
                    return candidate
        return None

    def _candidate_for_team(
        self,
        conn: Any,
        team: Any,
        base_state: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        team_id = str(team["team_id"])
        task_rows = conn.execute(
            "SELECT handoff_id, recipient_id, payload_json, created_at "
            "FROM multi_agent_handoffs "
            "WHERE team_id = ? AND kind = 'task' "
            "ORDER BY created_at, handoff_id",
            (team_id,),
        ).fetchall()

        stage_by_member: dict[str, str] = {}
        shared_task_id: str | None = None
        saw_v01_marker = False
        for row in task_rows:
            try:
                payload = json.loads(row["payload_json"])
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            marked = "shared_task_id" in payload or "stage" in payload
            if not marked:
                continue
            saw_v01_marker = True
            task_id = payload.get("shared_task_id")
            stage = payload.get("stage")
            if not isinstance(task_id, str) or not task_id.strip():
                raise ValueError("invalid V0.1 task identity")
            if stage not in _ALLOWED_STAGES:
                raise ValueError("invalid V0.1 task stage")
            recipient_id = str(row["recipient_id"])
            normalized_task_id = task_id.strip()
            if shared_task_id is None:
                shared_task_id = normalized_task_id
            elif shared_task_id != normalized_task_id:
                raise ValueError("conflicting V0.1 task identity")
            previous = stage_by_member.get(recipient_id)
            if previous is not None:
                raise ValueError("duplicate V0.1 member task handoff")
            if stage in stage_by_member.values():
                raise ValueError("duplicate V0.1 stage")
            stage_by_member[recipient_id] = stage

        if not saw_v01_marker:
            return None
        if shared_task_id is None or "worker" not in stage_by_member.values():
            raise ValueError("incomplete V0.1 team identity")

        member_rows = conn.execute(
            "SELECT member_id, parent_id, state, created_at, updated_at "
            "FROM multi_agent_members WHERE team_id = ? "
            "ORDER BY depth, created_at, member_id",
            (team_id,),
        ).fetchall()
        if not 2 <= len(member_rows) <= 3:
            raise ValueError("invalid V0.1 member count")

        root_id = str(team["root_member_id"])
        roles: dict[str, str] = {root_id: "supervisor"}
        for member_id, stage in stage_by_member.items():
            roles[member_id] = stage
        member_ids = {str(row["member_id"]) for row in member_rows}
        if root_id not in member_ids or set(roles) != member_ids:
            raise ValueError("V0.1 member roster mismatch")
        if len(member_rows) == 3 and set(roles.values()) != {
            "supervisor",
            "worker",
            "checker",
        }:
            raise ValueError("V0.1 three-member roster is incomplete")

        result_rows = conn.execute(
            "SELECT result_id, member_id, outcome, error, created_at "
            "FROM multi_agent_results WHERE team_id = ? "
            "ORDER BY result_id",
            (team_id,),
        ).fetchall()
        latest_result: dict[str, Any] = {}
        for row in result_rows:
            latest_result[str(row["member_id"])] = row

        error_members = {
            str(row["sender_id"])
            for row in conn.execute(
                "SELECT sender_id FROM multi_agent_handoffs "
                "WHERE team_id = ? AND kind = 'error'",
                (team_id,),
            ).fetchall()
        }

        team_state = str(team["state"])
        members = [
            self._member_view(
                row,
                role=roles[str(row["member_id"])],
                team_state=team_state,
                result=latest_result.get(str(row["member_id"])),
                has_error_handoff=str(row["member_id"]) in error_members,
            )
            for row in member_rows
        ]
        events = self._event_views(conn, team_id=team_id, roles=roles)
        task_view = self._task_view(base_state, shared_task_id=shared_task_id)
        final_result = self._final_result(
            shared_task_id=shared_task_id,
            team_id=team_id,
            team_state=team_state,
            members=members,
            result_count=len(result_rows),
        )
        return {
            "available": True,
            "task": task_view,
            "team": {
                "team_id": team_id,
                "state": team_state,
                "member_count": len(member_rows),
                "expected_member_count": 3,
                "roster_complete": len(member_rows) == 3,
            },
            "members": members,
            "events": events,
            "final_result": final_result,
        }

    @staticmethod
    def _task_view(
        base_state: Mapping[str, Any],
        *,
        shared_task_id: str,
    ) -> dict[str, Any]:
        task: Mapping[str, Any] | None = None
        raw_tasks = base_state.get("tasks", ())
        if isinstance(raw_tasks, list):
            matches = [
                item
                for item in raw_tasks
                if isinstance(item, Mapping) and item.get("task_id") == shared_task_id
            ]
            if len(matches) > 1:
                raise ValueError("ambiguous packaged task identity")
            if matches:
                task = matches[0]
        result: dict[str, Any] = {
            "task_id": shared_task_id,
            "state": str(task.get("state")) if task is not None else "not_in_task_queue",
        }
        if task is not None:
            command = task.get("command")
            if isinstance(command, str) and command.strip():
                result["command"] = command.strip()
        return result

    @staticmethod
    def _member_view(
        row: Any,
        *,
        role: str,
        team_state: str,
        result: Any | None,
        has_error_handoff: bool,
    ) -> dict[str, Any]:
        member_id = str(row["member_id"])
        member_state = str(row["state"])
        public_state = V01PackagedTeamStateProvider._public_member_state(
            role=role,
            member_state=member_state,
            team_state=team_state,
        )
        view: dict[str, Any] = {
            "member_id": member_id,
            "role": role,
            "state": public_state,
            "current_operation": V01PackagedTeamStateProvider._operation(
                role=role,
                state=public_state,
            ),
        }
        failed_result = False
        if result is not None:
            outcome = str(result["outcome"])
            failed_result = outcome in {"failed", "exception"} or result["error"] is not None
        if member_state == "failed" or failed_result or has_error_handoff:
            view["safe_error"] = {
                "code": "member_failed",
                "message": "Виконання учасника завершилося помилкою.",
            }
        return view

    @staticmethod
    def _public_member_state(*, role: str, member_state: str, team_state: str) -> str:
        if role != "supervisor":
            return member_state
        if team_state == "active":
            return "running"
        if team_state in _TERMINAL_TEAM_STATES:
            return team_state
        return member_state

    @staticmethod
    def _operation(*, role: str, state: str) -> str:
        if state == "waiting_approval":
            return "Очікує підтвердження."
        if state == "paused":
            return "Роботу призупинено."
        if state == "completed":
            return "Роботу завершено."
        if state == "failed":
            return "Роботу завершено з помилкою."
        if state == "cancelled":
            return "Роботу скасовано."
        if state == "spawned":
            return "Очікує запуску."
        if role == "supervisor":
            return "Координує командне завдання."
        if role == "checker":
            return "Перевіряє результат виконавця."
        return "Виконує командне завдання."

    @staticmethod
    def _event_views(
        conn: Any,
        *,
        team_id: str,
        roles: Mapping[str, str],
    ) -> list[dict[str, str]]:
        rows = conn.execute(
            "SELECT sender_id, recipient_id, kind, created_at "
            "FROM multi_agent_handoffs WHERE team_id = ? "
            "ORDER BY created_at, handoff_id",
            (team_id,),
        ).fetchall()
        events: list[dict[str, str]] = []
        for row in rows[-20:]:
            kind = str(row["kind"])
            sender_id = str(row["sender_id"])
            recipient_id = str(row["recipient_id"])
            if kind == "task":
                role = roles.get(recipient_id)
                if role not in {"worker", "checker"}:
                    continue
                code = f"{role}.assigned"
                message = (
                    "Завдання передано виконавцю."
                    if role == "worker"
                    else "Перевірку передано перевіряльнику."
                )
            elif kind in {"result", "error"}:
                role = roles.get(sender_id)
                if role not in {"worker", "checker"}:
                    continue
                code = f"{role}.{kind}"
                if kind == "error":
                    message = "Учасник завершив операцію з помилкою."
                else:
                    message = "Учасник зберіг результат операції."
            else:
                continue
            events.append(
                {
                    "code": code,
                    "message": message,
                    "time": str(row["created_at"]),
                }
            )
        return events

    @staticmethod
    def _final_result(
        *,
        shared_task_id: str,
        team_id: str,
        team_state: str,
        members: list[dict[str, Any]],
        result_count: int,
    ) -> dict[str, Any] | None:
        if team_state not in _TERMINAL_TEAM_STATES:
            return None
        summaries = {
            "completed": "Командне завдання завершено; записи результатів учасників зафіксовано.",
            "failed": "Командне завдання завершено з помилкою; доступний безпечний стан учасників.",
            "cancelled": "Командне завдання скасовано; збережений стан доступний після перезапуску.",
        }
        terminal_members = sum(
            1 for member in members if member["state"] in _TERMINAL_MEMBER_STATES
        )
        return {
            "status": team_state,
            "summary": summaries[team_state],
            "task_id": shared_task_id,
            "team_id": team_id,
            "terminal_member_count": terminal_members,
            "result_record_count": result_count,
        }
