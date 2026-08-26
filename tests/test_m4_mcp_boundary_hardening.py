from __future__ import annotations

import pytest

from nika_core.mcp_boundary import MCPServerConfig
from nika_core.tools import ToolRisk


@pytest.mark.parametrize("risk", [ToolRisk.READ_ONLY, ToolRisk.LOCAL_WRITE])
def test_untrusted_mcp_config_rejects_risk_downgrade(risk: ToolRisk) -> None:
    with pytest.raises(ValueError, match="trusted connector policy"):
        MCPServerConfig(server_id="untrusted", target=object(), default_risk=risk)


def test_mcp_config_allows_conservative_risk_levels() -> None:
    assert (
        MCPServerConfig(server_id="external", target=object()).default_risk
        is ToolRisk.EXTERNAL_SIDE_EFFECT
    )
    assert (
        MCPServerConfig(
            server_id="high-impact",
            target=object(),
            default_risk=ToolRisk.HIGH_IMPACT,
        ).default_risk
        is ToolRisk.HIGH_IMPACT
    )


def test_mcp_config_repr_does_not_expose_transport_target() -> None:
    secret = "oauth-token=nika-m4-secret-canary"

    class SecretTarget:
        def __repr__(self) -> str:
            return secret

    config = MCPServerConfig(server_id="secret-target", target=SecretTarget())

    rendered = repr(config)

    assert secret not in rendered
    assert "target=" not in rendered
