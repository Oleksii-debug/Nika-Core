from __future__ import annotations

import pytest

from nika_core.runtime.contracts import RuntimeErrorCode, RuntimeOutcome, RuntimeResult
from nika_core.runtime.retry import RetryPolicy


def test_retry_policy_rejects_truthy_non_boolean_fresh_retry_authority() -> None:
    with pytest.raises(TypeError, match="allow_fresh_retry"):
        RetryPolicy(max_retries=1, allow_fresh_retry=1)  # type: ignore[arg-type]


def test_retry_policy_rejects_raw_string_error_code_authority() -> None:
    with pytest.raises(TypeError, match="RuntimeErrorCode"):
        RetryPolicy(
            max_retries=1,
            retryable_error_codes=frozenset({"transient"}),  # type: ignore[arg-type]
        )


def test_retry_policy_rejects_mutable_error_code_authority() -> None:
    with pytest.raises(TypeError, match="frozenset"):
        RetryPolicy(
            max_retries=1,
            retryable_error_codes={RuntimeErrorCode.TRANSIENT},  # type: ignore[arg-type]
        )


def test_retry_policy_returns_real_boolean_for_fresh_retry_decision() -> None:
    policy = RetryPolicy(
        max_retries=1,
        retryable_error_codes=frozenset({RuntimeErrorCode.TRANSIENT}),
        allow_fresh_retry=True,
    )
    result = RuntimeResult(
        outcome=RuntimeOutcome.FAILED,
        error="temporary provider failure",
        error_code=RuntimeErrorCode.TRANSIENT,
    )

    decision = policy.should_retry(result, retries_used=0)

    assert decision is True
    assert type(decision) is bool
