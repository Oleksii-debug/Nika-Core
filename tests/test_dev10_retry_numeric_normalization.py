from datetime import UTC, datetime

import pytest

from nika_core.runtime.retry import RetryPolicy, ScriptRetryCondition, plan_script_retry


def test_retry_policy_rejects_integer_too_large_for_float_normalization() -> None:
    with pytest.raises(ValueError, match="finite non-negative number"):
        RetryPolicy(base_delay_seconds=10**400, max_delay_seconds=10**400)


def test_retry_after_rejects_integer_too_large_for_float_normalization() -> None:
    policy = RetryPolicy(max_retries=1, base_delay_seconds=1.0, max_delay_seconds=30.0)

    with pytest.raises(ValueError, match="finite non-negative number"):
        plan_script_retry(
            policy,
            operation_id="dev10-huge-retry-after",
            condition=ScriptRetryCondition.EXPLICIT_RATE_LIMIT,
            retries_used=0,
            now=datetime(2026, 9, 9, 9, 0, tzinfo=UTC),
            replay_safe=True,
            retry_after_seconds=10**400,
        )
