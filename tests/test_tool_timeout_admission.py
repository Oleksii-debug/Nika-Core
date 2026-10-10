from __future__ import annotations

import pytest

from nika_core.tools import ToolRisk, ToolSpec


@pytest.mark.parametrize(
    "timeout",
    [
        0,
        -1,
        -0.01,
        True,
        False,
        None,
        "30",
        float("nan"),
        float("inf"),
        -float("inf"),
        86_400.01,
        10**1000,
    ],
)
def test_tool_spec_rejects_invalid_or_unbounded_deadlines(timeout: object) -> None:
    with pytest.raises(ValueError, match="timeout_seconds must be finite"):
        ToolSpec(
            tool_id="proof.tool",
            description="deadline admission",
            timeout_seconds=timeout,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("timeout", [0.01, 1, 30.0, 86_400, 86_400.0])
def test_tool_spec_preserves_positive_finite_deadlines(timeout: float) -> None:
    spec = ToolSpec(
        tool_id="proof.tool",
        description="deadline admission",
        timeout_seconds=timeout,
    )
    assert spec.timeout_seconds == timeout


def test_default_tool_deadline_remains_thirty_seconds() -> None:
    assert ToolSpec(tool_id="proof.tool", description="default").timeout_seconds == 30.0


@pytest.mark.parametrize("risk", ["read_only", "external_side_effect", 1, None, True])
def test_tool_spec_rejects_non_enum_risk_carriers(risk: object) -> None:
    with pytest.raises(ValueError, match="risk must be a ToolRisk"):
        ToolSpec(
            tool_id="proof.tool",
            description="risk admission",
            risk=risk,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("risk", list(ToolRisk))
def test_tool_spec_preserves_canonical_risk_enum(risk: ToolRisk) -> None:
    assert ToolSpec(tool_id="proof.tool", description="risk", risk=risk).risk is risk
