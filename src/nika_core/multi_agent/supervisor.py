from __future__ import annotations

import asyncio
from dataclasses import dataclass

from nika_core.builder.repository import AgentDefinitionRepository
from nika_core.multi_agent.contracts import (
    AgentHandoff,
    ChildRequest,
    EvaluationScore,
    HandoffKind,
    MemberState,
    TeamMember,
    aggregate_scores,
    attenuate_grants,
)
from nika_core.multi_agent.store import MultiAgentStore
from nika_core.runtime.contracts import (
    AgentRuntimePort,
    RuntimeOutcome,
    RuntimeRequest,
    RuntimeResult,
)


@dataclass(frozen=True, slots=True)
class ChildExecution:
    member: TeamMember
    result: RuntimeResult | None
    exception: str | None = None


class MultiAgentSupervisor:
    """Bounded runtime-neutral supervisor over activated Nika agent definitions."""

    def __init__(
        self,
        *,
        runtime: AgentRuntimePort,
        store: MultiAgentStore,
        definitions: AgentDefinitionRepository,
    ) -> None:
        self._runtime = runtime
        self._store = store
        self._definitions = definitions

    async def fan_out(
        self,
        *,
        team_id: str,
        parent_id: str,
        requests: tuple[ChildRequest, ...],
    ) -> tuple[ChildExecution, ...]:
        quota = self._store.quota(team_id)
        if len(requests) > quota.max_children_per_parent:
            raise RuntimeError("fan-out exceeds children-per-parent quota")

        parent = self._store.member(team_id, parent_id)
        self._definitions.require_active(parent.agent_id, parent.agent_version)
        self._validate_child_requests(requests)

        members = tuple(
            self._store.spawn_child(
                team_id=team_id,
                parent_id=parent_id,
                child_id=request.member_id,
                agent_id=request.agent_id,
                agent_version=request.agent_version,
                thread_id=request.thread_id,
                requested_grants=request.requested_grants,
            )
            for request in requests
        )
        by_id = {request.member_id: request for request in requests}
        semaphore = asyncio.Semaphore(quota.max_parallel)

        async def run_child(member: TeamMember) -> ChildExecution:
            request = by_id[member.member_id]
            async with semaphore:
                self._store.set_member_state(
                    team_id=team_id,
                    member_id=member.member_id,
                    state=MemberState.RUNNING,
                )
                self._store.record_handoff(
                    AgentHandoff(
                        team_id=team_id,
                        sender_id=parent_id,
                        recipient_id=member.member_id,
                        kind=HandoffKind.TASK,
                        payload=request.payload,
                    )
                )
                try:
                    result = await self._runtime.run(
                        RuntimeRequest(
                            task_id=f"team:{team_id}:{member.member_id}",
                            thread_id=member.thread_id,
                            payload={
                                "team_id": team_id,
                                "parent_id": parent_id,
                                "member_id": member.member_id,
                                "agent_id": member.agent_id,
                                "agent_version": member.agent_version,
                                "tool_grants": [
                                    grant.model_dump(mode="json") for grant in member.tool_grants
                                ],
                                "handoff": request.payload,
                            },
                        )
                    )
                except asyncio.CancelledError:
                    await self._runtime.cancel(
                        task_id=f"team:{team_id}:{member.member_id}",
                        thread_id=member.thread_id,
                    )
                    self._store.set_member_state(
                        team_id=team_id,
                        member_id=member.member_id,
                        state=MemberState.CANCELLED,
                    )
                    raise
                except Exception as exc:  # noqa: BLE001 - isolate one worker from the team.
                    self._store.set_member_state(
                        team_id=team_id,
                        member_id=member.member_id,
                        state=MemberState.FAILED,
                    )
                    self._store.record_result(
                        team_id=team_id,
                        member_id=member.member_id,
                        outcome="exception",
                        error=type(exc).__name__,
                    )
                    return ChildExecution(member=member, result=None, exception=type(exc).__name__)

                state = self._state_for_result(result)
                self._store.set_member_state(
                    team_id=team_id,
                    member_id=member.member_id,
                    state=state,
                    resume_token=result.resume_token,
                )
                self._store.record_result(
                    team_id=team_id,
                    member_id=member.member_id,
                    outcome=result.outcome.value,
                    payload=dict(result.output),
                    error=result.error,
                )
                self._store.record_handoff(
                    AgentHandoff(
                        team_id=team_id,
                        sender_id=member.member_id,
                        recipient_id=parent_id,
                        kind=(
                            HandoffKind.ERROR
                            if result.outcome == RuntimeOutcome.FAILED
                            else HandoffKind.RESULT
                        ),
                        payload=dict(result.output),
                    )
                )
                return ChildExecution(member=member, result=result)

        return tuple(await asyncio.gather(*(run_child(member) for member in members)))

    async def cancel_team(self, team_id: str) -> tuple[TeamMember, ...]:
        active = self._store.recoverable_members(team_id)
        for member in active:
            await self._runtime.cancel(
                task_id=f"team:{team_id}:{member.member_id}",
                thread_id=member.thread_id,
            )
        return self._store.cancel_team(team_id)

    @staticmethod
    def aggregate_evaluations(scores: tuple[EvaluationScore, ...]) -> dict[str, float]:
        return aggregate_scores(scores)

    def _validate_child_requests(self, requests: tuple[ChildRequest, ...]) -> None:
        seen_member_ids: set[str] = set()
        seen_thread_ids: set[str] = set()
        for request in requests:
            if request.member_id in seen_member_ids:
                raise ValueError(f"duplicate child member_id: {request.member_id}")
            if request.thread_id in seen_thread_ids:
                raise ValueError(f"duplicate child thread_id: {request.thread_id}")
            seen_member_ids.add(request.member_id)
            seen_thread_ids.add(request.thread_id)

            stored = self._definitions.require_active(request.agent_id, request.agent_version)
            try:
                attenuate_grants(stored.definition.tool_grants, request.requested_grants)
            except PermissionError as exc:
                raise PermissionError(
                    f"child request exceeds activated definition {request.agent_id}:{request.agent_version}: {exc}"
                ) from exc

    @staticmethod
    def _state_for_result(result: RuntimeResult) -> MemberState:
        return {
            RuntimeOutcome.COMPLETED: MemberState.COMPLETED,
            RuntimeOutcome.WAITING_APPROVAL: MemberState.WAITING_APPROVAL,
            RuntimeOutcome.PAUSED: MemberState.RUNNING,
            RuntimeOutcome.CANCELLED: MemberState.CANCELLED,
            RuntimeOutcome.FAILED: MemberState.FAILED,
        }[result.outcome]
