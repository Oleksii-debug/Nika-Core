from __future__ import annotations

import datetime as dt

import pytest

from nika_core.runtime.contracts import RuntimeErrorCode, RuntimeOutcome, RuntimeResult
from nika_core.runtime.retry import (
    RetryPolicy,
    ScriptRetryCondition,
    ScriptRetryDisposition,
    ScriptRetryIntent,
    evaluate_script_retry_intent,
    plan_script_retry,
)

NOW = dt.datetime(2026, 9, 8, 13, 0, tzinfo=dt.UTC)


@pytest.mark.parametrize("operation_id", [" retry-op", "retry-op ", "\tretry-op", "retry-op\n"])
def test_script_retry_intent_rejects_noncanonical_operation_id(operation_id: str) -> None:
    """Reject retry authority that downstream durable bindings cannot decode safely."""

    with pytest.raises(ValueError, match="operation_id"):
        ScriptRetryIntent(
            operation_id=operation_id,
            condition=ScriptRetryCondition.RECOVERABLE_NETWORK_FAILURE,
            retry_number=1,
            not_before_utc=NOW + dt.timedelta(seconds=1),
        )


def test_script_retry_intent_keeps_internal_spaces_valid() -> None:
    intent = ScriptRetryIntent(
        operation_id="retry target one",
        condition=ScriptRetryCondition.RECOVERABLE_NETWORK_FAILURE,
        retry_number=1,
        not_before_utc=NOW + dt.timedelta(seconds=1),
    )

    assert intent.operation_id == "retry target one"


def test_retry_policy_rejects_boolean_max_retries() -> None:
    """Malformed boolean config must not silently authorize one retry."""

    with pytest.raises(ValueError, match="max_retries"):
        RetryPolicy(max_retries=True)


@pytest.mark.parametrize("retries_used", [-1, True])
def test_retry_policy_should_retry_rejects_malformed_attempt_count(retries_used: object) -> None:
    """Generic retry admission must share durable fail-closed attempt accounting."""

    policy = RetryPolicy(
        max_retries=1,
        retryable_error_codes=frozenset({RuntimeErrorCode.TRANSIENT}),
        allow_fresh_retry=True,
    )
    failure = RuntimeResult(
        outcome=RuntimeOutcome.FAILED,
        error="temporary provider failure",
        error_code=RuntimeErrorCode.TRANSIENT,
    )

    with pytest.raises(ValueError, match="retries_used"):
        policy.should_retry(failure, retries_used=retries_used)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field_name", "value", "expected_exception"),
    [
        pytest.param("base_delay_seconds", True, TypeError, id="boolean-base-delay"),
        pytest.param("max_delay_seconds", False, TypeError, id="boolean-max-delay"),
        pytest.param("base_delay_seconds", float("nan"), ValueError, id="nan-base-delay"),
        pytest.param("max_delay_seconds", float("inf"), ValueError, id="infinite-max-delay"),
    ],
)
def test_retry_policy_rejects_malformed_delay_configuration(
    field_name: str,
    value: object,
    expected_exception: type[Exception],
) -> None:
    """Malformed retry delay configuration must fail closed before retry planning."""

    with pytest.raises(expected_exception, match=field_name):
        RetryPolicy(**{field_name: value})


def test_zero_delay_automatic_retry_waits_across_restart() -> None:
    policy = RetryPolicy(max_retries=1, base_delay_seconds=0.0, max_delay_seconds=30.0)

    decision = plan_script_retry(
        policy,
        operation_id="network-fetch",
        condition=ScriptRetryCondition.RECOVERABLE_NETWORK_FAILURE,
        retries_used=0,
        now=NOW,
        replay_safe=True,
    )

    assert decision.disposition == ScriptRetryDisposition.SCHEDULED
    assert decision.intent is not None
    assert decision.intent.not_before_utc == NOW + dt.timedelta(seconds=1)

    restored = ScriptRetryIntent.from_payload(decision.intent.to_payload())
    waiting = evaluate_script_retry_intent(
        restored,
        policy,
        now=NOW,
        replay_safe=True,
    )
    assert waiting.disposition == ScriptRetryDisposition.WAITING

    ready = evaluate_script_retry_intent(
        restored,
        policy,
        now=NOW + dt.timedelta(seconds=1),
        replay_safe=True,
    )
    assert ready.disposition == ScriptRetryDisposition.READY


def test_restored_retry_wait_cannot_exceed_active_policy_cap() -> None:
    """Restart must not preserve retry authority beyond the active bounded backoff cap."""

    policy = RetryPolicy(max_retries=1, base_delay_seconds=1.0, max_delay_seconds=30.0)
    restored = ScriptRetryIntent.from_payload(
        {
            "version": 1,
            "operation_id": "network-fetch",
            "condition": ScriptRetryCondition.RECOVERABLE_NETWORK_FAILURE.value,
            "retry_number": 1,
            "not_before_utc": (NOW + dt.timedelta(seconds=31)).isoformat(),
            "deadline_utc": None,
        }
    )

    rejected = evaluate_script_retry_intent(
        restored,
        policy,
        now=NOW,
        replay_safe=True,
    )
    assert rejected.disposition == ScriptRetryDisposition.BACKOFF_LIMIT_EXCEEDED
    assert rejected.intent is None

    boundary = ScriptRetryIntent(
        operation_id="network-fetch",
        condition=ScriptRetryCondition.RECOVERABLE_NETWORK_FAILURE,
        retry_number=1,
        not_before_utc=NOW + dt.timedelta(seconds=30),
    )
    waiting = evaluate_script_retry_intent(
        boundary,
        policy,
        now=NOW,
        replay_safe=True,
    )
    assert waiting.disposition == ScriptRetryDisposition.WAITING
    assert waiting.intent == boundary


def test_automatic_retry_fails_closed_when_policy_cap_is_below_minimum_delay() -> None:
    policy = RetryPolicy(max_retries=1, base_delay_seconds=0.0, max_delay_seconds=0.5)

    decision = plan_script_retry(
        policy,
        operation_id="network-fetch",
        condition=ScriptRetryCondition.RECOVERABLE_NETWORK_FAILURE,
        retries_used=0,
        now=NOW,
        replay_safe=True,
    )

    assert decision.disposition == ScriptRetryDisposition.BACKOFF_LIMIT_EXCEEDED
    assert decision.intent is None
