from __future__ import annotations

import pytest

from nika_core.mcp_boundary import MCPServerConfig
from nika_core.tools import ToolRisk


@pytest.mark.parametrize(
    "raw_risk",
    [
        ToolRisk.EXTERNAL_SIDE_EFFECT.value,
        ToolRisk.HIGH_IMPACT.value,
    ],
)
def test_mcp_config_rejects_raw_string_risk_that_matches_allowed_enum(
    raw_risk: str,
) -> None:
    with pytest.raises(ValueError, match="trusted connector policy"):
        MCPServerConfig(
            server_id="raw-risk",
            target=object(),
            default_risk=raw_risk,  # type: ignore[arg-type]
        )
