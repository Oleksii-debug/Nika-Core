from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from collections.abc import Mapping
from pathlib import Path

from nika_core.intelligence.brain import DeterministicBrain, DeterministicBrainResult
from nika_core.intelligence.contracts import (
    DeterministicAction,
    DeterministicEffectReservation,
    DeterministicGoal,
    WorldState,
)
from nika_core.intelligence.unified_planning_adapter import UnifiedPlanningAdapter
from nika_core.tools import ToolExecutor, ToolRisk, ToolSpec

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNNER_VERSION = "1.0.0"
DEFAULT_DATASET = REPO_ROOT / "evals" / "deterministic_brain" / "v1.json"


class _EmptyEffectJournal:
    """No-op durable-state oracle used only to reach ToolExecutor policy authority."""

    def unresolved_operation_keys(self, *, task_id: str) -> tuple[str, ...]:
        del task_id
        return ()

    def reserve(
        self,
        *,
        task_id: str,
        action: DeterministicAction,
    ) -> DeterministicEffectReservation:
        del task_id, action
        raise AssertionError("external/high-impact effects must remain ToolExecutor-owned")

    def complete(self, operation_key: str) -> None:
        del operation_key
        raise AssertionError("unexpected deterministic effect-journal completion")

    def mark_uncertain(self, operation_key: str) -> None:
        del operation_key
        raise AssertionError("unexpected deterministic effect-journal uncertainty")

    def release_pending(self, operation_key: str) -> None:
        del operation_key
        raise AssertionError("unexpected deterministic effect-journal release")


