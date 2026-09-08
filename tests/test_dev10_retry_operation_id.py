from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from nika_core.runtime.retry import ScriptRetryCondition, ScriptRetryIntent


NOW = datetime(2026, 9, 8, 13, 0, tzinfo=UTC)


@pytest.mark.parametrize("operation_id", [" retry-op", "retry-op ", "\tretry-op", "retry-op\n"])
def test_script_retry_intent_rejects_noncanonical_operation_id(operation_id: str) -> None:
    """Reject retry authority that downstream durable bindings cannot decode safely."""

    with pytest.raises(ValueError, match="operation_id"):
        ScriptRetryIntent(
            operation_id=operation_id,
            condition=ScriptRetryCondition.RECOVERABLE_NETWORK_FAILURE,
            retry_number=1,
            not_before_utc=NOW + timedelta(seconds=1),
        )


def test_script_retry_intent_keeps_internal_spaces_valid() -> None:
    intent = ScriptRetryIntent(
        operation_id="retry target one",
        condition=ScriptRetryCondition.RECOVERABLE_NETWORK_FAILURE,
        retry_number=1,
        not_before_utc=NOW + timedelta(seconds=1),
    )

    assert intent.operation_id == "retry target one"
