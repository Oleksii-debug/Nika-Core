from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from nika_core.builder.repository import AgentDefinitionRepository
from nika_core.builder.spec import ToolGrant
from nika_core.multi_agent.checker import CheckerStatus, V01CheckerAgent
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
from nika_core.multi_agent.research_results import SourceInspectionAssignment
from nika_core.multi_agent.store import MultiAgentStore
from nika_core.multi_agent.supervisor import MultiAgentSupervisor
from nika_core.research.models import SourceSpec
from nika_core.runtime.contracts import RuntimeErrorCode

_JOURNEY_SCHEMA = "nika.v01.three-agent-source-monitoring:v3"
_WORKER_TASK_SCHEMA = "nika.v01.source-worker-task:v1"
_RESULT_SCHEMA = "nika.v01.three-agent-result:v3"
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
    """The operational checker/root identity for the fixed V0.1 team."""

    member_id: str
    agent_id: str
    agent_version: int
    requested_grants: tuple[ToolGrant, ...]
    instruction: str

    def __post_init__(self) -> None:
        _validate_role(self)


@dataclass(frozen=True, slots=True)
class V01SourceWorkerAssignment:
    """One independently bounded source worker in Scenario A."""

    member_id: str
    agent_id: str
    agent_version: int
    requested_grants: tuple[ToolGrant, ...]
    instruction: str
    source: SourceSpec
    max_items: int = 20

    def __post_init__(self) -> None:
        _validate_role(self)
        if not isinstance(self.source, SourceSpec):
            raise TypeError("source must be a SourceSpec")
        for label, value in (
            ("source_id", self.source.source_id),
            ("workspace_id", self.source.workspace_id),
            ("locator", self.source.locator),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{label} must not be empty")
        if isinstance(self.max_items, bool) or not isinstance(self.max_items, int):
            raise TypeError("max_items must be an integer")
        if not 1 <= self.max_items <= 100:
            raise ValueError("max_items must be between 1 and 100")

    def bind(self, *, team_id: str, task_id: str) -> SourceInspectionAssignment:
        identity = {
            "team_id": team_id,
            "task_id": task_id,
            "member_id": self.member_id,
            "source_id": self.source.source_id,
            "workspace_id": self.source.workspace_id,
            "source_kind": self.source.kind.value,
            "locator": self.source.locator,
        }
        digest = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:24]
        return SourceInspectionAssignment(
            team_id=team_id,
            task_id=task_id,
            assignment_id=f"assignment:{digest}:{self.member_id}",
            member_id=self.member_id,
            source=self.source,
            tool_call_id=f"tool-call:{digest}:{self.member_id}",
            effect_id=f"effect:{digest}:{self.member_id}",
            max_items=self.max_items,
        )


@dataclass(frozen=True, slots=True)
class V01ThreeAgentConfig:
    checker: V01ChildAssignment
    workers: tuple[V01SourceWorkerAssignment, V01SourceWorkerAssignment]

    def __post_init__(self) -> None:
        if len(self.workers) != 2:
            raise ValueError("V0.1 Scenario A requires exactly two source workers")
        member_ids = [self.checker.member_id, *(worker.member_id for worker in self.workers)]
        if len(member_ids) != len(set(member_ids)):
            raise ValueError("checker and worker member identities must differ")
        source_ids = [worker.source.source_id for worker in self.workers]
        locators = [worker.source.locator for worker in self.workers]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("V0.1 Scenario A requires two distinct source identities")
        if len(locators) != len(set(locators)):
            raise ValueError("V0.1 Scenario A requires two distinct source locators")


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
    workers: tuple[V01ChildObservation, V01ChildObservation]
    checker: V01ChildObservation
    final_output: dict[str, object]


