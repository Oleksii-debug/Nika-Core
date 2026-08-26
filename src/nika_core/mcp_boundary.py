from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from mcp import Client
from mcp.shared.exceptions import MCPError
from mcp.types import REQUEST_TIMEOUT

from nika_core.tools import ToolCall, ToolResult, ToolRisk, ToolSpec


@dataclass(frozen=True, slots=True)
class MCPServerConfig:
    server_id: str
    target: Any = field(repr=False)
    default_risk: ToolRisk = ToolRisk.EXTERNAL_SIDE_EFFECT
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if not self.server_id.strip():
            raise ValueError("server_id must not be empty")
        if ":" in self.server_id or any(character.isspace() for character in self.server_id):
            raise ValueError("server_id must not contain whitespace or ':'")
        if self.default_risk not in {
            ToolRisk.EXTERNAL_SIDE_EFFECT,
            ToolRisk.HIGH_IMPACT,
        }:
            raise ValueError("MCP risk downgrades require a trusted connector policy")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")


class MCPClientAdapter:
    """Translate official MCP SDK client results into stable Nika tool contracts."""

    def __init__(self, config: MCPServerConfig) -> None:
        self._config = config

    async def list_tools(self) -> tuple[ToolSpec, ...]:
        async with Client(
            self._config.target,
            read_timeout_seconds=self._config.timeout_seconds,
        ) as client:
            result = await client.list_tools()
        specs: list[ToolSpec] = []
        for tool in result.tools:
            specs.append(
                ToolSpec(
                    tool_id=f"mcp:{self._config.server_id}:{tool.name}",
                    description=tool.description or tool.title or tool.name,
                    risk=self._config.default_risk,
                    timeout_seconds=self._config.timeout_seconds,
                    input_schema=dict(tool.input_schema or {}),
                )
            )
        return tuple(specs)

    async def call(self, call: ToolCall) -> ToolResult:
        prefix = f"mcp:{self._config.server_id}:"
        if not call.tool_id.startswith(prefix):
            return ToolResult(call_id=call.call_id, tool_id=call.tool_id, error="wrong MCP server")
        tool_name = call.tool_id.removeprefix(prefix)
        if not tool_name:
            return ToolResult(call_id=call.call_id, tool_id=call.tool_id, error="invalid MCP tool id")
        if (
            self._config.default_risk
            in {ToolRisk.EXTERNAL_SIDE_EFFECT, ToolRisk.HIGH_IMPACT}
            and not call.approved
        ):
            return ToolResult(
                call_id=call.call_id,
                tool_id=call.tool_id,
                error="approval required",
            )
        try:
            async with Client(
                self._config.target,
                read_timeout_seconds=self._config.timeout_seconds,
            ) as client:
                result = await client.call_tool(tool_name, call.arguments)
        except MCPError as exc:
            error = "MCP tool timed out" if exc.code == REQUEST_TIMEOUT else "MCP tool call failed"
            return ToolResult(call_id=call.call_id, tool_id=call.tool_id, error=error)
        except Exception:  # noqa: BLE001 - normalize transport failures without leaking details.
            return ToolResult(
                call_id=call.call_id,
                tool_id=call.tool_id,
                error="MCP tool call failed",
            )
        if result.is_error:
            return ToolResult(call_id=call.call_id, tool_id=call.tool_id, error="MCP tool failed")
        if result.structured_content is not None:
            output: object = result.structured_content
        else:
            output = tuple(str(block) for block in result.content)
        return ToolResult(call_id=call.call_id, tool_id=call.tool_id, output=output)
