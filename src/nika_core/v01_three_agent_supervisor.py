from __future__ import annotations

from dataclasses import dataclass

from nika_core.builder.repository import AgentDefinitionRepository
from nika_core.builder.spec import ToolGrant
from nika_core.multi_agent.contracts import (
    ChildRequest,
    MemberState,
    TeamQuota,
    TeamState,
    attenuate_grants,
)
from nika_core.multi_agent.store import MultiAgentStore
from nika_core.multi_agent.supervisor import ChildExecution, MultiAgentSupervisor


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
        if self.worker.member_id == self.checker.member_id:
            raise ValueError("worker and checker member_id values must differ")


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
        self._store.create_team(
            team_id=fixed_team_id,
            root_member_id=self._config.root_member_id,
            root_agent_id=self._config.root_agent_id,
            root_agent_version=self._config.root_agent_version,
            root_thread_id=self._thread_id(fixed_team_id, self._config.root_member_id),
            root_grants=root_grants,
            quota=TeamQuota(
                max_depth=1,
                max_children_per_parent=2,
                max_total_agents=3,
                max_parallel=1,
            ),
        )

        worker_execution = (
            await self._coordinator.fan_out(
                team_id=fixed_team_id,
                parent_id=self._config.root_member_id,
                requests=(
                    self._request(
                        role=self._config.worker,
                        team_id=fixed_team_id,
                        payload={
                            "shared_task_id": task_id,
                            "user_goal": goal,
                            "stage": "worker",
                            "assignment": self._config.worker.instruction,
                        },
                    ),
                ),
            )
        )[0]
        worker = self._observe(worker_execution)

        checker_execution = (
            await self._coordinator.fan_out(
                team_id=fixed_team_id,
                parent_id=self._config.root_member_id,
                requests=(
                    self._request(
                        role=self._config.checker,
                        team_id=fixed_team_id,
                        payload={
                            "shared_task_id": task_id,
                            "user_goal": goal,
                            "stage": "checker",
                            "assignment": self._config.checker.instruction,
                            "worker_observation": worker.as_payload(),
                        },
                    ),
                ),
            )
        )[0]
        checker = self._observe(checker_execution)

        team_state = self._coordinator.finalize_team(fixed_team_id)
        return V01ThreeAgentResult(
            shared_task_id=task_id,
            team_id=fixed_team_id,
            team_state=team_state,
            worker=worker,
            checker=checker,
            final_output=self._terminal_output(worker, checker),
        )

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
    def _observe(execution: ChildExecution) -> V01ChildObservation:
        output = dict(execution.result.output) if execution.result is not None else {}
        result_error = execution.result.error if execution.result is not None else None
        return V01ChildObservation(
            member_id=execution.member.member_id,
            state=execution.member.state,
            output=output,
            error=execution.exception or result_error,
        )

    @staticmethod
    def _terminal_output(
        worker: V01ChildObservation,
        checker: V01ChildObservation,
    ) -> dict[str, object]:
        if checker.state is MemberState.COMPLETED:
            return dict(checker.output)
        if worker.state is MemberState.COMPLETED:
            return {
                "status": "worker_fallback",
                "worker_output": dict(worker.output),
                "checker_error": checker.error,
            }
        return {
            "status": "failed",
            "worker_error": worker.error,
            "checker_error": checker.error,
        }
