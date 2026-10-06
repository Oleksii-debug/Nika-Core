# M2 runtime retry and deadline semantics

Decision date: 2026-08-18.
Status: IMPLEMENTED/PREPARED on the dependent M2 branch; executable CI evidence is still required before milestone credit.

## Goal
Every agent invocation is bounded by step count and may also be bounded by wall-clock time. Temporary failures may be retried only when the retry can be made without silently replaying already-completed side effects.

## Deadline contract
`RuntimeRequest` and `RuntimeResumeRequest` accept optional `timeout_seconds` in addition to `max_steps`. Non-positive values fail closed before framework execution.

The LangGraph adapter uses Python `asyncio.wait_for()` around the exact active invocation. A deadline produces:
- `RuntimeOutcome.FAILED` rather than pretending the user cancelled the task;
- typed `RuntimeErrorCode.TIMEOUT`;
- the stable LangGraph thread ID as `resume_token` for diagnosis/explicit continuation policy;
- active-task registry cleanup after the timed-out invocation has been cancelled/awaited.

A timeout does **not** claim that the middle of an interrupted node is durably resumable. Safe continuation remains tied to LangGraph checkpoint boundaries.

## Typed errors
Framework-neutral runtime failures now distinguish at least:
- `TIMEOUT` — wall-clock execution bound exceeded;
- `TRANSIENT` — a caller/adapter explicitly classified a temporary failure;
- `INVALID_RESUME` — resume identity mismatch;
- `DUPLICATE_ACTIVE` — a second concurrent invocation tried to use the same task/thread;
- `INTERNAL` — uncategorized runtime/framework exception.

The coordinator never retries an untyped failure merely because it failed.

## Retry policy
`RetryPolicy` is Nika-owned because retry safety depends on Nika task state, side effects, audit and durability semantics rather than on one orchestration framework.

Defaults are fail-closed:
- `max_retries = 0`;
- no error code is retryable unless explicitly listed;
- a failed attempt must provide a durable `resume_token` before automatic retry;
- fresh replay without a resume token is forbidden unless a caller explicitly sets `allow_fresh_retry=True`;
- exponential backoff is deterministic and bounded by `max_delay_seconds`.

When a safe retry is accepted, `TaskRuntimeCoordinator` performs:
`RUNNING -> RETRYING -> RUNNING`, records `runtime.retry_scheduled` and `runtime.retry_started`, then resumes through `RuntimeResumeMode.CONTINUE` using the exact token returned by the failed attempt. The final outcome is processed through the normal task-state/audit path.

## Why timeout is not automatically retryable
The default policy intentionally does not retry `TIMEOUT`. An invocation may have performed an external side effect before a timeout and may still only be safe to continue from a persisted checkpoint. Higher-level tool/provider adapters may later opt into timeout retry only when idempotency and checkpoint evidence make that safe.

## Prepared deterministic tests
`tests/test_runtime_retry_timeout.py` covers:
1. invalid non-positive timeout fails before execution;
2. a slow graph produces typed TIMEOUT, not CANCELLED;
3. an explicitly TRANSIENT durable failure retries by resume token and completes;
4. a transient failure without a resume token is not replayed by default;
5. exponential retry delay is capped.

These tests are committed but are not claimed as passed until GitHub allocates an executable runner and Ruff/compile/pytest actually run.
