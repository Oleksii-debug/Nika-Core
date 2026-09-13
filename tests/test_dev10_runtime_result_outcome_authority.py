from __future__ import annotations

import pytest

from nika_core.runtime.contracts import RuntimeOutcome, RuntimeResult


def test_runtime_result_rejects_raw_string_outcome_before_authority_use() -> None:
    with pytest.raises(TypeError, match="outcome must be a RuntimeOutcome"):
        RuntimeResult(outcome="completed")  # type: ignore[arg-type]


def test_runtime_result_accepts_canonical_runtime_outcome() -> None:
    result = RuntimeResult(outcome=RuntimeOutcome.COMPLETED)

    assert result.outcome is RuntimeOutcome.COMPLETED
