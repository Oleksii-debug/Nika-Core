from __future__ import annotations

import hashlib
import json
from dataclasses import replace

from nika_core.intelligence.contracts import (
    DeterministicAction,
    DeterministicGoal,
    DeterministicPlan,
    DeterministicPlanProvenance,
    WorldState,
)

_STATE_VERSION = "world-state/v1"


class DeterministicPlanProvenanceMismatchError(ValueError):
    """Raised when durable provenance does not describe the exact plan context."""


def seal_plan_provenance(
    plan: DeterministicPlan,
    *,
    state: WorldState,
    goal: DeterministicGoal,
    actions: tuple[DeterministicAction, ...],
    planner: object,
    planner_invoked: bool = True,
) -> DeterministicPlan:
    """Replace any caller/planner evidence with Nika-owned evidence for this exact plan."""

    provenance = _build_provenance(
        plan=plan,
        state=state,
        goal=goal,
        actions=actions,
        planner=planner,
        planner_invoked=planner_invoked,
    )
    return replace(plan, provenance=provenance)


def verify_plan_provenance(
    plan: DeterministicPlan,
    *,
    state: WorldState,
    goal: DeterministicGoal,
    actions: tuple[DeterministicAction, ...],
    planner: object,
    planner_invoked: bool = True,
) -> None:
    """Fail closed if restored/audit provenance is stale for the supplied plan context."""

    if plan.provenance is None:
        raise DeterministicPlanProvenanceMismatchError(
            "deterministic plan provenance is missing"
        )
    expected = _build_provenance(
        plan=replace(plan, provenance=None),
        state=state,
        goal=goal,
        actions=actions,
        planner=planner,
        planner_invoked=planner_invoked,
    )
    if plan.provenance != expected:
        raise DeterministicPlanProvenanceMismatchError(
            "deterministic plan provenance does not match the exact plan context"
        )


def _build_provenance(
    *,
    plan: DeterministicPlan,
    state: WorldState,
    goal: DeterministicGoal,
    actions: tuple[DeterministicAction, ...],
    planner: object,
    planner_invoked: bool,
) -> DeterministicPlanProvenance:
    action_map = {action.action_id: action for action in actions}
    selected_rules: list[dict[str, object]] = []
    for step in plan.steps:
        action = action_map.get(step.action_id)
        if action is None:
            selected_rules.append(
                {
                    "action_id": step.action_id,
                    "tool_id": step.tool_id,
                    "registered": False,
                }
            )
            continue
        selected_rules.append(
            {
                "action_id": action.action_id,
                "requires": sorted(action.requires),
                "forbids": sorted(action.forbids),
                "adds": sorted(action.adds),
                "removes": sorted(action.removes),
                "tool_id": action.tool_id,
                # Bind execution semantics without persisting raw argument names or values.
                # DeterministicAction owns an immutable defensive snapshot, so this digest and
                # ToolCall's later dict(action.arguments) consume the same frozen identity.
                "arguments_fingerprint": _fingerprint(dict(action.arguments)),
                "registered": True,
            }
        )

    planner_id, planner_version, planner_strategy = _planner_descriptor(planner)
    return DeterministicPlanProvenance(
        schema_version=1,
        goal_fingerprint=_fingerprint(
            {
                "required": sorted(goal.required),
                "forbidden": sorted(goal.forbidden),
            }
        ),
        state_fingerprint=_fingerprint({"facts": sorted(state.facts)}),
        state_version=_STATE_VERSION,
        selected_rules_fingerprint=_fingerprint(selected_rules),
        selected_rule_count=len(selected_rules),
        planner_id=planner_id,
        planner_version=planner_version,
        planner_strategy=planner_strategy,
        steps_fingerprint=_fingerprint(
            [
                {"action_id": step.action_id, "tool_id": step.tool_id}
                for step in plan.steps
            ]
        ),
        step_count=len(plan.steps),
        planner_invoked=planner_invoked,
    )


def _planner_descriptor(planner: object) -> tuple[str, str, str]:
    planner_type = type(planner)
    fallback_id = f"{planner_type.__module__}.{planner_type.__qualname__}"
    return (
        _safe_descriptor(getattr(planner, "planner_id", None), fallback_id),
        _safe_descriptor(getattr(planner, "planner_version", None), "unversioned"),
        _safe_descriptor(getattr(planner, "planner_strategy", None), "deterministic-plan"),
    )


def _safe_descriptor(value: object, fallback: str) -> str:
    if isinstance(value, str):
        normalized = value.strip()
        if normalized and len(normalized) <= 256 and not any(
            ord(char) < 32 for char in normalized
        ):
            return normalized
    return fallback


def _fingerprint(payload: object) -> str:
    encoded = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "DeterministicPlanProvenanceMismatchError",
    "seal_plan_provenance",
    "verify_plan_provenance",
]
