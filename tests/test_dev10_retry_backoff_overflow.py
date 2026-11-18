from __future__ import annotations

from datetime import UTC, datetime, timedelta

from nika_core.runtime.retry import (
    RetryPolicy,
    ScriptRetryCondition,
    ScriptRetryDisposition,
    plan_script_retry,
)

NOW = datetime(2026, 9, 9, 5, 0, tzinfo=UTC)


def test_large_durable_attempt_history_clamps_backoff_without_overflow() -> None:
    policy = RetryPolicy(
        max_retries=1_000_000,
        base_delay_seconds=1,
        max_delay_seconds=30,
    )

    assert policy.delay_seconds(retry_number=1_000_000) == 30

    decision = plan_script_retry(
        policy,
        operation_id="restart-large-attempt-history",
        condition=ScriptRetryCondition.RECOVERABLE_NETWORK_FAILURE,
        retries_used=999_999,
        now=NOW,
        replay_safe=True,
    )

    assert decision.disposition == ScriptRetryDisposition.SCHEDULED
    assert decision.intent is not None
    assert decision.intent.retry_number == 1_000_000
    assert decision.intent.not_before_utc == NOW + timedelta(seconds=30)
