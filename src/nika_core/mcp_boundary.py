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


_MAX_MCP_SEGMENT_CHARS = 128


def _exact_utf8_text(
    value: object,
    *,
    field: str,
    non_empty: bool = False,
) -> str:
    if type(value) is not str:
        raise TypeError(f"{field} must be an exact string")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{field} must be valid UTF-8") from exc
    if non_empty and not value.strip():
        raise ValueError(f"{field} must not be empty")
    return value


def _exact_mcp_segment(value: object, *, field: str) -> str:
    text = _exact_utf8_text(value, field=field, non_empty=True)
    if len(text) > _MAX_MCP_SEGMENT_CHARS:
        raise ValueError(
            f"{field} must contain at most {_MAX_MCP_SEGMENT_CHARS} characters"
        )
    if text != text.strip():
        raise ValueError(f"{field} must not contain edge whitespace")
    if ":" in text:
        raise ValueError(f"{field} must not contain ':'")
    return text


def _exact_tool_risk(value: object) -> ToolRisk:
    if type(value) is not ToolRisk:
        raise TypeError("default_risk must be an exact ToolRisk")
    return value


def _snapshot_tool_call(call: ToolCall) -> ToolCall:
    if type(call) is not ToolCall:
        raise TypeError("call must be an exact ToolCall")
    try:
        call_id = _exact_utf8_text(call.call_id, field="call_id")
        tool_id = _exact_utf8_text(call.tool_id, field="tool_id", non_empty=True)
        arguments = call.arguments
        approved = call.approved
        task_id = call.task_id
        authorization = call.authorization
    except AttributeError as exc:
        raise ValueError("call is incomplete") from exc

    if type(arguments) is not dict:
        raise TypeError("arguments must be an exact dict")
    if type(approved) is not bool:
        raise TypeError("approved must be an exact bool")
    if task_id is not None:
        task_id = _exact_utf8_text(task_id, field="task_id")

    return ToolCall(
        call_id=call_id,
        tool_id=tool_id,
        arguments=dict(arguments),
        approved=approved,
        task_id=task_id,
        authorization=authorization,
    )


@dataclass(frozen=True, slots=True)
class MCPServerConfig:
    server_id: str
    target: Any
    default_risk: ToolRisk = ToolRisk.EXTERNAL_SIDE_EFFECT

    def __post_init__(self) -> None:
        _exact_mcp_segment(self.server_id, field="server_id")
        _exact_tool_risk(self.default_risk)


class MCPClientAdapter:
    """Translate official MCP SDK client results into stable Nika tool contracts."""

    def __init__(
        self,
        config: MCPServerConfig,
        *,
        approval_policy: ApprovalPolicy | None = None,
        effect_guard: ToolEffectGuard | None = None,
    ) -> None:
        if type(config) is not MCPServerConfig:
            raise TypeError("config must be an exact MCPServerConfig")
        try:
            server_id = _exact_mcp_segment(config.server_id, field="server_id")
            target = config.target
            default_risk = _exact_tool_risk(config.default_risk)
        except AttributeError as exc:
            raise ValueError("config is incomplete") from exc

        self._server_id = server_id
        self._target = target
        self._default_risk = default_risk
        self._approval_policy = approval_policy
        self._effect_guard = effect_guard

    async def list_tools(self) -> tuple[ToolSpec, ...]:
        async with Client(self._target) as client:
            result = await client.list_tools()
        specs: list[ToolSpec] = []
        seen_tool_ids: set[str] = set()
        for tool in result.tools:
            tool_name = _exact_mcp_segment(
                tool.name,
                field="MCP tool name",
            )
            tool_id = f"mcp:{self._server_id}:{tool_name}"
            if tool_id in seen_tool_ids:
                raise ValueError(f"duplicate MCP tool id: {tool_id}")
            seen_tool_ids.add(tool_id)
            specs.append(
                ToolSpec(
                    tool_id=tool_id,
                    description=tool.description or tool.title or tool_name,
                    risk=self._default_risk,
                    input_schema=dict(tool.input_schema or {}),
                )
            )
        return tuple(specs)

    async def call(self, call: ToolCall) -> ToolResult:
        canonical_call = _snapshot_tool_call(call)
        prefix = f"mcp:{self._server_id}:"
        if not canonical_call.tool_id.startswith(prefix):
            return ToolResult(
                call_id=canonical_call.call_id,
                tool_id=canonical_call.tool_id,
                error="wrong MCP server",
            )
        tool_name = canonical_call.tool_id.removeprefix(prefix)
        if not tool_name.strip():
            return ToolResult(
                call_id=canonical_call.call_id,
                tool_id=canonical_call.tool_id,
                error="invalid MCP tool id",
            )
        tool_name = _exact_mcp_segment(tool_name, field="MCP tool name")
        spec = ToolSpec(
            tool_id=canonical_call.tool_id,
            description=f"MCP tool {tool_name}",
            risk=self._default_risk,
        )

        async def invoke(arguments: dict[str, object]) -> object:
            async with Client(self._target) as client:
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
        return await executor.execute(canonical_call)