class V01ThreeAgentSupervisor:
    """Scenario A composition: two durable source workers and one root checker."""

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
        self._checker = V01CheckerAgent()

    async def run(
        self,
        *,
        user_goal: str,
        shared_task_id: str,
        team_id: str,
    ) -> V01ThreeAgentResult:
        goal = _required_text(user_goal, "user_goal")
        task_id = _required_text(shared_task_id, "shared_task_id")
        fixed_team_id = _required_text(team_id, "team_id")
        checker_grants = self._validated_checker_grants()
        for worker in self._config.workers:
            self._validated_worker_grants(worker, checker_grants)

        assignments = tuple(
            worker.bind(team_id=fixed_team_id, task_id=task_id)
            for worker in self._config.workers
        )
        self._ensure_team(
            team_id=fixed_team_id,
            shared_task_id=task_id,
            user_goal=goal,
            checker_grants=checker_grants,
            assignments=assignments,
        )
        workers = await self._ensure_workers(
            team_id=fixed_team_id,
            shared_task_id=task_id,
            user_goal=goal,
            checker_grants=checker_grants,
            assignments=assignments,
        )
        handoffs = self._store.inbound_result_handoffs(
            fixed_team_id,
            self._config.checker.member_id,
        )
        expected_summary = self._checker.compare(
            team_id=fixed_team_id,
            task_id=task_id,
            checker_id=self._config.checker.member_id,
            assignments=assignments,
            handoffs=handoffs,
        )
        checker = await self._ensure_checker(team_id=fixed_team_id)
        team_state = self._coordinator.finalize_team(fixed_team_id)
        summary_payload = expected_summary.to_payload()
        checker_validated = (
            checker.state is MemberState.COMPLETED
            and self._checker_output(checker.output) == summary_payload
        )
        status = (
            expected_summary.status.value
            if checker_validated
            else CheckerStatus.EVIDENCE_INVALID.value
        )
        return V01ThreeAgentResult(
            shared_task_id=task_id,
            team_id=fixed_team_id,
            team_state=team_state,
            workers=(workers[0], workers[1]),
            checker=checker,
            final_output={
                "schema": _RESULT_SCHEMA,
                "team_id": fixed_team_id,
                "task_id": task_id,
                "status": status,
                "checker_output_validated": checker_validated,
                "workers": [worker.as_payload() for worker in workers],
                "checker": checker.as_payload(),
                "checker_summary": summary_payload,
            },
        )

    def _ensure_team(
        self,
        *,
        team_id: str,
        shared_task_id: str,
        user_goal: str,
        checker_grants: tuple[ToolGrant, ...],
        assignments: tuple[SourceInspectionAssignment, ...],
    ) -> None:
        checker = self._config.checker
        root_payload = self._checker_task_payload(
            shared_task_id=shared_task_id,
            user_goal=user_goal,
            assignments=assignments,
        )
        try:
            state = self._store.team_state(team_id)
        except KeyError:
            self._store.create_team(
                team_id=team_id,
                root_member_id=checker.member_id,
                root_agent_id=checker.agent_id,
                root_agent_version=checker.agent_version,
                root_thread_id=self._thread_id(team_id, checker.member_id),
                root_grants=checker_grants,
                quota=self._quota(),
                root_task_handoff=AgentHandoff(
                    team_id=team_id,
                    sender_id=checker.member_id,
                    recipient_id=checker.member_id,
                    kind=HandoffKind.TASK,
                    payload=root_payload,
                    handoff_id=f"task:{team_id}:{checker.member_id}",
                    correlation_id=f"team:{team_id}:{checker.member_id}:root-task",
                ),
            )
            return

        if state not in {TeamState.ACTIVE, TeamState.COMPLETED}:
            raise RuntimeError(f"V0.1 team cannot resume from state: {state.value}")
        if self._store.quota(team_id) != self._quota():
            raise PermissionError("existing team quota does not match V0.1 journey")
        root = self._store.member(team_id, checker.member_id)
        if (
            root.parent_id is not None
            or root.depth != 0
            or root.agent_id != checker.agent_id
            or root.agent_version != checker.agent_version
            or root.thread_id != self._thread_id(team_id, checker.member_id)
            or root.tool_grants != checker_grants
        ):
            raise PermissionError("existing checker root does not match V0.1 journey")
        if self._store.task_payload(team_id, root.member_id) != root_payload:
            raise PermissionError("existing team task identity does not match V0.1 journey")
        expected_ids = {checker.member_id, *(worker.member_id for worker in self._config.workers)}
        actual_ids = {member.member_id for member in self._store.members(team_id)}
        if not actual_ids.issubset(expected_ids):
            raise PermissionError("existing team contains a member outside V0.1 journey")
        if state is TeamState.COMPLETED and actual_ids != expected_ids:
            raise RuntimeError("completed V0.1 team is missing durable member evidence")

    async def _ensure_workers(
        self,
        *,
        team_id: str,
        shared_task_id: str,
        user_goal: str,
        checker_grants: tuple[ToolGrant, ...],
        assignments: tuple[SourceInspectionAssignment, ...],
    ) -> tuple[V01ChildObservation, ...]:
        roles = dict(zip((item.member_id for item in assignments), self._config.workers))
        assignment_by_id = {item.member_id: item for item in assignments}
        existing = {member.member_id: member for member in self._store.members(team_id)}
        for member_id, member in existing.items():
            role = roles.get(member_id)
            if role is not None:
                self._validate_existing_worker(
                    existing=member,
                    role=role,
                    assignment=assignment_by_id[member_id],
                    team_id=team_id,
                    shared_task_id=shared_task_id,
                    user_goal=user_goal,
                    checker_grants=checker_grants,
                )

        if any(
            member_id in existing
            and existing[member_id].state in {MemberState.SPAWNED, MemberState.RUNNING}
            for member_id in roles
        ):
            await self._coordinator.recover_team(team_id)
            existing = {member.member_id: member for member in self._store.members(team_id)}

        missing = [member_id for member_id in roles if member_id not in existing]
        if missing:
            if self._store.team_state(team_id) is not TeamState.ACTIVE:
                raise RuntimeError("terminal V0.1 team cannot create missing worker evidence")
            await self._coordinator.fan_out(
                team_id=team_id,
                parent_id=self._config.checker.member_id,
                requests=tuple(
                    self._worker_request(
                        role=roles[member_id],
                        assignment=assignment_by_id[member_id],
                        team_id=team_id,
                        shared_task_id=shared_task_id,
                        user_goal=user_goal,
                    )
                    for member_id in missing
                ),
            )

        return tuple(
            self._observe_terminal(team_id=team_id, member_id=worker.member_id)
            for worker in self._config.workers
        )

    async def _ensure_checker(self, *, team_id: str) -> V01ChildObservation:
        checker_id = self._config.checker.member_id
        member = self._store.member(team_id, checker_id)
        if member.state in {MemberState.SPAWNED, MemberState.RUNNING}:
            if self._store.team_state(team_id) is not TeamState.ACTIVE:
                raise RuntimeError("terminal V0.1 team cannot run missing checker evidence")
            await self._coordinator.run_root_member(team_id=team_id, member_id=checker_id)
        return self._observe_terminal(team_id=team_id, member_id=checker_id)

    def _observe_terminal(self, *, team_id: str, member_id: str) -> V01ChildObservation:
        member = self._store.member(team_id, member_id)
        if member.state in {
            MemberState.SPAWNED,
            MemberState.RUNNING,
            MemberState.WAITING_APPROVAL,
            MemberState.PAUSED,
        }:
            raise RuntimeError(
                f"V0.1 member has no terminal durable result: {member.state.value}"
            )
        result = self._store.member_result(team_id, member_id)
        allowed_outcomes = {
            MemberState.COMPLETED: frozenset({"completed"}),
            MemberState.FAILED: frozenset({"exception", "failed"}),
            MemberState.CANCELLED: frozenset({"cancelled"}),
        }
        if result.outcome not in allowed_outcomes.get(member.state, frozenset()):
            raise RuntimeError("durable member state conflicts with its result outcome")
        return V01ChildObservation(
            member_id=member.member_id,
            state=member.state,
            output=dict(result.payload),
            error=self._safe_observation_error(result.error),
        )

    def _validate_existing_worker(
        self,
        *,
        existing: TeamMember,
        role: V01SourceWorkerAssignment,
        assignment: SourceInspectionAssignment,
        team_id: str,
        shared_task_id: str,
        user_goal: str,
        checker_grants: tuple[ToolGrant, ...],
    ) -> None:
        expected_grants = self._validated_worker_grants(role, checker_grants)
        if (
            existing.parent_id != self._config.checker.member_id
            or existing.depth != 1
            or existing.agent_id != role.agent_id
            or existing.agent_version != role.agent_version
            or existing.thread_id != self._thread_id(team_id, role.member_id)
            or existing.tool_grants != expected_grants
        ):
            raise PermissionError("existing worker does not match V0.1 journey")
        expected_payload = self._worker_task_payload(
            role=role,
            assignment=assignment,
            shared_task_id=shared_task_id,
            user_goal=user_goal,
        )
        if self._store.task_payload(team_id, role.member_id) != expected_payload:
            raise PermissionError("existing worker task does not match V0.1 journey")

    def _validated_checker_grants(self) -> tuple[ToolGrant, ...]:
        checker = self._config.checker
        active = self._definitions.require_active(checker.agent_id, checker.agent_version)
        return attenuate_grants(active.definition.tool_grants, checker.requested_grants)

    def _validated_worker_grants(
        self,
        worker: V01SourceWorkerAssignment,
        checker_grants: tuple[ToolGrant, ...],
    ) -> tuple[ToolGrant, ...]:
        active = self._definitions.require_active(worker.agent_id, worker.agent_version)
        attenuate_grants(active.definition.tool_grants, worker.requested_grants)
        return attenuate_grants(checker_grants, worker.requested_grants)

    def _checker_task_payload(
        self,
        *,
        shared_task_id: str,
        user_goal: str,
        assignments: tuple[SourceInspectionAssignment, ...],
    ) -> dict[str, object]:
        return {
            "schema": _JOURNEY_SCHEMA,
            "shared_task_id": shared_task_id,
            "user_goal": user_goal,
            "stage": "checker",
            "instruction": self._config.checker.instruction,
            "source_assignments": [assignment.to_payload() for assignment in assignments],
            "config_fingerprint": self._config_fingerprint(),
        }

    @staticmethod
    def _worker_task_payload(
        *,
        role: V01SourceWorkerAssignment,
        assignment: SourceInspectionAssignment,
        shared_task_id: str,
        user_goal: str,
    ) -> dict[str, object]:
        return {
            "schema": _WORKER_TASK_SCHEMA,
            "shared_task_id": shared_task_id,
            "user_goal": user_goal,
            "stage": "source_worker",
            "instruction": role.instruction,
            "source_assignment": assignment.to_payload(),
        }

    def _worker_request(
        self,
        *,
        role: V01SourceWorkerAssignment,
        assignment: SourceInspectionAssignment,
        team_id: str,
        shared_task_id: str,
        user_goal: str,
    ) -> ChildRequest:
        return ChildRequest(
            member_id=role.member_id,
            agent_id=role.agent_id,
            agent_version=role.agent_version,
            thread_id=self._thread_id(team_id, role.member_id),
            requested_grants=role.requested_grants,
            payload=self._worker_task_payload(
                role=role,
                assignment=assignment,
                shared_task_id=shared_task_id,
                user_goal=user_goal,
            ),
        )

    def _config_fingerprint(self) -> str:
        checker = self._config.checker
        payload = {
            "schema": _JOURNEY_SCHEMA,
            "checker": _role_payload(checker),
            "workers": [
                {
                    **_role_payload(worker),
                    "source": {
                        "source_id": worker.source.source_id,
                        "workspace_id": worker.source.workspace_id,
                        "source_kind": worker.source.kind.value,
                        "locator": worker.source.locator,
                    },
                    "max_items": worker.max_items,
                }
                for worker in self._config.workers
            ],
            "quota": {
                "max_depth": 1,
                "max_children_per_parent": 2,
                "max_total_agents": 3,
                "max_parallel": 2,
            },
        }
        return hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _checker_output(output: dict[str, object]) -> object:
        nested = output.get("checker_summary")
        return nested if nested is not None else output

    @staticmethod
    def _quota() -> TeamQuota:
        return TeamQuota(
            max_depth=1,
            max_children_per_parent=2,
            max_total_agents=3,
            max_parallel=2,
        )

    @staticmethod
    def _safe_observation_error(error: str | None) -> str | None:
        if error is None:
            return None
        if error in _SAFE_OBSERVATION_ERRORS:
            return error
        return "RuntimeFailure"

    @staticmethod
    def _thread_id(team_id: str, member_id: str) -> str:
        return f"v01:{team_id}:{member_id}"


def _role_payload(role: V01ChildAssignment | V01SourceWorkerAssignment) -> dict[str, object]:
    return {
        "member_id": role.member_id,
        "agent_id": role.agent_id,
        "agent_version": role.agent_version,
        "requested_grants": [
            grant.model_dump(mode="json") for grant in role.requested_grants
        ],
        "instruction": role.instruction,
    }


def _validate_role(role: V01ChildAssignment | V01SourceWorkerAssignment) -> None:
    if not role.member_id.strip() or not role.agent_id.strip():
        raise ValueError("role identifiers must not be empty")
    if role.agent_version < 1:
        raise ValueError("role agent_version must be positive")
    if not role.instruction.strip():
        raise ValueError("role instruction must not be empty")


def _required_text(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must not be empty")
    return value.strip()