def _as_string_list(value: object, *, field: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{field} must be a list of strings")
    return value


def _goal(raw: Mapping[str, object]) -> DeterministicGoal:
    return DeterministicGoal(
        required=frozenset(_as_string_list(raw.get("required", []), field="goal.required")),
        forbidden=frozenset(_as_string_list(raw.get("forbidden", []), field="goal.forbidden")),
    )


def _actions(raw_actions: object) -> tuple[DeterministicAction, ...]:
    if not isinstance(raw_actions, list):
        raise ValueError("actions must be a list")
    actions: list[DeterministicAction] = []
    for index, raw in enumerate(raw_actions):
        if not isinstance(raw, dict):
            raise ValueError(f"actions[{index}] must be an object")
        action_id = raw.get("action_id")
        if not isinstance(action_id, str):
            raise ValueError(f"actions[{index}].action_id must be a string")
        tool_id = raw.get("tool_id")
        if tool_id is not None and not isinstance(tool_id, str):
            raise ValueError(f"actions[{index}].tool_id must be a string or null")
        arguments = raw.get("arguments", {})
        if not isinstance(arguments, dict):
            raise ValueError(f"actions[{index}].arguments must be an object")
        actions.append(
            DeterministicAction(
                action_id=action_id,
                requires=frozenset(
                    _as_string_list(raw.get("requires", []), field=f"actions[{index}].requires")
                ),
                forbids=frozenset(
                    _as_string_list(raw.get("forbids", []), field=f"actions[{index}].forbids")
                ),
                adds=frozenset(
                    _as_string_list(raw.get("adds", []), field=f"actions[{index}].adds")
                ),
                removes=frozenset(
                    _as_string_list(raw.get("removes", []), field=f"actions[{index}].removes")
                ),
                tool_id=tool_id,
                arguments=dict(arguments),
            )
        )
    return tuple(actions)


def _build_tools(
    raw_tools: object,
    calls: dict[str, int],
) -> ToolExecutor:
    if raw_tools is None:
        raw_tools = []
    if not isinstance(raw_tools, list):
        raise ValueError("tools must be a list")
    executor = ToolExecutor()
    for index, raw in enumerate(raw_tools):
        if not isinstance(raw, dict):
            raise ValueError(f"tools[{index}] must be an object")
        tool_id = raw.get("tool_id")
        risk_name = raw.get("risk", ToolRisk.READ_ONLY.value)
        behavior = raw.get("behavior", "success")
        if not isinstance(tool_id, str):
            raise ValueError(f"tools[{index}].tool_id must be a string")
        if not isinstance(risk_name, str):
            raise ValueError(f"tools[{index}].risk must be a string")
        if behavior not in {"success", "fail_once"}:
            raise ValueError(f"tools[{index}].behavior is unsupported: {behavior!r}")
        try:
            risk = ToolRisk(risk_name)
        except ValueError as exc:
            raise ValueError(f"tools[{index}].risk is unsupported: {risk_name!r}") from exc
        calls.setdefault(tool_id, 0)

        async def handler(
            _arguments: dict[str, object],
            *,
            bound_tool_id: str = tool_id,
            bound_behavior: object = behavior,
        ) -> object:
            calls[bound_tool_id] += 1
            if bound_behavior == "fail_once" and calls[bound_tool_id] == 1:
                raise RuntimeError("synthetic deterministic evaluation failure")
            return {"tool_id": bound_tool_id, "call": calls[bound_tool_id]}

        executor.register(
            ToolSpec(tool_id=tool_id, description=f"evaluation tool {tool_id}", risk=risk),
            handler,
        )
    return executor


def _result_record(result: DeterministicBrainResult) -> dict[str, object]:
    return {
        "ok": result.ok,
        "completed_actions": list(result.completed_actions),
        "final_facts": sorted(result.final_state.facts),
        "error_code": result.error_code.value if result.error_code is not None else None,
        "error": result.error,
        "plan_steps": [step.action_id for step in result.plan.steps],
        "planning_history": [
            [step.action_id for step in plan.steps] for plan in result.planning_history
        ],
        "replans": result.replans,
    }


def _fingerprint(value: object) -> str:
    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _count_leaves(value: object) -> int:
    if isinstance(value, dict):
        return sum(_count_leaves(item) for item in value.values())
    if isinstance(value, list):
        return 1
    return 1


def _matches(expected: object, actual: object) -> tuple[bool, int, int]:
    """Compare expected as an exact recursive subset of actual."""
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            leaves = _count_leaves(expected)
            return False, leaves, 0
        total = 0
        passed = 0
        ok = True
        for key, expected_value in expected.items():
            if key not in actual:
                leaves = _count_leaves(expected_value)
                total += leaves
                ok = False
                continue
            child_ok, child_total, child_passed = _matches(expected_value, actual[key])
            ok = ok and child_ok
            total += child_total
            passed += child_passed
        return ok, total, passed
    matched = expected == actual
    return matched, 1, int(matched)


def _case_inputs(
    case: Mapping[str, object],
) -> tuple[WorldState, DeterministicGoal, tuple[DeterministicAction, ...]]:
    raw_goal = case.get("goal")
    if not isinstance(raw_goal, dict):
        raise ValueError("goal must be an object")
    state = WorldState(frozenset(_as_string_list(case.get("state", []), field="state")))
    return state, _goal(raw_goal), _actions(case.get("actions"))


async def _single_run(case: Mapping[str, object]) -> dict[str, object]:
    state, goal, actions = _case_inputs(case)
    calls: dict[str, int] = {}
    tools = _build_tools(case.get("tools"), calls)
    journal = _EmptyEffectJournal() if case.get("effect_journal") == "empty" else None
    task_id = case.get("task_id")
    if task_id is not None and not isinstance(task_id, str):
        raise ValueError("task_id must be a string")
    case_id = case.get("id")
    if not isinstance(case_id, str):
        raise ValueError("case id must be a string")
    result = await DeterministicBrain(
        planner=UnifiedPlanningAdapter(),
        tools=tools,
        effect_journal=journal,
    ).run(
        run_id=f"eval:{case_id}",
        task_id=task_id,
        state=state,
        goal=goal,
        actions=actions,
    )
    return {"result": _result_record(result), "handler_calls": dict(sorted(calls.items()))}


async def _restart_replay(case: Mapping[str, object]) -> dict[str, object]:
    state, goal, actions = _case_inputs(case)
    calls: dict[str, int] = {}
    case_id = case.get("id")
    if not isinstance(case_id, str):
        raise ValueError("case id must be a string")

    first = await DeterministicBrain(
        planner=UnifiedPlanningAdapter(),
        tools=_build_tools(case.get("tools"), calls),
    ).run(
        run_id=f"eval:{case_id}:before-restart",
        state=state,
        goal=goal,
        actions=actions,
    )
    second = await DeterministicBrain(
        planner=UnifiedPlanningAdapter(),
        tools=_build_tools(case.get("tools"), calls),
    ).run(
        run_id=f"eval:{case_id}:after-restart",
        state=first.final_state,
        goal=goal,
        actions=actions,
        previously_completed_action_ids=first.completed_actions,
    )
    return {
        "first_result": _result_record(first),
        "second_result": _result_record(second),
        "handler_calls": dict(sorted(calls.items())),
    }


async def _repeatability(case: Mapping[str, object]) -> dict[str, object]:
    state, goal, actions = _case_inputs(case)
    runs = case.get("runs")
    if not isinstance(runs, int) or isinstance(runs, bool) or runs < 2:
        raise ValueError("repeatability runs must be an integer >= 2")
    records: list[dict[str, object]] = []
    for index in range(runs):
        result = await DeterministicBrain(
            planner=UnifiedPlanningAdapter(),
            tools=ToolExecutor(),
        ).run(
            run_id=f"eval:repeatability:{index}",
            state=state,
            goal=goal,
            actions=actions,
        )
        records.append(_result_record(result))
    fingerprints = [_fingerprint(record) for record in records]
    baseline = records[0]
    mismatches = sum(record != baseline for record in records[1:])
    return {
        "result": baseline,
        "runs": runs,
        "unique_result_fingerprints": len(set(fingerprints)),
        "mismatches": mismatches,
        "result_fingerprint": fingerprints[0],
    }


async def _evaluate_case(case: Mapping[str, object]) -> dict[str, object]:
    mode = case.get("mode")
    if mode == "single_run":
        return await _single_run(case)
    if mode == "restart_replay":
        return await _restart_replay(case)
    if mode == "repeatability":
        return await _repeatability(case)
    raise ValueError(f"unsupported evaluation mode: {mode!r}")


async def evaluate(dataset_path: Path) -> dict[str, object]:
    raw_bytes = dataset_path.read_bytes()
    dataset = json.loads(raw_bytes)
    if not isinstance(dataset, dict):
        raise ValueError("dataset root must be an object")
    if dataset.get("schema_version") != 1:
        raise ValueError("unsupported dataset schema_version")
    dataset_id = dataset.get("dataset_id")
    dataset_version = dataset.get("dataset_version")
    if not isinstance(dataset_id, str) or not dataset_id:
        raise ValueError("dataset_id must be a non-empty string")
    if not isinstance(dataset_version, str) or not dataset_version:
        raise ValueError("dataset_version must be a non-empty string")
    raw_cases = dataset.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ValueError("dataset cases must be a non-empty list")

    case_results: list[dict[str, object]] = []
    contract_checks_total = 0
    contract_checks_passed = 0
    seen_ids: set[str] = set()
    repeatability_runs = 0
    repeatability_mismatches = 0
    policy_handler_calls = 0

    for raw_case in raw_cases:
        if not isinstance(raw_case, dict):
            raise ValueError("each evaluation case must be an object")
        case_id = raw_case.get("id")
        category = raw_case.get("category")
        expected = raw_case.get("expected")
        if not isinstance(case_id, str) or not case_id:
            raise ValueError("case id must be a non-empty string")
        if case_id in seen_ids:
            raise ValueError(f"duplicate evaluation case id: {case_id}")
        seen_ids.add(case_id)
        if not isinstance(category, str) or not category:
            raise ValueError(f"case {case_id} category must be a non-empty string")
        if not isinstance(expected, dict):
            raise ValueError(f"case {case_id} expected must be an object")

        observed = await _evaluate_case(raw_case)
        passed, check_total, check_passed = _matches(expected, observed)
        contract_checks_total += check_total
        contract_checks_passed += check_passed
        if category == "repeatability":
            repeatability_runs += int(observed["runs"])
            repeatability_mismatches += int(observed["mismatches"])
        if category == "policy_denial":
            handler_calls = observed.get("handler_calls", {})
            if isinstance(handler_calls, dict):
                policy_handler_calls += sum(int(value) for value in handler_calls.values())

        case_results.append(
            {
                "id": case_id,
                "category": category,
                "status": "pass" if passed else "fail",
                "contract_checks_total": check_total,
                "contract_checks_passed": check_passed,
                "observed": observed,
                "observed_fingerprint": _fingerprint(observed),
            }
        )

    cases_passed = sum(case["status"] == "pass" for case in case_results)
    evidence_core = {
        "dataset_id": dataset_id,
        "dataset_version": dataset_version,
        "runner_version": RUNNER_VERSION,
        "cases": [
            {"id": case["id"], "observed_fingerprint": case["observed_fingerprint"]}
            for case in case_results
        ],
    }
    return {
        "schema_version": 1,
        "dataset": {
            "id": dataset_id,
            "version": dataset_version,
            "sha256": hashlib.sha256(raw_bytes).hexdigest(),
        },
        "runner": {"version": RUNNER_VERSION},
        "metrics": {
            "cases_total": len(case_results),
            "cases_passed": cases_passed,
            "cases_failed": len(case_results) - cases_passed,
            "contract_checks_total": contract_checks_total,
            "contract_checks_passed": contract_checks_passed,
            "repeatability_runs": repeatability_runs,
            "repeatability_mismatches": repeatability_mismatches,
            "policy_denied_handler_calls": policy_handler_calls,
            "all_pass": cases_passed == len(case_results),
        },
        "cases": case_results,
        "evidence_fingerprint": _fingerprint(evidence_core),
        "strategy_promotion": {"attempted": False, "performed": False},
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the versioned Deterministic Brain baseline")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    payload = asyncio.run(evaluate(args.dataset))
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if args.output is None:
        print(encoded, end="")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    return 0 if bool(payload["metrics"]["all_pass"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
