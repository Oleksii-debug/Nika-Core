from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from nika_core.runtime.retry import (
    RetryPolicy,
    ScriptRetryCondition,
    ScriptRetryDisposition,
    ScriptRetryIntent,
    evaluate_script_retry_intent,
    plan_script_retry,
)


NOW = datetime(2026, 8, 27, 20, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    "condition",
    [
        ScriptRetryCondition.TEMPORARY_BUSY,
        ScriptRetryCondition.EXPLICIT_RATE_LIMIT,
        ScriptRetryCondition.RECOVERABLE_NETWORK_FAILURE,
        ScriptRetryCondition.TEMPORARY_PAGE_READINESS_FAILURE,
    ],
)
def test_only_declared_transient_conditions_schedule(condition: ScriptRetryCondition) -> None:
    decision = plan_script_retry(
        RetryPolicy(max_retries=2, base_delay_seconds=1, max_delay_seconds=4),
        operation_id="target:українська назва",
        condition=condition,
        retries_used=0,
        now=NOW,
        replay_safe=True,
    )

    assert decision.disposition == ScriptRetryDisposition.SCHEDULED
    assert decision.intent is not None
    assert decision.intent.retry_number == 1
    assert decision.intent.not_before_utc == NOW + timedelta(seconds=1)


@pytest.mark.parametrize(
    "condition",
    [
        ScriptRetryCondition.UNCERTAIN_EXTERNAL_EFFECT,
        ScriptRetryCondition.APPROVAL_DENIED,
        ScriptRetryCondition.PERMISSION_FAILURE,
        ScriptRetryCondition.SEMANTIC_LOCATOR_AMBIGUITY,
        ScriptRetryCondition.DETERMINISTIC_VALIDATION_ERROR,
    ],
)
def test_non_transient_conditions_never_schedule(condition: ScriptRetryCondition) -> None:
    decision = plan_script_retry(
        RetryPolicy(max_retries=5, base_delay_seconds=0.25, max_delay_seconds=2),
        operation_id="operation-1",
        condition=condition,
        retries_used=0,
        now=NOW,
        replay_safe=True,
    )

    assert decision.disposition == ScriptRetryDisposition.NOT_RETRYABLE
    assert decision.condition == condition
    assert decision.intent is None


def test_safe_condition_still_requires_explicit_replay_safety() -> None:
    decision = plan_script_retry(
        RetryPolicy(max_retries=2),
        operation_id="external-call",
        condition=ScriptRetryCondition.RECOVERABLE_NETWORK_FAILURE,
        retries_used=0,
        now=NOW,
        replay_safe=False,
    )

    assert decision.disposition == ScriptRetryDisposition.NOT_RETRYABLE
    assert decision.intent is None


def test_attempts_and_backoff_are_bounded() -> None:
    policy = RetryPolicy(max_retries=3, base_delay_seconds=0.5, max_delay_seconds=1)

    first = plan_script_retry(
        policy,
        operation_id="op",
        condition=ScriptRetryCondition.TEMPORARY_BUSY,
        retries_used=0,
        now=NOW,
        replay_safe=True,
    )
    third = plan_script_retry(
        policy,
        operation_id="op",
        condition=ScriptRetryCondition.TEMPORARY_BUSY,
        retries_used=2,
        now=NOW,
        replay_safe=True,
    )
    exhausted = plan_script_retry(
        policy,
        operation_id="op",
        condition=ScriptRetryCondition.TEMPORARY_BUSY,
        retries_used=3,
        now=NOW,
        replay_safe=True,
    )

    assert first.intent is not None
    assert first.intent.not_before_utc == NOW + timedelta(seconds=0.5)
    assert third.intent is not None
    assert third.intent.not_before_utc == NOW + timedelta(seconds=1)
    assert exhausted.disposition == ScriptRetryDisposition.ATTEMPTS_EXHAUSTED
    assert exhausted.intent is None


