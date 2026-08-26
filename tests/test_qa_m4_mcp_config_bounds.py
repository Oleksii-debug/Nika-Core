from __future__ import annotations

import math

import pytest

from nika_core.mcp_boundary import MCPServerConfig


@pytest.mark.parametrize(
    "timeout_seconds",
    [math.nan, math.inf, -math.inf],
)
def test_mcp_config_rejects_non_finite_deadline(timeout_seconds: float) -> None:
    """Bounded MCP execution requires a finite request deadline."""
    with pytest.raises(ValueError, match="timeout_seconds"):
        MCPServerConfig(
            server_id="qa-finite-deadline",
            target=object(),
            timeout_seconds=timeout_seconds,
        )


@pytest.mark.parametrize(
    ("field_name", "field_value"),
    [
        ("max_tool_pages", True),
        ("max_tool_pages", 1.5),
        ("max_tools", True),
        ("max_tools", 1.5),
    ],
)
def test_mcp_config_rejects_non_integer_catalog_bounds(
    field_name: str,
    field_value: object,
) -> None:
    """Catalog limits are counts and must fail closed unless they are real integers."""
    with pytest.raises(ValueError, match=field_name):
        MCPServerConfig(
            server_id="qa-integer-catalog-bounds",
            target=object(),
            **{field_name: field_value},
        )
