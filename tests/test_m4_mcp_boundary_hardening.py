from __future__ import annotations

import asyncio

import pytest
from mcp.server import MCPServer

from nika_core.mcp_boundary import MCPClientAdapter, MCPServerConfig
from nika_core.tools import ToolCall, ToolRisk


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


@pytest.mark.parametrize("server_id", [" leading", "inner space", "nested:server"])
def test_mcp_config_rejects_ambiguous_server_namespace(server_id: str) -> None:
    with pytest.raises(ValueError, match="server_id"):
        MCPServerConfig(server_id=server_id, target=object())


@pytest.mark.parametrize("timeout_seconds", [0.0, -1.0])
def test_mcp_config_rejects_non_positive_deadline(timeout_seconds: float) -> None:
    with pytest.raises(ValueError, match="timeout_seconds"):
        MCPServerConfig(
            server_id="deadline",
            target=object(),
            timeout_seconds=timeout_seconds,
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


def test_discovered_mcp_tool_inherits_boundary_deadline() -> None:
    server = MCPServer("nika-m4-deadline-discovery")

    @server.tool()
    async def echo(value: str) -> dict[str, str]:
        """Echo a value."""
        return {"value": value}

    adapter = MCPClientAdapter(
        MCPServerConfig(server_id="deadline-discovery", target=server, timeout_seconds=1.25)
    )

    specs = asyncio.run(adapter.list_tools())

    assert len(specs) == 1
    assert specs[0].timeout_seconds == 1.25


def test_direct_mcp_call_normalizes_sdk_timeout() -> None:
    server = MCPServer("nika-m4-deadline-call")

    @server.tool()
    async def slow() -> dict[str, bool]:
        """Sleep beyond the configured deadline."""
        await asyncio.sleep(0.2)
        return {"completed": True}

    adapter = MCPClientAdapter(
        MCPServerConfig(server_id="deadline-call", target=server, timeout_seconds=0.01)
    )

    result = asyncio.run(
        adapter.call(
            ToolCall(
                call_id="deadline-call-1",
                tool_id="mcp:deadline-call:slow",
                arguments={},
                approved=True,
            )
        )
    )

    assert result.ok is False
    assert result.error == "MCP tool timed out"


def test_direct_mcp_call_normalizes_transport_failure_without_raw_details() -> None:
    secret = "transport-secret-canary"

    class BrokenTransport:
        async def __aenter__(self) -> object:
            raise RuntimeError(secret)

        async def __aexit__(self, *_args: object) -> None:
            return None

    adapter = MCPClientAdapter(MCPServerConfig(server_id="broken", target=BrokenTransport()))

    result = asyncio.run(
        adapter.call(
            ToolCall(
                call_id="broken-call-1",
                tool_id="mcp:broken:anything",
                arguments={},
                approved=True,
            )
        )
    )

    assert result.ok is False
    assert result.error == "MCP tool call failed"
    assert secret not in result.error
