from __future__ import annotations

import asyncio
import math
import re
from dataclasses import dataclass, field
from typing import Any

from mcp import Client
from mcp.shared.exceptions import MCPError
from mcp.types import REQUEST_TIMEOUT

from nika_core.tools import (
    ApprovalPolicy,
    ToolCall,
    ToolEffectGuard,
    ToolExecutor,
    ToolResult,
    ToolRisk,
    ToolSpec,
)


_MCP_TOOL_NAME_RE = re.compile(r"[A-Za-z0-9_.-]{1,128}\Z")


class MCPBoundaryError(ValueError):
    """Safe normalized MCP discovery/boundary failure."""


@dataclass(frozen=True, slots=True)
class MCPServerConfig:
    server_id: str
    target: Any = field(repr=False)
    default_risk: ToolRisk = ToolRisk.EXTERNAL_SIDE_EFFECT
    timeout_seconds: float = 30.0
    max_tool_pages: int = 32
    max_tools: int = 512

    def __post_init__(self) -> None:
        if not isinstance(self.server_id, str) or not self.server_id.strip():
            raise ValueError("server_id must not be empty")
        if (
            self.server_id != self.server_id.strip()
            or ":" in self.server_id
            or any(character.isspace() for character in self.server_id)
        ):
            raise ValueError("server_id must not contain whitespace or ':'")
        if self.default_risk not in {
            ToolRisk.EXTERNAL_SIDE_EFFECT,
            ToolRisk.HIGH_IMPACT,
        }:
            raise ValueError("MCP risk downgrades require a trusted connector policy")
        if isinstance(self.timeout_seconds, bool) or not isinstance(
            self.timeout_seconds, (int, float)
        ):
            raise ValueError("timeout_seconds must be a finite positive number")
        try:
            normalized_timeout = float(self.timeout_seconds)
        except (OverflowError, TypeError, ValueError):
            raise ValueError("timeout_seconds must be a finite positive number") from None
        if not math.isfinite(normalized_timeout) or normalized_timeout <= 0:
            raise ValueError("timeout_seconds must be a finite positive number")
        object.__setattr__(self, "timeout_seconds", normalized_timeout)
        for field_name in ("max_tool_pages", "max_tools"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{field_name} must be a positive integer")


class MCPClientAdapter:
    """Translate official MCP SDK tools into stable Nika ToolExecutor contracts."""

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
        specs: list[ToolSpec] = []
        seen_tool_ids: set[str] = set()
        seen_cursors: set[str] = set()
        cursor: str | None = None

        try:
            async with Client(
                self._config.target,
                read_timeout_seconds=float(self._config.timeout_seconds),
            ) as client:
                for _page_number in range(self._config.max_tool_pages):
                    result = await client.list_tools(cursor=cursor)
                    for tool in result.tools:
                        tool_name = self._validated_tool_name(tool.name)
                        tool_id = f"mcp:{self._config.server_id}:{tool_name}"
                        if tool_id in seen_tool_ids:
                            raise MCPBoundaryError("duplicate MCP tool id")
                        if len(specs) >= self._config.max_tools:
                            raise MCPBoundaryError("MCP tool catalog exceeds max_tools")
                        seen_tool_ids.add(tool_id)
                        specs.append(
                            ToolSpec(
                                tool_id=tool_id,
                                description=tool.description or tool.title or tool.name,
                                risk=self._config.default_risk,
                                timeout_seconds=float(self._config.timeout_seconds),
                                input_schema=dict(tool.input_schema or {}),
                            )
                        )
                    next_cursor = result.next_cursor
                    if next_cursor is None:
                        return tuple(specs)
                    if next_cursor in seen_cursors:
                        raise MCPBoundaryError(
                            "MCP tool catalog repeated pagination cursor"
                        )
                    seen_cursors.add(next_cursor)
                    cursor = next_cursor
        except asyncio.CancelledError:
            raise
        except MCPBoundaryError:
            raise
        except Exception:  # noqa: BLE001 - suppress untrusted transport/decode details.
            raise MCPBoundaryError("MCP tool discovery failed") from None

        raise MCPBoundaryError("MCP tool catalog exceeds max_tool_pages")

    @staticmethod
    def _validated_tool_name(value: object) -> str:
        if not isinstance(value, str) or _MCP_TOOL_NAME_RE.fullmatch(value) is None:
            raise MCPBoundaryError("invalid MCP tool name")
        return value

    async def register_tools(self, executor: ToolExecutor) -> tuple[ToolSpec, ...]:
        """Discover once and register exact ToolSpecs into a caller-owned canonical executor."""
        if not isinstance(executor, ToolExecutor):
            raise TypeError("executor must be ToolExecutor")

        specs = await self.list_tools()
        existing_ids = {spec.tool_id for spec in executor.specs()}
        collisions = sorted(spec.tool_id for spec in specs if spec.tool_id in existing_ids)
        if collisions:
            raise ValueError(
                "MCP tool id already registered: " + ", ".join(collisions)
            )

        for spec in specs:
            executor.register(spec, self._handler_for(spec.tool_id))
        return specs

    async def call(self, call: ToolCall) -> ToolResult:
        """Compatibility path: execute the exact currently discovered spec through ToolExecutor."""
        prefix = f"mcp:{self._config.server_id}:"
        if not call.tool_id.startswith(prefix):
            return ToolResult(
                call_id=call.call_id,
                tool_id=call.tool_id,
                error="wrong MCP server",
            )

        try:
            specs = await self.list_tools()
        except (MCPBoundaryError, ValueError):
            return ToolResult(
                call_id=call.call_id,
                tool_id=call.tool_id,
                error="MCP tool discovery failed",
            )

        spec = next((item for item in specs if item.tool_id == call.tool_id), None)
        if spec is None:
            return ToolResult(
                call_id=call.call_id,
                tool_id=call.tool_id,
                error="unknown MCP tool",
            )

        executor = ToolExecutor(
            approval_policy=self._approval_policy,
            effect_guard=self._effect_guard,
        )
        executor.register(spec, self._handler_for(spec.tool_id))
        return await executor.execute(call)

    def _handler_for(self, tool_id: str):
        prefix = f"mcp:{self._config.server_id}:"
        if not tool_id.startswith(prefix):
            raise ValueError("wrong MCP server")
        tool_name = tool_id.removeprefix(prefix)
        if not tool_name:
            raise ValueError("invalid MCP tool id")

        async def invoke(arguments: dict[str, object]) -> object:
            try:
                async with Client(
                    self._config.target,
                    read_timeout_seconds=float(self._config.timeout_seconds),
                ) as client:
                    result = await client.call_tool(tool_name, arguments)
            except asyncio.CancelledError:
                raise
            except MCPError as exc:
                if exc.code == REQUEST_TIMEOUT:
                    raise TimeoutError("MCP tool timed out") from None
                raise RuntimeError("MCP tool call failed") from None
            except Exception:
                raise RuntimeError("MCP tool call failed") from None

            if result.is_error:
                raise RuntimeError("MCP tool failed")
            if result.structured_content is not None:
                return result.structured_content
            return tuple(str(block) for block in result.content)

        return invoke
