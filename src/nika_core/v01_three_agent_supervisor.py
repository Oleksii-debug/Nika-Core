from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from nika_core.builder.repository import AgentDefinitionRepository
from nika_core.builder.spec import ToolGrant
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
from nika_core.multi_agent.store import MultiAgentStore
from nika_core.multi_agent.supervisor import MultiAgentSupervisor
from nika_core.runtime.contracts import RuntimeErrorCode

_JOURNEY_SCHEMA = "nika-v01-three-agent-v1"
_SAFE_OBSERVATION_ERRORS = frozenset(
    {
        "AssertionError",
        "KeyError",
        "OSError",
        "PermissionError",
        "RuntimeError",
        "RuntimeFailure",
        "TimeoutError",
        "TypeError",
        "ValueError",
        "WorkerException",
        *(item.value for item in RuntimeErrorCode),
    }
)


@dataclass(frozen=True, slots=True)
class V01ChildAssignment:
    """One fixed child role in the V0.1 supervisor/worker/checker journey."""

    member_id: str
    agent_id: str
    agent_version: int
    requested_grants: tuple[ToolGrant, ...]
    instruction: str

    def __post_init__(self) -> None:
        if not self.member_id.strip() or not self.agent_id.strip():
            raise ValueError("child assignment identifiers must not be empty")
        if self.agent_version < 1:
            raise ValueError("child agent_version must be positive")
        if not self.instruction.strip():
            raise ValueError("child instruction must not be empty")


@dataclass(frozen=True, slots=True)
class V01ThreeAgentConfig:
    root_member_id: str
    root_agent_id: str
    root_agent_version: int
    root_grants: tuple[ToolGrant, ...]
    worker: V01ChildAssignment
    checker: V01ChildAssignment

    def __post_init__(self) -> None:
        if not self.root_member_id.strip() or not self.root_agent_id.strip():
            raise ValueError("root identifiers must not be empty")
        if self.root_agent_version < 1:
            raise ValueError("root agent_version must be positive")
        member_ids = {self.root_member_id, self.worker.member_id, self.checker.member_id}
        if len(member_ids) != 3:
            raise ValueError("root, worker and checker member_id values must differ")


