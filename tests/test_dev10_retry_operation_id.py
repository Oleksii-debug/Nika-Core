from __future__ import annotations

import datetime as dt

import pytest

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
