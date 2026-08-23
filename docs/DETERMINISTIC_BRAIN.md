# Deterministic Brain

The Deterministic Brain is Nika Core's model-free execution path for structured domains. It does
not call ModelGateway, Ollama, Foundry Local, or a cloud model. Nika owns the framework-neutral
world-state, goal, action, plan, error, validation, approval-boundary, and replay semantics.
Unified Planning remains an optional replaceable planning engine behind those contracts.

## Reuse decision

- **REUSE:** Unified Planning 1.3.x plus the `up-aries==0.5.0` engine adapter through the
  `planning` optional dependency. Unified Planning is Apache-2.0. The exact adopted
  `up-aries==0.5.0` plugin and Aries v0.5.0 upstream source tree are MIT-licensed.
- **REUSE:** Nika's integrated runtime `IdempotencyLedger` and startup recovery classification for
  durable non-read-only tool-effect intent, completion and reconciliation state.
- **ADAPT:** `UnifiedPlanningAdapter` maps Nika boolean facts/actions/goals to Unified Planning
  objects and normalizes planner result status back to Nika error semantics.
- **ADAPT:** `RuntimeIdempotencyEffectJournal` maps deterministic action identity onto the existing
  runtime ledger without creating a second database, retry ledger or recovery framework.
- **CUSTOM (thin):** Nika validates returned action identities/effects, bounds execution and
  re-planning, owns stable effect identity/fingerprints, and delegates every tool call to the
  normal `ToolExecutor` permission/approval boundary.

Pyperplan is not the Nika default dependency. Fresh adoption review found that although the
`up-pyperplan` wrapper is Apache-2.0, the Pyperplan 2.1 engine itself is GPLv3+. Nika therefore
does not silently treat that transitive engine as Apache-licensed. Other Unified Planning engine
wrappers remain candidates only after their underlying binary/runtime license and Windows fit are
verified; wrapper metadata alone is not sufficient distribution provenance.

Aries is selected as the current first proof engine because it is a maintained official Unified
Planning integration with permissive source/plugin licensing and packaged Windows/Linux support.
It is not claimed to prove every unsatisfiable action-planning problem. Before engine invocation,
Nika performs a sound thin reachability precheck for the obvious cases its own explicit contract
can prove: a missing required fact that no registered action can add, or a present forbidden fact
that no registered action can remove. Those goals fail immediately as `GOAL_UNREACHABLE` without
starting a planner process. Harder unsatisfiable cases remain the planner's responsibility, so
`GOAL_UNREACHABLE` stays distinct from `NO_PLAN_FOUND`.

No Unified Planning problem, fluent, action, plan, result, Aries, gRPC, SQLite or runtime-ledger
type is exposed by the Nika deterministic planning contracts.

## Execution contract

A deterministic run receives:

- a `WorldState` containing explicit facts;
- a `DeterministicGoal` with required and forbidden facts;
- uniquely identified `DeterministicAction` definitions with explicit preconditions/effects and
  optional registered Nika tool calls;
- caller budgets for maximum executed steps, maximum re-plans, and total planning wall time;
- optionally, a `WorldStateObserver` for authoritative state drift detection;
- optionally, ordered `previously_completed_action_ids` recovered from a durable checkpoint;
- for durable non-read-only tool execution, a `DeterministicEffectJournal` plus stable `task_id`
  and `execution_id` supplied by the owning runtime/task lifecycle.

Before the first tool action in each returned plan, Nika simulates the entire plan against the
current state. Unknown action IDs, planner/tool identity mismatch, repeated completed actions,
false preconditions, deterministic no-op effects, oversized plans, or plans that do not reach the
declared goal fail before side effects execute.

A changed observed state invalidates the current plan and causes a bounded fresh plan from the
new state. Completed action IDs are removed from the next planner action set. Nika therefore does
not silently replay an already completed tool effect after re-plan or normal returned-result
restart. If external state loss makes a completed non-repeatable action necessary again, planning
fails closed rather than deliberately replaying that side effect.

`planning_timeout_seconds` is a total caller-visible planning budget across the initial plan and
all re-plans. The asyncio caller boundary remains authoritative: a timed-out worker thread is not
described as a hard-killed native planner process. A particular Unified Planning engine may
support an internal solve timeout, but Nika does not claim hard native/process cancellation from
that API without engine-specific executable evidence.