@dataclass(frozen=True, slots=True)
class V01ChildObservation:
    member_id: str
    state: MemberState
    output: dict[str, object]
    error: str | None

    def as_payload(self) -> dict[str, object]:
        return {
            "member_id": self.member_id,
            "state": self.state.value,
            "output": dict(self.output),
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class V01ThreeAgentResult:
    shared_task_id: str
    team_id: str
    team_state: TeamState
    worker: V01ChildObservation
    checker: V01ChildObservation
    final_output: dict[str, object]


class V01ThreeAgentSupervisor:
    """Thin fixed V0.1 journey over the integrated M7 multi-agent coordinator."""

    def __init__(
        self,
        *,
        coordinator: MultiAgentSupervisor,
        store: MultiAgentStore,
        definitions: AgentDefinitionRepository,
        config: V01ThreeAgentConfig,
    ) -> None:
        self._coordinator = coordinator
        self._store = store
        self._definitions = definitions
        self._config = config

    async def run(
        self,
        *,
        user_goal: str,
        shared_task_id: str,
        team_id: str,
    ) -> V01ThreeAgentResult:
        goal = user_goal.strip()
        task_id = shared_task_id.strip()
        fixed_team_id = team_id.strip()
        if not goal:
            raise ValueError("user_goal must not be empty")
        if not task_id:
            raise ValueError("shared_task_id must not be empty")
        if not fixed_team_id:
            raise ValueError("team_id must not be empty")

        root_grants = self._validated_root_grants()
        self._ensure_team(
            team_id=fixed_team_id,
            shared_task_id=task_id,
            user_goal=goal,
            root_grants=root_grants,
        )

        worker = await self._ensure_child(
            role=self._config.worker,
            team_id=fixed_team_id,
            root_grants=root_grants,
            payload={
                "shared_task_id": task_id,
                "user_goal": goal,
                "stage": "worker",
                "assignment": self._config.worker.instruction,
            },
        )
        checker = await self._ensure_child(
            role=self._config.checker,
            team_id=fixed_team_id,
            root_grants=root_grants,
            payload={
                "shared_task_id": task_id,
                "user_goal": goal,
                "stage": "checker",
                "assignment": self._config.checker.instruction,
                "worker_observation": worker.as_payload(),
            },
        )

        team_state = self._coordinator.finalize_team(fixed_team_id)
        return V01ThreeAgentResult(
            shared_task_id=task_id,
            team_id=fixed_team_id,
            team_state=team_state,
            worker=worker,
            checker=checker,
            final_output=self._compare_outputs(worker, checker),
        )

    def _ensure_team(
        self,
        *,
        team_id: str,
        shared_task_id: str,
        user_goal: str,
        root_grants: tuple[ToolGrant, ...],
    ) -> None:
        root_payload = {
            "schema": _JOURNEY_SCHEMA,
            "shared_task_id": shared_task_id,
            "user_goal": user_goal,
            "config_fingerprint": self._config_fingerprint(),
        }
        try:
            state = self._store.team_state(team_id)
        except KeyError:
            root_member_id = self._config.root_member_id
            self._store.create_team(
                team_id=team_id,
                root_member_id=root_member_id,
                root_agent_id=self._config.root_agent_id,
                root_agent_version=self._config.root_agent_version,
                root_thread_id=self._thread_id(team_id, root_member_id),
                root_grants=root_grants,
                quota=self._quota(),
                root_task_handoff=AgentHandoff(
                    team_id=team_id,
                    sender_id=root_member_id,
                    recipient_id=root_member_id,
                    kind=HandoffKind.TASK,
                    payload=root_payload,
                    handoff_id=f"task:{team_id}:{root_member_id}",
                    correlation_id=f"team:{team_id}:{root_member_id}:root-task",
                ),
            )
            return

        if state not in {TeamState.ACTIVE, TeamState.COMPLETED}:
            raise RuntimeError(f"V0.1 team cannot resume from state: {state.value}")
        if self._store.quota(team_id) != self._quota():
            raise PermissionError("existing team quota does not match V0.1 journey")
        root = self._store.member(team_id, self._config.root_member_id)
        if (
            root.parent_id is not None
            or root.depth != 0
            or root.agent_id != self._config.root_agent_id
            or root.agent_version != self._config.root_agent_version
            or root.thread_id != self._thread_id(team_id, root.member_id)
            or root.tool_grants != root_grants
        ):
            raise PermissionError("existing team root does not match V0.1 journey")
        if self._store.task_payload(team_id, root.member_id) != root_payload:
            raise PermissionError("existing team task identity does not match V0.1 journey")
        expected_ids = {
            self._config.root_member_id,
            self._config.worker.member_id,
            self._config.checker.member_id,
        }
        members = self._store.members(team_id)
        actual_ids = {member.member_id for member in members}
        if not actual_ids.issubset(expected_ids):
            raise PermissionError("existing team contains a member outside V0.1 journey")
        if state is TeamState.COMPLETED and actual_ids != expected_ids:
            raise RuntimeError("completed V0.1 team is missing durable child evidence")

    async def _ensure_child(
        self,
        *,
        role: V01ChildAssignment,
        team_id: str,
        root_grants: tuple[ToolGrant, ...],
        payload: dict[str, object],
    ) -> V01ChildObservation:
        existing = {member.member_id: member for member in self._store.members(team_id)}.get(
            role.member_id
        )
        if existing is None:
            if self._store.team_state(team_id) is not TeamState.ACTIVE:
                raise RuntimeError("terminal V0.1 team cannot create missing child evidence")
            await self._coordinator.fan_out(
                team_id=team_id,
                parent_id=self._config.root_member_id,
                requests=(self._request(role=role, team_id=team_id, payload=payload),),
            )
        else:
            self._validate_existing_child(
                existing=existing,
                role=role,
                team_id=team_id,
                root_grants=root_grants,
                payload=payload,
            )
            if existing.state in {MemberState.SPAWNED, MemberState.RUNNING}:
                await self._coordinator.recover_team(team_id)

        member = self._store.member(team_id, role.member_id)
        if member.state in {
            MemberState.SPAWNED,
            MemberState.RUNNING,
            MemberState.WAITING_APPROVAL,
        }:
            raise RuntimeError(
                f"V0.1 child has no terminal durable result: {member.state.value}"
            )
        result = self._store.member_result(team_id, role.member_id)
        allowed_outcomes = {
            MemberState.COMPLETED: frozenset({"completed"}),
            MemberState.FAILED: frozenset({"exception", "failed"}),
            MemberState.CANCELLED: frozenset({"cancelled"}),
        }
        if result.outcome not in allowed_outcomes.get(member.state, frozenset()):
            raise RuntimeError("durable child state conflicts with its result outcome")
        return V01ChildObservation(
            member_id=member.member_id,
            state=member.state,
            output=dict(result.payload),
            error=self._safe_observation_error(result.error),
        )

    def _validate_existing_child(
        self,
        *,
        existing: TeamMember,
        role: V01ChildAssignment,
        team_id: str,
        root_grants: tuple[ToolGrant, ...],
        payload: dict[str, object],
    ) -> None:
        active = self._definitions.require_active(role.agent_id, role.agent_version)
        attenuate_grants(active.definition.tool_grants, role.requested_grants)
        expected_grants = attenuate_grants(root_grants, role.requested_grants)
        if (
            existing.parent_id != self._config.root_member_id
            or existing.depth != 1
            or existing.agent_id != role.agent_id
            or existing.agent_version != role.agent_version
            or existing.thread_id != self._thread_id(team_id, role.member_id)
            or existing.tool_grants != expected_grants
        ):
            raise PermissionError("existing child does not match V0.1 journey")
        if self._store.task_payload(team_id, role.member_id) != payload:
            raise PermissionError("existing child task does not match V0.1 journey")

    def _config_fingerprint(self) -> str:
        def assignment(role: V01ChildAssignment) -> dict[str, object]:
            return {
                "member_id": role.member_id,
                "agent_id": role.agent_id,
                "agent_version": role.agent_version,
                "requested_grants": [
                    grant.model_dump(mode="json") for grant in role.requested_grants
                ],
                "instruction": role.instruction,
            }

        payload = {
            "schema": _JOURNEY_SCHEMA,
            "root_member_id": self._config.root_member_id,
            "root_agent_id": self._config.root_agent_id,
            "root_agent_version": self._config.root_agent_version,
            "root_grants": [
                grant.model_dump(mode="json") for grant in self._config.root_grants
            ],
            "worker": assignment(self._config.worker),
            "checker": assignment(self._config.checker),
            "quota": {
                "max_depth": 1,
                "max_children_per_parent": 2,
                "max_total_agents": 3,
                "max_parallel": 1,
            },
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _quota() -> TeamQuota:
        return TeamQuota(
            max_depth=1,
            max_children_per_parent=2,
            max_total_agents=3,
            max_parallel=1,
        )

    @staticmethod
    def _safe_observation_error(error: str | None) -> str | None:
        if error is None:
            return None
        if error in _SAFE_OBSERVATION_ERRORS:
            return error
        return "RuntimeFailure"

    def _validated_root_grants(self) -> tuple[ToolGrant, ...]:
        active = self._definitions.require_active(
            self._config.root_agent_id,
            self._config.root_agent_version,
        )
        return attenuate_grants(active.definition.tool_grants, self._config.root_grants)

    def _request(
        self,
        *,
        role: V01ChildAssignment,
        team_id: str,
        payload: dict[str, object],
    ) -> ChildRequest:
        return ChildRequest(
            member_id=role.member_id,
            agent_id=role.agent_id,
            agent_version=role.agent_version,
            thread_id=self._thread_id(team_id, role.member_id),
            requested_grants=role.requested_grants,
            payload=payload,
        )

    @staticmethod
    def _thread_id(team_id: str, member_id: str) -> str:
        return f"v01:{team_id}:{member_id}"

    @staticmethod
    def _compare_outputs(
        worker: V01ChildObservation,
        checker: V01ChildObservation,
    ) -> dict[str, object]:
        if checker.state is MemberState.COMPLETED:
            return {
                "status": (
                    "checked" if worker.state is MemberState.COMPLETED else "degraded"
                ),
                "worker_output": dict(worker.output),
                "checker_output": dict(checker.output),
                "worker_error": worker.error,
                "checker_error": checker.error,
            }
        if worker.state is MemberState.COMPLETED:
            return {
                "status": "worker_fallback",
                "worker_output": dict(worker.output),
                "checker_output": dict(checker.output),
                "worker_error": worker.error,
                "checker_error": checker.error,
            }
        return {
            "status": "failed",
            "worker_output": dict(worker.output),
            "checker_output": dict(checker.output),
            "worker_error": worker.error,
            "checker_error": checker.error,
        }
