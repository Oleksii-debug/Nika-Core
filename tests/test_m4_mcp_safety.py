from __future__ import annotations

import asyncio

from mcp.server import MCPServer

from nika_core.mcp_boundary import MCPClientAdapter, MCPServerConfig
from nika_core.tools import ToolCall


def test_risky_mcp_call_fails_closed_without_explicit_approval() -> None:
    called = False
    server = MCPServer("nika-m4-safety-test")

    @server.tool()
    async def publish(value: str) -> dict[str, str]:
        """Represent an external side effect."""
        nonlocal called
        called = True
        return {"published": value}

    adapter = MCPClientAdapter(MCPServerConfig(server_id="safety", target=server))
    result = asyncio.run(
        adapter.call(
            ToolCall(
                call_id="mcp-denied-1",
                tool_id="mcp:safety:publish",
                arguments={"value": "blocked"},
            )
        )
    )

    assert result.ok is False
    assert result.error == "approval required"
    assert called is False
