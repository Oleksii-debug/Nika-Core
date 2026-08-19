from __future__ import annotations

import asyncio
from dataclasses import dataclass

from nika_core.intelligence.contracts import (
    DeterministicAction,
    DeterministicGoal,
    DeterministicPlan,
    DeterministicPlanner,
    WorldState,
)
from nika_core.tools import ToolCall, ToolExecutor


@dataclass(frozen=True, slots=True)
class DeterministicBrainResult:
    plan: DeterministicPlan
    completed_actions: tuple[str, ...]
    final_state: WorldState
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


class DeterministicBrain:
    """Plan and execute explicit workflows without any language model."""

    def __init__(self, *, planner: DeterministicPlanner, tools: ToolExecutor) -> None:
        self._planner = planner
        self._tools = tools

    async def run(
        self,
        *,
        run_id: str,
        state: WorldState,
        goal: DeterministicGoal,
        actions: tuple[DeterministicAction, ...],
        approved_action_ids: frozenset[str] = frozenset(),
    ) -> DeterministicBrainResult:
        if not run_id.strip():
            raise ValueError("run_id must not be empty")
        action_map = {action.action_id: action for action in actions}
        if len(action_map) != len(actions):
            raise ValueError("duplicate deterministic action_id")

        plan = await asyncio.to_thread(
            self._planner.plan,
            state=state,
            goal=goal,
            actions=actions,
        )
        facts = set(state.facts)
        completed: list[str] = []

        for index, step in enumerate(plan.steps):
            action = action_map.get(step.action_id)
            if action is None:
                return DeterministicBrainResult(
                    plan=plan,
                    completed_actions=tuple(completed),
                    final_state=WorldState(frozenset(facts)),
                    error=f"planned action is unavailable: {step.action_id}",
                )
            if not action.requires <= facts or action.forbids & facts:
                return DeterministicBrainResult(
                    plan=plan,
                    completed_actions=tuple(completed),
                    final_state=WorldState(frozenset(facts)),
                    error=f"planned action preconditions are no longer true: {action.action_id}",
                )

            if action.tool_id is not None:
                result = await self._tools.execute(
                    ToolCall(
                        call_id=f"{run_id}:{index}:{action.action_id}",
                        tool_id=action.tool_id,
                        arguments=dict(action.arguments),
                        approved=action.action_id in approved_action_ids,
                    )
                )
                if not result.ok:
                    return DeterministicBrainResult(
                        plan=plan,
                        completed_actions=tuple(completed),
                        final_state=WorldState(frozenset(facts)),
                        error=result.error or "tool failed",
                    )

            facts.difference_update(action.removes)
            facts.update(action.adds)
            completed.append(action.action_id)

        final_state = WorldState(frozenset(facts))
        if not goal.required <= final_state.facts or goal.forbidden & final_state.facts:
            return DeterministicBrainResult(
                plan=plan,
                completed_actions=tuple(completed),
                final_state=final_state,
                error="plan completed without satisfying the goal",
            )
        return DeterministicBrainResult(
            plan=plan,
            completed_actions=tuple(completed),
            final_state=final_state,
        )
