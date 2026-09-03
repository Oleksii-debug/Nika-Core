from __future__ import annotations

import asyncio

from mcp.server import MCPServer

from nika_core.mcp_boundary import MCPClientAdapter, MCPServerConfig
from nika_core.tools import ToolCall, ToolRisk, ToolSpec


def _server(called: list[dict[str, object]]) -> MCPServer:
    server = MCPServer("nika-w62-mcp")

    @server.tool()
    async def publish(value: str) -> dict[str, str]:
        called.append({"value": value})
        return {"published": value}

    return server


def test_mcp_caller_approved_true_is_not_authority() -> None:
    called: list[dict[str, object]] = []
    adapter = MCPClientAdapter(MCPServerConfig(server_id="w62", target=_server(called)))
    result = asyncio.run(
        adapter.call(
            ToolCall(
                call_id="call-mcp-bypass",
                tool_id="mcp:w62:publish",
                arguments={"value": "blocked"},
                approved=True,
            )
        )
    )
    assert result.error == "approval required"
    assert called == []


def test_mcp_trusted_policy_is_consulted_even_if_caller_sets_true() -> None:
    called: list[dict[str, object]] = []
    policy_calls = 0

    async def deny(spec: ToolSpec, call: ToolCall) -> bool:
        nonlocal policy_calls
        policy_calls += 1
        assert spec.risk is ToolRisk.EXTERNAL_SIDE_EFFECT
        assert call.approved is True
        return False

    adapter = MCPClientAdapter(
        MCPServerConfig(server_id="w62", target=_server(called)),
        approval_policy=deny,
    )
    result = asyncio.run(
        adapter.call(
            ToolCall(
                call_id="call-mcp-deny",
                tool_id="mcp:w62:publish",
                arguments={"value": "blocked"},
                approved=True,
            )
        )
    )
    assert result.error == "approval required"
    assert policy_calls == 1
    assert called == []
