from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mcp import Client

from nika_core.tools import (
    ApprovalPolicy,
    ToolCall,
    ToolEffectGuard,
    ToolExecutor,
    ToolResult,
    ToolRisk,
    ToolSpec,
)


@dataclass(frozen=True, slots=True)
class MCPServerConfig:
    server_id: str
    target: Any
    default_risk: ToolRisk = ToolRisk.EXTERNAL_SIDE_EFFECT

    def __post_init__(self) -> None:
        if not self.server_id.strip():
            raise ValueError("server_id must not be empty")


class MCPClientAdapter:
    """Translate official MCP SDK client results into stable Nika tool contracts."""

    def __init__(
        self,
        config: MCPServerConfig,
        *,
        approval_policy: ApprovalPolicy | None = None,
        effect_guard: ToolEffectGuard | None = None,
    ) -> None:
        self._config = config
        self._approval_policy = approval_policy
        self._effect_guard = effect_guard

    async def list_tools(self) -> tuple[ToolSpec, ...]:
        async with Client(self._config.target) as client:
            result = await client.list_tools()
        specs: list[ToolSpec] = []
        for tool in result.tools:
            specs.append(
                ToolSpec(
                    tool_id=f"mcp:{self._config.server_id}:{tool.name}",
                    description=tool.description or tool.title or tool.name,
                    risk=self._config.default_risk,
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
            return ToolResult(
                call_id=call.call_id,
                tool_id=call.tool_id,
                error="invalid MCP tool id",
            )
        spec = ToolSpec(
            tool_id=call.tool_id,
            description=f"MCP tool {tool_name}",
            risk=self._config.default_risk,
        )
        async def invoke(arguments: dict[str, object]) -> object:
            async with Client(self._config.target) as client:
                result = await client.call_tool(tool_name, arguments)
            if result.is_error:
                raise RuntimeError("MCP tool failed")
            if result.structured_content is not None:
                return result.structured_content
            return tuple(str(block) for block in result.content)

        executor = ToolExecutor(
            approval_policy=self._approval_policy,
            effect_guard=self._effect_guard,
        )
        executor.register(spec, invoke)
        return await executor.execute(call)