## Durable tool-effect boundary

A returned `completed_actions` list alone cannot close the process-loss window after a tool has
changed external/local state but before the brain returns to its caller. Deterministic Brain now
uses the existing runtime idempotency/recovery mechanism for that boundary instead of inventing
another checkpoint store.

For a registered tool whose `ToolRisk` is not `READ_ONLY`:

1. execution fails closed with `SIDE_EFFECT_JOURNAL_REQUIRED` if no durable effect journal exists;
2. the journal commits a `PENDING` reservation **before** `ToolExecutor` may invoke the handler;
3. the operation key is stable for `(task_id, execution_id, action_id)` and the ledger input
   fingerprint binds tool ID, arguments, preconditions and deterministic effects;
4. a planner-selected action still enters `ToolExecutor` with `approved=False`;
5. approval denial or another proven-before-handler rejection releases the unused reservation;
6. a normal successful handler call is durably changed to `COMPLETED` before deterministic state
   is advanced in memory;
7. timeout, cancellation or an adapter failure with uncertain external outcome becomes
   `UNCERTAIN`; abrupt process loss can leave the already-durable reservation `PENDING`;
8. an existing `PENDING` or `UNCERTAIN` reservation blocks the action with
   `SIDE_EFFECT_RECONCILIATION_REQUIRED` and the handler is not replayed;
9. an existing matching `COMPLETED` reservation lets restart reconstruct the declared
   deterministic action effects without calling the handler again;
10. changing the tool/arguments/effect semantics while reusing the same durable execution/action
    identity fails closed as `SIDE_EFFECT_IDENTITY_CONFLICT`.

The existing `RuntimeRecoveryService` already classifies any task with `PENDING` or `UNCERTAIN`
idempotency records as `RECONCILE_SIDE_EFFECTS`, so startup auto-resume cannot cross an unresolved
deterministic tool effect. Reconciliation remains an external-system/operator concern using the
existing ledger contract: if completion can be proved, reconcile it as completed; if a pending
operation can be proved not to have happened, release it explicitly under the normal runtime
policy.

This is deliberately **not** an exactly-once claim. Nika guarantees durable pre-effect identity
and fail-closed no-replay under uncertainty. The external system still needs its own idempotency
or inspection/reconciliation capability when it can apply an effect and lose the response.

Read-only tool calls do not require the effect journal because they are not declared mutation
boundaries. Misclassifying an effectful tool as `READ_ONLY` is a Tool Registry/policy defect and is
not legitimized by the planner.

## Approval boundary

A planner selects an action; it does not authorize it. `approved_action_ids` is retained only for
source compatibility and has no authority to set `ToolCall.approved=True`. Deterministic Brain
tool calls always enter `ToolExecutor` without planner-manufactured approval and therefore use
the same approval policy/security evidence path as non-planner tool execution. High-impact or
external-side-effect tools remain denied unless that normal boundary authorizes the exact call.

For durable non-read-only calls the `ToolCall.call_id` is the stable runtime-ledger operation key,
which correlates audit evidence with the same effect identity used for crash recovery. It grants
no approval by itself.

## Error semantics

Planner failures carry `DeterministicErrorCode`, including dependency unavailable, proven
unreachable goal, no plan found by an incomplete planner, planning timeout, planner resource
limit, unsupported problem, and planner failure. Execution results additionally classify plan
length, invalid-plan, re-plan-limit, unavailable-action, ordinary tool-execution failure, missing
side-effect journal, durable effect-identity conflict, reconciliation-required effect state,
durable-record failure, and goal-unsatisfied failures.

The message remains human-readable while `error_code` is stable for programmatic handling.

## Restart evidence

For read-only/purely deterministic work, a caller persists the returned `final_state` plus ordered
`completed_actions` in its normal durable task/checkpoint state. On restart those values are
passed back as the initial state and `previously_completed_action_ids`. The brain excludes those
identities before planning and returns the cumulative completed sequence so another checkpoint
can be written without relying on chat/model memory.

For non-read-only tool actions, the effect journal is the additional crash-window authority. A
completed durable effect can reconstruct its declared state transition even when process loss
occurred before the enclosing brain result/checkpoint was returned. Pending or uncertain effects
must be reconciled before continuation.

No second persistence engine is introduced; authoritative state remains in Nika's existing
runtime/task SQLite and idempotency/recovery layers.
