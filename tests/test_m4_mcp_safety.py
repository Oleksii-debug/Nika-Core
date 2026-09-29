from __future__ import annotations

import asyncio

import pytest
from mcp.server import MCPServer

from nika_core.mcp_boundary import MCPClientAdapter, MCPServerConfig
from nika_core.tools import ToolCall, ToolRisk


class _BehavioralToolId(str):
    def __new__(cls, value: str, events: list[str]) -> _BehavioralToolId:
        instance = super().__new__(cls, value)
        instance.events = events
        return instance

    def startswith(self, *args: object, **kwargs: object) -> bool:
        self.events.append("startswith")
        return True

    def removeprefix(self, prefix: str) -> str:
        self.events.append("removeprefix")
        return "publish"


def _server(called: list[dict[str, object]], label: str = "published") -> MCPServer:
    server = MCPServer(f"nika-m4-safety-{label}")

    @server.tool()
    async def publish(value: str) -> dict[str, str]:
        """Represent an MCP operation."""
        called.append({"value": value})
        return {label: value}

    return server


def test_risky_mcp_call_fails_closed_without_explicit_approval() -> None:
    called: list[dict[str, object]] = []
    adapter = MCPClientAdapter(MCPServerConfig(server_id="safety", target=_server(called)))
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
    assert called == []


def test_behavioral_tool_id_cannot_spoof_server_routing() -> None:
    called: list[dict[str, object]] = []
    events: list[str] = []
    adapter = MCPClientAdapter(MCPServerConfig(server_id="safety", target=_server(called)))
    call = ToolCall(
        call_id="mcp-spoof-1",
        tool_id=_BehavioralToolId("mcp:other:ignored", events),
        arguments={"value": "blocked"},
    )

    with pytest.raises(TypeError, match="tool_id must be an exact string"):
        asyncio.run(adapter.call(call))

    assert events == []
    assert called == []


def test_adapter_snapshots_external_risk_before_config_mutation() -> None:
    called: list[dict[str, object]] = []
    config = MCPServerConfig(server_id="safety", target=_server(called))
    adapter = MCPClientAdapter(config)

    object.__setattr__(config, "default_risk", ToolRisk.READ_ONLY)
    object.__setattr__(config, "server_id", "redirected")

    result = asyncio.run(
        adapter.call(
            ToolCall(
                call_id="mcp-risk-snapshot-1",
                tool_id="mcp:safety:publish",
                arguments={"value": "blocked"},
            )
        )
    )

    assert result.error == "approval required"
    assert called == []


def test_adapter_snapshots_target_before_config_mutation() -> None:
    original_calls: list[dict[str, object]] = []
    redirected_calls: list[dict[str, object]] = []
    config = MCPServerConfig(
        server_id="readonly",
        target=_server(original_calls, "original"),
        default_risk=ToolRisk.READ_ONLY,
    )
    adapter = MCPClientAdapter(config)

    object.__setattr__(config, "target", _server(redirected_calls, "redirected"))
    object.__setattr__(config, "server_id", "mutated")

    result = asyncio.run(
        adapter.call(
            ToolCall(
                call_id="mcp-target-snapshot-1",
                tool_id="mcp:readonly:publish",
                arguments={"value": "stable"},
            )
        )
    )

    assert result.ok is True
    assert result.output == {"original": "stable"}
    assert original_calls == [{"value": "stable"}]
    assert redirected_calls == []


def test_mcp_config_rejects_behavioral_identity_and_plain_risk() -> None:
    events: list[str] = []

    with pytest.raises(TypeError, match="server_id must be an exact string"):
        MCPServerConfig(
            server_id=_BehavioralToolId("safety", events),
            target=object(),
        )
    with pytest.raises(TypeError, match="default_risk must be an exact ToolRisk"):
        MCPServerConfig(
            server_id="safety",
            target=object(),
            default_risk="read_only",  # type: ignore[arg-type]
        )

    assert events == []
