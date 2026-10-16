from __future__ import annotations

import json
import math
import re
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
_MAX_MCP_CURSOR_BYTES = 4096
_MAX_MCP_LIST_PAGES = 1000
_MAX_MCP_LIST_TOOLS = 10_000
_MAX_MCP_DISCOVERY_BYTES = 8_388_608
_MAX_MCP_ARGUMENT_DEPTH = 64
_MAX_MCP_ARGUMENT_NODES = 10_000
_MAX_MCP_ARGUMENT_BYTES = 1_048_576
_MAX_MCP_INTEGER_BITS = 4096
_MCP_TOOL_NAME_RE = re.compile(r"[A-Za-z0-9._-]+")


def _encode_utf8_strict(value: str, *, field: str) -> bytes:
    try:
        return value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{field} must be valid UTF-8") from exc


def _exact_utf8_text(
    value: object,
    *,
    field: str,
    non_empty: bool = False,
) -> str:
    if type(value) is not str:
        raise TypeError(f"{field} must be an exact string")
    _encode_utf8_strict(value, field=field)
    if non_empty and not value.strip():
        raise ValueError(f"{field} must not be empty")
    return value


def _exact_mcp_segment(value: object, *, field: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{field} must be an exact string")
    if len(value) > _MAX_MCP_SEGMENT_CHARS:
        raise ValueError(
            f"{field} must contain at most {_MAX_MCP_SEGMENT_CHARS} characters"
        )
    text = _exact_utf8_text(value, field=field, non_empty=True)
    if text != text.strip():
        raise ValueError(f"{field} must not contain edge whitespace")
    if ":" in text:
        raise ValueError(f"{field} must not contain ':'")
    return text


def _exact_mcp_tool_name(value: object) -> str:
    text = _exact_mcp_segment(value, field="MCP tool name")
    if _MCP_TOOL_NAME_RE.fullmatch(text) is None:
        raise ValueError(
            "MCP tool name must use only A-Z, a-z, 0-9, '_', '-' or '.'"
        )
    return text


def _exact_mcp_cursor(value: object) -> str:
    field = "MCP next cursor"
    if type(value) is not str:
        raise TypeError(f"{field} must be an exact string")
    if len(value) > _MAX_MCP_CURSOR_BYTES:
        raise ValueError(
            f"{field} must contain at most {_MAX_MCP_CURSOR_BYTES} UTF-8 bytes"
        )
    encoded = _encode_utf8_strict(value, field=field)
    if len(encoded) > _MAX_MCP_CURSOR_BYTES:
        raise ValueError(
            f"{field} must contain at most {_MAX_MCP_CURSOR_BYTES} UTF-8 bytes"
        )
    return value


def _exact_tool_risk(value: object) -> ToolRisk:
    if type(value) is not ToolRisk:
        raise TypeError("default_risk must be an exact ToolRisk")
    return value


def _charge_mcp_argument_budget(
    budget: list[int], *, nodes: int = 0, encoded_bytes: int = 0
) -> None:
    if nodes > budget[0]:
        raise ValueError("MCP arguments exceed safe node limit")
    if encoded_bytes > budget[1]:
        raise ValueError("MCP arguments exceed safe UTF-8 byte limit")
    budget[0] -= nodes
    budget[1] -= encoded_bytes


def _mcp_scalar_bytes(value: str | float | bool | None) -> int:
    if type(value) is str and len(value) > _MAX_MCP_ARGUMENT_BYTES:
        raise ValueError("MCP arguments exceed safe UTF-8 byte limit")
    return len(
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        .encode("utf-8")
    )


def _snapshot_mcp_json(
    value: object,
    *,
    path: str,
    depth: int = 0,
    active: set[int] | None = None,
    budget: list[int] | None = None,
) -> object:
    if depth > _MAX_MCP_ARGUMENT_DEPTH:
        raise ValueError("MCP arguments exceed safe nesting depth")
    if budget is None:
        budget = [_MAX_MCP_ARGUMENT_NODES, _MAX_MCP_ARGUMENT_BYTES]
    _charge_mcp_argument_budget(budget, nodes=1)

    if value is None or type(value) is bool:
        _charge_mcp_argument_budget(budget, encoded_bytes=_mcp_scalar_bytes(value))
        return value
    if type(value) is int:
        if value.bit_length() > _MAX_MCP_INTEGER_BITS:
            raise ValueError("MCP arguments exceed safe integer size")
        _charge_mcp_argument_budget(budget, encoded_bytes=_mcp_scalar_bytes(value))
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError(f"{path} must not contain NaN or infinity")
        _charge_mcp_argument_budget(budget, encoded_bytes=_mcp_scalar_bytes(value))
        return value
    if type(value) is str:
        if len(value) > budget[1]:
            raise ValueError("MCP arguments exceed safe UTF-8 byte limit")
        text = _exact_utf8_text(value, field=path)
        _charge_mcp_argument_budget(budget, encoded_bytes=_mcp_scalar_bytes(text))
        return text
    if type(value) is not dict and type(value) is not list:
        raise TypeError(f"{path} must contain only exact JSON values")
    if len(value) > _MAX_MCP_ARGUMENT_NODES:
        raise ValueError("MCP arguments exceed safe node limit")

    if active is None:
        active = set()
    identity = id(value)
    if identity in active:
        raise ValueError("MCP arguments must not contain recursive containers")
    active.add(identity)
    _charge_mcp_argument_budget(budget, encoded_bytes=2)
    try:
        if type(value) is list:
            snapshot_items: list[object] = []
            for index, item in enumerate(value):
                if index:
                    _charge_mcp_argument_budget(budget, encoded_bytes=1)
                snapshot_items.append(
                    _snapshot_mcp_json(
                        item,
                        path=f"{path}[{index}]",
                        depth=depth + 1,
                        active=active,
                        budget=budget,
                    )
                )
            return snapshot_items

        snapshot: dict[str, object] = {}
        for index, (raw_key, item) in enumerate(value.items()):
            key_field = f"{path} key {index}"
            if type(raw_key) is not str:
                raise TypeError(f"{key_field} must be an exact string")
            if len(raw_key) > budget[1]:
                raise ValueError("MCP arguments exceed safe UTF-8 byte limit")
            key = _exact_utf8_text(raw_key, field=key_field)
            key_bytes = _mcp_scalar_bytes(key) + 1 + (1 if index else 0)
            _charge_mcp_argument_budget(budget, encoded_bytes=key_bytes)
            snapshot[key] = _snapshot_mcp_json(
                item,
                path=f"{path}[{index}]",
                depth=depth + 1,
                active=active,
                budget=budget,
            )
        return snapshot
    finally:
        active.remove(identity)


def _snapshot_mcp_arguments(arguments: object) -> dict[str, object]:
    if type(arguments) is not dict:
        raise TypeError("arguments must be an exact dict")
    snapshot = _snapshot_mcp_json(arguments, path="arguments")
    if type(snapshot) is not dict:
        raise TypeError("arguments must be an exact dict")
    return snapshot


def _snapshot_tool_call(call: ToolCall) -> ToolCall:
    if type(call) is not ToolCall:
        raise TypeError("call must be an exact ToolCall")
    try:
        call_id = _exact_utf8_text(call.call_id, field="call_id")
        tool_id = _exact_utf8_text(call.tool_id, field="tool_id", non_empty=True)
        arguments = _snapshot_mcp_arguments(call.arguments)
        approved = call.approved
        task_id = call.task_id
        authorization = call.authorization
    except AttributeError as exc:
        raise ValueError("call is incomplete") from exc

    if type(approved) is not bool:
        raise TypeError("approved must be an exact bool")
    if task_id is not None:
        task_id = _exact_utf8_text(task_id, field="task_id")

    return ToolCall(
        call_id=call_id,
        tool_id=tool_id,
        arguments=arguments,
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
        specs: list[ToolSpec] = []
        seen_tool_ids: set[str] = set()
        seen_cursors: set[str] = set()
        cursor: str | None = None
        pages = 0
        discovery_bytes = 0

        async with Client(self._target) as client:
            while True:
                pages += 1
                if pages > _MAX_MCP_LIST_PAGES:
                    raise ValueError("MCP tools pagination exceeded safe page limit")
                result = await client.list_tools(cursor=cursor)
                try:
                    page_tools = result.tools
                    next_cursor = result.next_cursor
                except AttributeError as exc:
                    raise ValueError("MCP tools page is incomplete") from exc
                if type(page_tools) is not list:
                    raise TypeError("MCP tools page must contain an exact list")
                if len(page_tools) > _MAX_MCP_LIST_TOOLS - len(specs):
                    raise ValueError("MCP tools discovery exceeds safe tool limit")

                for tool in page_tools:
                    tool_name = _exact_mcp_tool_name(tool.name)
                    tool_id = f"mcp:{self._server_id}:{tool_name}"
                    if tool_id in seen_tool_ids:
                        raise ValueError(f"duplicate MCP tool id: {tool_id}")
                    seen_tool_ids.add(tool_id)
                    try:
                        raw_description = tool.description
                        raw_title = tool.title
                        raw_schema = tool.input_schema
                    except AttributeError as exc:
                        raise ValueError("MCP tool metadata is incomplete") from exc

                    description: str | None = None
                    if raw_description is not None:
                        if type(raw_description) is not str:
                            raise TypeError(
                                "MCP tool description must be an exact string"
                            )
                        if len(raw_description) > _MAX_MCP_DISCOVERY_BYTES:
                            raise ValueError(
                                "MCP discovery exceeds safe metadata limit"
                            )
                        description = _exact_utf8_text(
                            raw_description, field="MCP tool description"
                        )
                    if not description and raw_title is not None:
                        if type(raw_title) is not str:
                            raise TypeError("MCP tool title must be an exact string")
                        if len(raw_title) > _MAX_MCP_DISCOVERY_BYTES:
                            raise ValueError(
                                "MCP discovery exceeds safe metadata limit"
                            )
                        description = _exact_utf8_text(
                            raw_title, field="MCP tool title"
                        )
                    if not description:
                        description = tool_name

                    if raw_schema is None:
                        schema: dict[str, object] = {}
                    else:
                        schema = _snapshot_mcp_arguments(raw_schema)
                    discovery_bytes += (
                        len(tool_id.encode("utf-8"))
                        + len(description.encode("utf-8"))
                        + len(
                            json.dumps(
                                schema,
                                ensure_ascii=False,
                                allow_nan=False,
                                separators=(",", ":"),
                            ).encode("utf-8")
                        )
                    )
                    if discovery_bytes > _MAX_MCP_DISCOVERY_BYTES:
                        raise ValueError("MCP discovery exceeds safe metadata limit")
                    specs.append(
                        ToolSpec(
                            tool_id=tool_id,
                            description=description,
                            risk=self._default_risk,
                            input_schema=schema,
                        )
                    )

                if next_cursor is None:
                    return tuple(specs)
                cursor = _exact_mcp_cursor(next_cursor)
                if cursor in seen_cursors:
                    raise ValueError("MCP tools pagination cursor repeated")
                seen_cursors.add(cursor)

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
        tool_name = _exact_mcp_tool_name(tool_name)
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