def test_rate_limit_hint_is_respected_without_escaping_backoff_bound() -> None:
    policy = RetryPolicy(max_retries=2, base_delay_seconds=1, max_delay_seconds=5)

    bounded = plan_script_retry(
        policy,
        operation_id="rate-limited-op",
        condition=ScriptRetryCondition.EXPLICIT_RATE_LIMIT,
        retries_used=0,
        now=NOW,
        replay_safe=True,
        retry_after_seconds=4,
    )
    too_long = plan_script_retry(
        policy,
        operation_id="rate-limited-op",
        condition=ScriptRetryCondition.EXPLICIT_RATE_LIMIT,
        retries_used=0,
        now=NOW,
        replay_safe=True,
        retry_after_seconds=6,
    )

    assert bounded.intent is not None
    assert bounded.intent.not_before_utc == NOW + timedelta(seconds=4)
    assert too_long.disposition == ScriptRetryDisposition.BACKOFF_LIMIT_EXCEEDED
    assert too_long.intent is None


def test_deadline_blocks_retry_that_cannot_start_before_expiry() -> None:
    policy = RetryPolicy(max_retries=2, base_delay_seconds=2, max_delay_seconds=4)

    decision = plan_script_retry(
        policy,
        operation_id="deadline-op",
        condition=ScriptRetryCondition.RECOVERABLE_NETWORK_FAILURE,
        retries_used=0,
        now=NOW,
        replay_safe=True,
        deadline=NOW + timedelta(seconds=2),
    )

    assert decision.disposition == ScriptRetryDisposition.DEADLINE_EXCEEDED
    assert decision.intent is None


def test_pause_and_cancel_are_fail_closed() -> None:
    policy = RetryPolicy(max_retries=2, base_delay_seconds=1)
    paused = plan_script_retry(
        policy,
        operation_id="paused-op",
        condition=ScriptRetryCondition.TEMPORARY_PAGE_READINESS_FAILURE,
        retries_used=0,
        now=NOW,
        replay_safe=True,
        paused=True,
    )
    cancelled = plan_script_retry(
        policy,
        operation_id="cancelled-op",
        condition=ScriptRetryCondition.TEMPORARY_PAGE_READINESS_FAILURE,
        retries_used=0,
        now=NOW,
        replay_safe=True,
        cancelled=True,
    )

    assert paused.disposition == ScriptRetryDisposition.PAUSED
    assert paused.intent is not None
    assert cancelled.disposition == ScriptRetryDisposition.CANCELLED
    assert cancelled.intent is None


def test_retry_intent_round_trips_through_json_and_restart_rechecks_safety() -> None:
    policy = RetryPolicy(max_retries=2, base_delay_seconds=3, max_delay_seconds=5)
    planned = plan_script_retry(
        policy,
        operation_id="ціль з пробілом",
        condition=ScriptRetryCondition.TEMPORARY_BUSY,
        retries_used=0,
        now=NOW,
        replay_safe=True,
        deadline=NOW + timedelta(minutes=1),
    )
    assert planned.intent is not None

    encoded = json.loads(json.dumps(planned.intent.to_payload(), ensure_ascii=False))
    restored = ScriptRetryIntent.from_payload(encoded)

    waiting = evaluate_script_retry_intent(
        restored,
        policy,
        now=NOW + timedelta(seconds=2),
        replay_safe=True,
    )
    ready = evaluate_script_retry_intent(
        restored,
        policy,
        now=NOW + timedelta(seconds=3),
        replay_safe=True,
    )
    unsafe_after_restart = evaluate_script_retry_intent(
        restored,
        policy,
        now=NOW + timedelta(seconds=3),
        replay_safe=False,
    )

    assert restored == planned.intent
    assert waiting.disposition == ScriptRetryDisposition.WAITING
    assert ready.disposition == ScriptRetryDisposition.READY
    assert unsafe_after_restart.disposition == ScriptRetryDisposition.NOT_RETRYABLE
    assert unsafe_after_restart.intent is None


