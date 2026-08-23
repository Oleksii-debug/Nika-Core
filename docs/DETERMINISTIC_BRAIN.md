# Deterministic Brain

The Deterministic Brain is Nika Core's model-free execution path for structured domains. It does
not call ModelGateway, Ollama, Foundry Local, or a cloud model. Nika owns the framework-neutral
world-state, goal, action, plan, error, validation, approval-boundary, and replay semantics.
Unified Planning remains an optional replaceable planning engine behind those contracts.

## Reuse decision

- **REUSE:** Unified Planning 1.3.x and the installed Pyperplan engine through the existing
  `planning` optional dependency.
- **ADAPT:** `UnifiedPlanningAdapter` maps Nika boolean facts/actions/goals to Unified Planning
  objects and normalizes planner result status back to Nika error semantics.
- **CUSTOM (thin):** Nika validates returned action identities/effects, bounds execution and
  re-planning, preserves completed-effect identity across restart, and delegates every tool call
  to the normal `ToolExecutor` permission/approval boundary.

No Unified Planning problem, fluent, action, plan, or result type is exposed by the Nika
deterministic planning contracts.

## Execution contract

A deterministic run receives:

- a `WorldState` containing explicit facts;
- a `DeterministicGoal` with required and forbidden facts;
- uniquely identified `DeterministicAction` definitions with explicit preconditions/effects and
  optional registered Nika tool calls;
- caller budgets for maximum executed steps, maximum re-plans, and total planning wall time;
- optionally, a `WorldStateObserver` for authoritative state drift detection;
- optionally, ordered `previously_completed_action_ids` recovered from a durable checkpoint.

Before the first tool action in each returned plan, Nika simulates the entire plan against the
current state. Unknown action IDs, planner/tool identity mismatch, repeated completed actions,
false preconditions, deterministic no-op effects, oversized plans, or plans that do not reach the
declared goal fail before side effects execute.

A changed observed state invalidates the current plan and causes a bounded fresh plan from the
new state. Completed action IDs are removed from the next planner action set. Nika therefore does
not silently replay an already completed tool effect after re-plan or restart. If external state
loss makes a completed non-repeatable action necessary again, planning fails closed rather than
replaying that side effect.

`planning_timeout_seconds` is a total caller-visible planning budget across the initial plan and
all re-plans. The asyncio caller boundary remains authoritative: a timed-out worker thread is not
described as a hard-killed native planner process. Unified Planning exposes a per-solve timeout,
but Nika does not claim hard native cancellation from that API without engine-specific evidence.

## Approval boundary

A planner selects an action; it does not authorize it. `approved_action_ids` is retained only for
source compatibility and has no authority to set `ToolCall.approved=True`. Deterministic Brain
tool calls always enter `ToolExecutor` without planner-manufactured approval and therefore use
the same approval policy/security evidence path as non-planner tool execution. High-impact or
external-side-effect tools remain denied unless that normal boundary authorizes the exact call.

## Error semantics

Planner failures carry `DeterministicErrorCode`, including dependency unavailable, proven
unreachable goal, no plan found by an incomplete planner, planning timeout, planner resource
limit, unsupported problem, and planner failure. Execution results additionally classify plan
length, invalid-plan, re-plan-limit, unavailable-action, tool-execution, and goal-unsatisfied
failures.

The message remains human-readable while `error_code` is stable for programmatic handling.

## Restart evidence

A caller persists the returned `final_state` plus ordered `completed_actions` in its normal
durable task/checkpoint state. On restart those values are passed back as the initial state and
`previously_completed_action_ids`. The brain excludes those identities before planning and
returns the cumulative completed sequence so another checkpoint can be written without relying
on chat/model memory.

This contract does not invent a second persistence engine; durable storage remains the
responsibility of Nika's existing runtime/task checkpoint layer.
