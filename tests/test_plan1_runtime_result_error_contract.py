"""Plan 1 Section 2: failed runtime outcomes must carry inert text diagnostics."""

from __future__ import annotations

import pytest

from nika_core.runtime.contracts import RuntimeOutcome, RuntimeResult


class HostileRuntimeError(str):
    """Untrusted diagnostic carrier must not execute an overridden method."""

    def __bool__(self) -> bool:
        raise AssertionError("custom error truthiness was evaluated")

    def strip(self, *args: object, **kwargs: object) -> str:
        raise AssertionError("custom error strip was evaluated")


@pytest.mark.parametrize("error", [True, 1, object(), HostileRuntimeError("failed")])
def test_runtime_result_rejects_nonplain_error_carrier(error: object) -> None:
    with pytest.raises(TypeError, match="plain string"):
        RuntimeResult(outcome=RuntimeOutcome.FAILED, error=error)


@pytest.mark.parametrize("error", [None, "", " ", "\t"])
def test_failed_result_rejects_missing_or_whitespace_error(error: str | None) -> None:
    with pytest.raises(ValueError, match="requires a nonempty error"):
        RuntimeResult(outcome=RuntimeOutcome.FAILED, error=error)


def test_failed_result_preserves_regular_diagnostic_and_newline() -> None:
    message = "durable recovery failed\noperator reconciliation required"
    result = RuntimeResult(outcome=RuntimeOutcome.FAILED, error=message)
    assert result.error == message


def test_nonfailed_result_keeps_existing_optional_diagnostic_behavior() -> None:
    assert RuntimeResult(outcome=RuntimeOutcome.COMPLETED).error is None
    assert RuntimeResult(outcome=RuntimeOutcome.COMPLETED, error="").error == ""
    with pytest.raises(TypeError, match="plain string"):
        RuntimeResult(outcome=RuntimeOutcome.COMPLETED, error=HostileRuntimeError("x"))
