from __future__ import annotations

import pytest

from nika_core.model_gateway.contracts import ModelUsage


def test_usage_total_must_cover_all_known_component_tokens() -> None:
    with pytest.raises(ValueError, match="known component tokens"):
        ModelUsage(input_tokens=5, output_tokens=4, total_tokens=6)


def test_usage_total_can_equal_or_exceed_known_component_tokens() -> None:
    exact = ModelUsage(input_tokens=5, output_tokens=4, total_tokens=9)
    extended = ModelUsage(input_tokens=5, output_tokens=4, total_tokens=10)

    assert exact.total_tokens == 9
    assert extended.total_tokens == 10
