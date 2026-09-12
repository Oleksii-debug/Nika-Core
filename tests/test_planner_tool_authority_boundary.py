from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

from nika_core.data.sqlite import SQLiteStore
from nika_core.intelligence.brain import DeterministicBrain, DeterministicBrainResult
from nika_core.intelligence.contracts import (
    DeterministicAction,
    DeterministicErrorCode,
    DeterministicGoal,
    DeterministicPlan,
    PlanStep,
    WorldState,
)
from nika_core.intelligence.runtime_effect_journal import RuntimeIdempotencyEffectJournal
from nika_core.kernel.task_queue import TaskQueue
from nika_core.runtime.idempotency import IdempotencyLedger
from nika_core.tools import (
    ToolAuthorization,
    ToolCall,
    ToolEffectGuard,
    ToolExecutor,
    ToolRisk,
    ToolSpec,
    tool_arguments_fingerprint,
)


class _OneActionPlanner:
    """Planner that can propose an action but has no execution authority."""

    def __init__(
        self,
        action: DeterministicAction,
        *,
        after_proposal: Callable[[], None] | None = None,
    ) -> None:
        self._action = action
        self._after_proposal = after_proposal

    def plan(self, *, state, goal, actions):
        del state, goal
        assert self._action in actions
        plan = DeterministicPlan(
            steps=(
                PlanStep(
                    action_id=self._action.action_id,
                    tool_id=self._action.tool_id,
                ),
            )
        )
        if self._after_proposal is not None:
            self._after_proposal()
        return plan


def _run_planner_tool_case(
    tmp_path: Path,
    *,
    permission_allowed: bool,
    revoke_after_proposal: bool = False,
    planner_claims_approved: bool = False,
) -> tuple[DeterministicBrainResult, list[dict[str, object]], list[ToolCall]]:
    store = SQLiteStore(tmp_path / "planner-authority.db")
    store.initialize()
    task_id = TaskQueue(store).create(
        workspace_id="planner-authority",
        agent_id="one-shot-46",
    ).task_id
    ledger = IdempotencyLedger(store)
    permission = {"allowed": permission_allowed}
    handler_calls: list[dict[str, object]] = []
    policy_calls: list[ToolCall] = []

    async def approval_policy(spec: ToolSpec, call: ToolCall):
        policy_calls.append(call)
        if not permission["allowed"]:
            return None
        assert call.task_id is not None
        return ToolAuthorization(
            tool_id=call.tool_id,
            task_id=call.task_id,
            risk=spec.risk,
            arguments_fingerprint=tool_arguments_fingerprint(call.arguments),
            effect_fingerprint=f"effect:{call.call_id}",
            approval_fingerprint=f"approval:{call.call_id}",
        )

    async def handler(arguments: dict[str, object]) -> object:
        handler_calls.append(dict(arguments))
        return {"published": True}

    tools = ToolExecutor(
        approval_policy=approval_policy,
        effect_guard=ToolEffectGuard(ledger),
    )
    tools.register(
        ToolSpec(
            tool_id="publish.result",
            description="publish a result",
            risk=ToolRisk.HIGH_IMPACT,
        ),
        handler,
    )
    action = DeterministicAction(
        action_id="publish-result",
        adds=frozenset({"published"}),
        tool_id="publish.result",
        arguments={"target": "fixture"},
    )

    def revoke() -> None:
        permission["allowed"] = False

    planner = _OneActionPlanner(
        action,
        after_proposal=revoke if revoke_after_proposal else None,
    )
    brain = DeterministicBrain(
        planner=planner,
        tools=tools,
        effect_journal=RuntimeIdempotencyEffectJournal(ledger),
    )
    approved_action_ids = (
        frozenset({action.action_id}) if planner_claims_approved else frozenset()
    )
    result = asyncio.run(
        brain.run(
            run_id="planner-authority-boundary",
            state=WorldState(),
            goal=DeterministicGoal(required=frozenset({"published"})),
            actions=(action,),
            approved_action_ids=approved_action_ids,
            task_id=task_id,
        )
    )
    return result, handler_calls, policy_calls


def test_denied_tool_stays_denied_even_when_planner_claims_approval(tmp_path: Path) -> None:
    result, handler_calls, policy_calls = _run_planner_tool_case(
        tmp_path,
        permission_allowed=False,
        planner_claims_approved=True,
    )

    assert result.error_code == DeterministicErrorCode.TOOL_EXECUTION_FAILED
    assert result.error == "approval required"
    assert result.completed_actions == ()
    assert result.final_state == WorldState()
    assert handler_calls == []
    assert len(policy_calls) == 1
    assert policy_calls[0].approved is False


def test_allowed_tool_runs_only_after_executor_grants_exact_authority(tmp_path: Path) -> None:
    result, handler_calls, policy_calls = _run_planner_tool_case(
        tmp_path,
        permission_allowed=True,
    )

    assert result.ok
    assert result.error_code is None
    assert result.completed_actions == ("publish-result",)
    assert result.final_state == WorldState(frozenset({"published"}))
    assert handler_calls == [{"target": "fixture"}]
    assert len(policy_calls) == 1
    assert policy_calls[0].approved is False


def test_permission_revoked_after_planning_is_rechecked_before_effect(tmp_path: Path) -> None:
    result, handler_calls, policy_calls = _run_planner_tool_case(
        tmp_path,
        permission_allowed=True,
        revoke_after_proposal=True,
        planner_claims_approved=True,
    )

    assert result.error_code == DeterministicErrorCode.TOOL_EXECUTION_FAILED
    assert result.error == "approval required"
    assert result.completed_actions == ()
    assert result.final_state == WorldState()
    assert handler_calls == []
    assert len(policy_calls) == 1
    assert policy_calls[0].approved is False
