from __future__ import annotations

import asyncio
from dataclasses import dataclass

from nika_core.multi_agent.contracts import (
    AgentHandoff,
    ChildRequest,
    HandoffKind,
    MemberState,
    TeamMember,
    aggregate_scores,
    EvaluationScore,
)
from nika_core.multi_agent.store import MultiAgentStore
from nika_core.runtime.contracts import AgentRuntimePort, RuntimeOutcome, RuntimeRequest, RuntimeResult


@dataclass(frozen=True, slots=True)
class ChildExecution:
    member: TeamMember
    result: RuntimeResult | None
    exception: str | None = None


class MultiAgentSupervisor:
    """Bounded runtime-neutral supervisor over the existing AgentRuntimePort."""

    def __init__(self, *, runtime: AgentRuntimePort, store: MultiAgentStore) -> None:
        self._runtime = runtime
        self._store = store

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
                except Exception as exc:  # runtime boundary must contain worker failure
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

    @staticmethod
    def _state_for_result(result: RuntimeResult) -> MemberState:
        return {
            RuntimeOutcome.COMPLETED: MemberState.COMPLETED,
            RuntimeOutcome.WAITING_APPROVAL: MemberState.WAITING_APPROVAL,
            RuntimeOutcome.PAUSED: MemberState.RUNNING,
            RuntimeOutcome.CANCELLED: MemberState.CANCELLED,
            RuntimeOutcome.FAILED: MemberState.FAILED,
        }[result.outcome]