def test_restart_rechecks_pause_cancel_deadline_and_attempt_policy() -> None:
    intent = ScriptRetryIntent(
        operation_id="restart-op",
        condition=ScriptRetryCondition.TEMPORARY_BUSY,
        retry_number=2,
        not_before_utc=NOW + timedelta(seconds=1),
        deadline_utc=NOW + timedelta(seconds=10),
    )

    paused = evaluate_script_retry_intent(
        intent,
        RetryPolicy(max_retries=2),
        now=NOW + timedelta(seconds=2),
        replay_safe=True,
        paused=True,
    )
    cancelled = evaluate_script_retry_intent(
        intent,
        RetryPolicy(max_retries=2),
        now=NOW + timedelta(seconds=2),
        replay_safe=True,
        cancelled=True,
    )
    expired = evaluate_script_retry_intent(
        intent,
        RetryPolicy(max_retries=2),
        now=NOW + timedelta(seconds=10),
        replay_safe=True,
    )
    stricter_policy = evaluate_script_retry_intent(
        intent,
        RetryPolicy(max_retries=1),
        now=NOW + timedelta(seconds=2),
        replay_safe=True,
    )

    assert paused.disposition == ScriptRetryDisposition.PAUSED
    assert cancelled.disposition == ScriptRetryDisposition.CANCELLED
    assert expired.disposition == ScriptRetryDisposition.DEADLINE_EXCEEDED
    assert stricter_policy.disposition == ScriptRetryDisposition.ATTEMPTS_EXHAUSTED


@pytest.mark.parametrize(
    "payload_patch",
    [
        {"version": 2},
        {"retry_number": True},
        {"condition": "arbitrary_transient"},
        {"not_before_utc": "2026-08-27T20:00:00"},
    ],
)
def test_durable_intent_codec_fails_closed(payload_patch: dict[str, object]) -> None:
    payload = ScriptRetryIntent(
        operation_id="codec-op",
        condition=ScriptRetryCondition.TEMPORARY_BUSY,
        retry_number=1,
        not_before_utc=NOW + timedelta(seconds=1),
        deadline_utc=NOW + timedelta(seconds=10),
    ).to_payload()
    payload.update(payload_patch)

    with pytest.raises(ValueError):
        ScriptRetryIntent.from_payload(payload)


def test_retry_safety_flags_require_real_booleans() -> None:
    with pytest.raises(ValueError, match="replay_safe"):
        plan_script_retry(
            RetryPolicy(max_retries=1),
            operation_id="op",
            condition=ScriptRetryCondition.TEMPORARY_BUSY,
            retries_used=0,
            now=NOW,
            replay_safe="yes",  # type: ignore[arg-type]
        )


def test_retry_intent_rejects_untyped_condition_and_float_version() -> None:
    with pytest.raises(ValueError, match="ScriptRetryCondition"):
        ScriptRetryIntent(
            operation_id="op",
            condition="temporary_busy",  # type: ignore[arg-type]
            retry_number=1,
            not_before_utc=NOW + timedelta(seconds=1),
        )

    payload = ScriptRetryIntent(
        operation_id="op",
        condition=ScriptRetryCondition.TEMPORARY_BUSY,
        retry_number=1,
        not_before_utc=NOW + timedelta(seconds=1),
    ).to_payload()
    payload["version"] = 1.0
    with pytest.raises(ValueError, match="version"):
        ScriptRetryIntent.from_payload(payload)


def test_non_finite_policy_backoff_fails_closed() -> None:
    decision = plan_script_retry(
        RetryPolicy(max_retries=1, base_delay_seconds=0, max_delay_seconds=float("inf")),
        operation_id="op",
        condition=ScriptRetryCondition.TEMPORARY_BUSY,
        retries_used=0,
        now=NOW,
        replay_safe=True,
    )

    assert decision.disposition == ScriptRetryDisposition.BACKOFF_LIMIT_EXCEEDED
    assert decision.intent is None
