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


@pytest.mark.parametrize(
    "invalid",
    [
        "failed\x00malformed",
        "failed\x1b[31m",
        "failed\rforged status",
        "failed\u202eoverride",
        "failed\u2028forged status",
        "failed\u0085forged status",
        "failed\ud800surrogate",
        "e\u0301",
    ],
)
def test_runtime_result_rejects_control_and_noncanonical_error(invalid: str) -> None:
    with pytest.raises(ValueError, match="invalid Unicode|noncanonical"):
        RuntimeResult(outcome=RuntimeOutcome.FAILED, error=invalid)


def test_runtime_result_bounds_diagnostic_bytes_before_durable_use() -> None:
    from nika_core.runtime.contracts import MAX_RUNTIME_RESULT_ERROR_UTF8_BYTES

    assert MAX_RUNTIME_RESULT_ERROR_UTF8_BYTES == 4096
    assert RuntimeResult(outcome=RuntimeOutcome.FAILED, error="é" * 2048).error
    with pytest.raises(ValueError, match="size limit"):
        RuntimeResult(outcome=RuntimeOutcome.FAILED, error="é" * 2049)
    with pytest.raises(ValueError, match="size limit"):
        RuntimeResult(outcome=RuntimeOutcome.COMPLETED, error="x" * 4097)


def test_runtime_result_preserves_multiline_and_tabs_without_escape_codes() -> None:
    msg = "first line\nnext\tstep"
    assert RuntimeResult(outcome=RuntimeOutcome.FAILED, error=msg).error == msg
    assert RuntimeResult(outcome=RuntimeOutcome.COMPLETED, error="").error == ""
