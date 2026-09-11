from __future__ import annotations

from datetime import UTC, datetime

from nika_core.runtime.retry import (
    RetryPolicy,
    ScriptRetryCondition,
    ScriptRetryDisposition,
    plan_script_retry,
)

NOW = datetime(2026, 9, 9, 8, 0, tzinfo=UTC)


def test_retry_delay_datetime_overflow_fails_closed() -> None:
    policy = RetryPolicy(
        max_retries=1,
        base_delay_seconds=1e308,
        max_delay_seconds=1e308,
    )

    decision = plan_script_retry(
        policy,
        operation_id="provider-retry-overflow",
        condition=ScriptRetryCondition.RECOVERABLE_NETWORK_FAILURE,
        retries_used=0,
        now=NOW,
        replay_safe=True,
    )

    assert decision.disposition == ScriptRetryDisposition.BACKOFF_LIMIT_EXCEEDED
    assert decision.intent is None


def test_retry_after_datetime_overflow_fails_closed() -> None:
    policy = RetryPolicy(
        max_retries=1,
        base_delay_seconds=1.0,
        max_delay_seconds=1e308,
    )

    decision = plan_script_retry(
        policy,
        operation_id="rate-limit-overflow",
        condition=ScriptRetryCondition.EXPLICIT_RATE_LIMIT,
        retries_used=0,
        now=NOW,
        replay_safe=True,
        retry_after_seconds=1e308,
    )

    assert decision.disposition == ScriptRetryDisposition.BACKOFF_LIMIT_EXCEEDED
    assert decision.intent is None
