from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from math import isfinite

from nika_core.builder.repository import AgentDefinitionRepository
from nika_core.multi_agent.contracts import (
    AgentHandoff,
    ChildRequest,
    EvaluationScore,
    HandoffKind,
    MemberState,
    TeamMember,
    TeamState,
    aggregate_scores,
    attenuate_grants,
)
from nika_core.multi_agent.store import MultiAgentStore
from nika_core.runtime.contracts import (
    AgentRuntimePort,
    RuntimeCapability,
    RuntimeErrorCode,
    RuntimeOutcome,
    RuntimeRequest,
    RuntimeResult,
    RuntimeResumeMode,
    RuntimeResumeRequest,
)

_SAFE_EXCEPTION_NAMES = frozenset(
    {
        "AssertionError",
        "KeyError",
        "OSError",
        "PermissionError",
        "RuntimeError",
        "TimeoutError",
        "TypeError",
        "ValueError",
    }
)
_DEFAULT_RUNTIME_TIMEOUT_SECONDS = 300.0


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
        runtime_timeout_seconds: float = _DEFAULT_RUNTIME_TIMEOUT_SECONDS,
    ) -> None:
        timeout = float(runtime_timeout_seconds)
        if not isfinite(timeout) or timeout <= 0:
            raise ValueError("runtime_timeout_seconds must be a positive finite value")
        self._runtime = runtime
        self._store = store
        self._definitions = definitions
        self._runtime_timeout_seconds = timeout
        self._recovering_teams: set[str] = set()

    async def run_root_member(self, *, team_id: str, member_id: str) -> ChildExecution:
        """Execute or crash-resume the durable root member through AgentRuntimePort once.

        The V0.1 representative team stores the supervisor as the root member. This method
        makes that same durable identity operational without adding another coordinator or
        another agent. Terminal roots are replayed from durable evidence and PAUSED roots stay
        paused until an explicit resume path is selected by the caller.
        """
        if self._store.team_state(team_id) is not TeamState.ACTIVE:
            raise RuntimeError("team is not active")
        member = self._store.member(team_id, member_id)
        if member.parent_id is not None:
            raise ValueError("root execution requires the team root member")
        self._definitions.require_active(member.agent_id, member.agent_version)

        if member.state in {
            MemberState.COMPLETED,
            MemberState.FAILED,
            MemberState.CANCELLED,
            MemberState.WAITING_APPROVAL,
            MemberState.PAUSED,
        }:
            return ChildExecution(member=member, result=None)
        if member.state is MemberState.SPAWNED:
            return await self._run_new_root(member)
        if member.state is MemberState.RUNNING:
            return await self._recover_root(member)
        raise RuntimeError(f"unsupported root member state: {member.state.value}")

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
        resume_tokens = {
            request.member_id: self._initial_resume_token(
                team_id=team_id,
                member_id=request.member_id,
                thread_id=request.thread_id,
            )
            for request in requests
        }

        members = tuple(
            self._store.spawn_child(
                team_id=team_id,
                parent_id=parent_id,
                child_id=request.member_id,
                agent_id=request.agent_id,
                agent_version=request.agent_version,
                thread_id=request.thread_id,
                requested_grants=request.requested_grants,
                task_handoff=AgentHandoff(
                    team_id=team_id,
                    sender_id=parent_id,
                    recipient_id=request.member_id,
                    kind=HandoffKind.TASK,
                    payload=request.payload,
                    handoff_id=f"task:{team_id}:{request.member_id}",
                    correlation_id=f"team:{team_id}:{parent_id}:{request.member_id}",
                ),
            )
            for request in requests
        )
        by_id = {request.member_id: request for request in requests}
        semaphore = asyncio.Semaphore(quota.max_parallel)

        async def run_child(member: TeamMember) -> ChildExecution:
            request = by_id[member.member_id]
            async with semaphore:
                return await self._run_new_child(
                    member,
                    request.payload,
                    resume_token=resume_tokens[member.member_id],
                )

        return tuple(await asyncio.gather(*(run_child(member) for member in members)))

    async def recover_team(self, team_id: str) -> tuple[ChildExecution, ...]:
        """Recover persisted child work once per team for this supervisor instance.

        WAITING_APPROVAL and PAUSED children remain untouched because resuming either requires an
        explicit decision. SPAWNED children are safe to start from their persisted TASK handoff;
        RUNNING children resume only from the recovery cursor persisted before execution.
        A duplicate concurrent recovery call on the same supervisor returns no work rather than
        issuing a second runtime resume while the first recovery attempt is still active.
        """
        if team_id in self._recovering_teams:
            return ()
        self._recovering_teams.add(team_id)
        try:
            if self._store.team_state(team_id) is not TeamState.ACTIVE:
                raise RuntimeError("team is not active")
            quota = self._store.quota(team_id)
            candidates = tuple(
                member
                for member in self._store.recoverable_children(team_id)
                if member.state in {MemberState.SPAWNED, MemberState.RUNNING}
            )
            semaphore = asyncio.Semaphore(quota.max_parallel)

            async def recover_child(member: TeamMember) -> ChildExecution:
                async with semaphore:
                    return await self._recover_child(member)

            return tuple(await asyncio.gather(*(recover_child(member) for member in candidates)))
        finally:
            self._recovering_teams.discard(team_id)

    def finalize_team(self, team_id: str) -> TeamState:
        """Explicitly close a team once no further fan-out is planned."""
        return self._store.finalize_team(team_id)

    async def cancel_team(self, team_id: str) -> tuple[TeamMember, ...]:
        state = self._store.team_state(team_id)
        if state is TeamState.CANCELLED:
            return self._store.members(team_id)
        if state is not TeamState.ACTIVE:
            raise RuntimeError(f"team cannot be cancelled from terminal state: {state.value}")
        active = self._store.recoverable_members(team_id)
        for member in active:
            await self._runtime.cancel(
                task_id=self._task_id(team_id, member.member_id),
                thread_id=member.thread_id,
            )
        return self._store.cancel_team(team_id)

    @staticmethod
    def aggregate_evaluations(scores: tuple[EvaluationScore, ...]) -> dict[str, float]:
        return aggregate_scores(scores)

    async def _run_new_root(self, member: TeamMember) -> ChildExecution:
        resume_token = self._initial_resume_token(
            team_id=member.team_id,
            member_id=member.member_id,
            thread_id=member.thread_id,
        )
        self._store.set_member_state(
            team_id=member.team_id,
            member_id=member.member_id,
            state=MemberState.RUNNING,
            resume_token=resume_token,
        )
        prepared = self._store.member(member.team_id, member.member_id)
        payload = self._store.task_payload(member.team_id, member.member_id)
        inbound_handoffs = self._store.inbound_result_handoffs(
            member.team_id,
            member.member_id,
        )
        try:
            result = await self._runtime.run(
                RuntimeRequest(
                    task_id=self._task_id(member.team_id, member.member_id),
                    thread_id=member.thread_id,
                    payload=self._runtime_payload(
                        prepared,
                        payload,
                        inbound_handoffs=inbound_handoffs,
                    ),
                    timeout_seconds=self._runtime_timeout_seconds,
                )
            )
        except asyncio.CancelledError:
            await self._runtime.cancel(
                task_id=self._task_id(member.team_id, member.member_id),
                thread_id=member.thread_id,
            )
            self._store.set_member_state(
                team_id=member.team_id,
                member_id=member.member_id,
                state=MemberState.CANCELLED,
            )
            raise
        except Exception as exc:  # noqa: BLE001 - isolate root runtime failure from persistence.
            return self._finish_exception(prepared, exc)
        return self._finish_result(prepared, result)

    async def _recover_root(self, member: TeamMember) -> ChildExecution:
        try:
            if not member.resume_token:
                raise RuntimeError("running root has no durable resume token")
            result = await self._runtime.resume(
                RuntimeResumeRequest(
                    task_id=self._task_id(member.team_id, member.member_id),
                    thread_id=member.thread_id,
                    resume_token=member.resume_token,
                    mode=RuntimeResumeMode.CONTINUE,
                    timeout_seconds=self._runtime_timeout_seconds,
                )
            )
        except asyncio.CancelledError:
            await self._runtime.cancel(
                task_id=self._task_id(member.team_id, member.member_id),
                thread_id=member.thread_id,
            )
            self._store.set_member_state(
                team_id=member.team_id,
                member_id=member.member_id,
                state=MemberState.CANCELLED,
            )
            raise
        except Exception as exc:  # noqa: BLE001 - recovery failure belongs to root execution.
            current = self._store.member(member.team_id, member.member_id)
            return self._finish_exception(current, exc)
        current = self._store.member(member.team_id, member.member_id)
        return self._finish_result(current, result)

    async def _run_new_child(
        self,
        member: TeamMember,
        handoff_payload: dict[str, object],
        *,
        resume_token: str | None,
    ) -> ChildExecution:
        prepared = self._store.prepare_member_execution(
            team_id=member.team_id,
            member_id=member.member_id,
            resume_token=resume_token,
        )
        try:
            result = await self._runtime.run(
                RuntimeRequest(
                    task_id=self._task_id(member.team_id, member.member_id),
                    thread_id=member.thread_id,
                    payload=self._runtime_payload(prepared, handoff_payload),
                    timeout_seconds=self._runtime_timeout_seconds,
                )
            )
        except asyncio.CancelledError:
            await self._runtime.cancel(
                task_id=self._task_id(member.team_id, member.member_id),
                thread_id=member.thread_id,
            )
            self._store.set_member_state(
                team_id=member.team_id,
                member_id=member.member_id,
                state=MemberState.CANCELLED,
            )
            raise
        except Exception as exc:  # noqa: BLE001 - isolate one worker from the team.
            return self._finish_exception(prepared, exc)
        return self._finish_result(prepared, result)

    async def _recover_child(self, member: TeamMember) -> ChildExecution:
        try:
            self._definitions.require_active(member.agent_id, member.agent_version)
            if member.state is MemberState.SPAWNED:
                payload = self._store.task_payload(member.team_id, member.member_id)
                token = self._initial_resume_token(
                    team_id=member.team_id,
                    member_id=member.member_id,
                    thread_id=member.thread_id,
                )
                return await self._run_new_child(member, payload, resume_token=token)
            if member.state is not MemberState.RUNNING:
                raise RuntimeError(f"member state is not auto-recoverable: {member.state.value}")
            if not member.resume_token:
                raise RuntimeError("running child has no durable resume token")
            result = await self._runtime.resume(
                RuntimeResumeRequest(
                    task_id=self._task_id(member.team_id, member.member_id),
                    thread_id=member.thread_id,
                    resume_token=member.resume_token,
                    mode=RuntimeResumeMode.CONTINUE,
                    timeout_seconds=self._runtime_timeout_seconds,
                )
            )
        except asyncio.CancelledError:
            await self._runtime.cancel(
                task_id=self._task_id(member.team_id, member.member_id),
                thread_id=member.thread_id,
            )
            self._store.set_member_state(
                team_id=member.team_id,
                member_id=member.member_id,
                state=MemberState.CANCELLED,
            )
            raise
        except Exception as exc:  # noqa: BLE001 - recovery failure belongs to this child.
            current = self._store.member(member.team_id, member.member_id)
            return self._finish_exception(current, exc)
        current = self._store.member(member.team_id, member.member_id)
        return self._finish_result(current, result)

    def _finish_exception(self, member: TeamMember, exc: Exception) -> ChildExecution:
        exception_name = type(exc).__name__
        error = (
            exception_name if exception_name in _SAFE_EXCEPTION_NAMES else "WorkerException"
        )
        result_handoff = (
            None
            if member.parent_id is None
            else self._result_handoff(
                member,
                kind=HandoffKind.ERROR,
                payload={"error": error},
            )
        )
        updated = self._store.finish_member_execution(
            team_id=member.team_id,
            member_id=member.member_id,
            state=MemberState.FAILED,
            outcome="exception",
            error=error,
            result_handoff=result_handoff,
        )
        return ChildExecution(member=updated, result=None, exception=error)

    def _finish_result(self, member: TeamMember, result: RuntimeResult) -> ChildExecution:
        safe_error = self._safe_runtime_error(result)
        safe_result = replace(result, error=safe_error)
        state = self._state_for_result(safe_result)
        if state is MemberState.PAUSED:
            self._store.set_member_state(
                team_id=member.team_id,
                member_id=member.member_id,
                state=MemberState.PAUSED,
                resume_token=safe_result.resume_token,
            )
            return ChildExecution(
                member=self._store.member(member.team_id, member.member_id),
                result=safe_result,
            )
        kind = (
            HandoffKind.ERROR
            if safe_result.outcome in {RuntimeOutcome.FAILED, RuntimeOutcome.CANCELLED}
            else HandoffKind.RESULT
        )
        payload = dict(safe_result.output)
        result_handoff = (
            None
            if member.parent_id is None
            else self._result_handoff(member, kind=kind, payload=payload)
        )
        updated = self._store.finish_member_execution(
            team_id=member.team_id,
            member_id=member.member_id,
            state=state,
            resume_token=safe_result.resume_token,
            outcome=safe_result.outcome.value,
            payload=payload,
            error=safe_error,
            result_handoff=result_handoff,
        )
        return ChildExecution(member=updated, result=safe_result)

    @staticmethod
    def _safe_runtime_error(result: RuntimeResult) -> str | None:
        if result.outcome is not RuntimeOutcome.FAILED:
            return None
        if result.error_code is not None:
            try:
                return RuntimeErrorCode(result.error_code).value
            except ValueError:
                return "RuntimeFailure"
        return "RuntimeFailure"

    def _initial_resume_token(
        self,
        *,
        team_id: str,
        member_id: str,
        thread_id: str,
    ) -> str | None:
        if RuntimeCapability.DURABLE_RESUME not in self._runtime.capabilities:
            return None
        token_factory = getattr(self._runtime, "initial_resume_token", None)
        if not callable(token_factory):
            raise TypeError(
                "durable runtime must expose initial_resume_token for crash-safe team execution"
            )
        token = token_factory(
            task_id=self._task_id(team_id, member_id),
            thread_id=thread_id,
        )
        if not token:
            raise RuntimeError("durable runtime returned an empty initial resume token")
        return str(token)

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
                    f"child request exceeds activated definition "
                    f"{request.agent_id}:{request.agent_version}: {exc}"
                ) from exc

    @staticmethod
    def _runtime_payload(
        member: TeamMember,
        handoff_payload: dict[str, object],
        *,
        inbound_handoffs: tuple[AgentHandoff, ...] = (),
    ) -> dict[str, object]:
        return {
            "team_id": member.team_id,
            "parent_id": member.parent_id,
            "member_id": member.member_id,
            "agent_id": member.agent_id,
            "agent_version": member.agent_version,
            "tool_grants": [grant.model_dump(mode="json") for grant in member.tool_grants],
            "handoff": handoff_payload,
            "inbound_handoffs": [
                {
                    "handoff_id": handoff.handoff_id,
                    "team_id": handoff.team_id,
                    "sender_id": handoff.sender_id,
                    "recipient_id": handoff.recipient_id,
                    "kind": handoff.kind.value,
                    "correlation_id": handoff.correlation_id,
                    "payload": dict(handoff.payload),
                }
                for handoff in inbound_handoffs
            ],
        }

    @staticmethod
    def _result_handoff(
        member: TeamMember,
        *,
        kind: HandoffKind,
        payload: dict[str, object],
    ) -> AgentHandoff:
        if member.parent_id is None:
            raise ValueError("child result has no parent recipient")
        return AgentHandoff(
            team_id=member.team_id,
            sender_id=member.member_id,
            recipient_id=member.parent_id,
            kind=kind,
            payload=payload,
            correlation_id=f"team:{member.team_id}:{member.parent_id}:{member.member_id}",
        )

    @staticmethod
    def _task_id(team_id: str, member_id: str) -> str:
        return f"team:{team_id}:{member_id}"

    @staticmethod
    def _state_for_result(result: RuntimeResult) -> MemberState:
        return {
            RuntimeOutcome.COMPLETED: MemberState.COMPLETED,
            RuntimeOutcome.WAITING_APPROVAL: MemberState.WAITING_APPROVAL,
            RuntimeOutcome.PAUSED: MemberState.PAUSED,
            RuntimeOutcome.CANCELLED: MemberState.CANCELLED,
            RuntimeOutcome.FAILED: MemberState.FAILED,
        }[result.outcome]
