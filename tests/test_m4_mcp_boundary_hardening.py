from __future__ import annotations

import asyncio
import traceback
from types import SimpleNamespace

import pytest
from mcp.server import MCPServer

import nika_core.mcp_boundary as mcp_boundary
from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.mcp_boundary import (
    MCPBoundaryError,
    MCPClientAdapter,
    MCPServerConfig,
)
from nika_core.runtime.idempotency import IdempotencyLedger
from nika_core.tools import (
    ToolAuthorization,
    ToolCall,
    ToolEffectGuard,
    ToolExecutor,
    ToolRisk,
    ToolSpec,
    tool_arguments_fingerprint,
)


@pytest.mark.parametrize("risk", [ToolRisk.READ_ONLY, ToolRisk.LOCAL_WRITE])
def test_untrusted_mcp_config_rejects_risk_downgrade(risk: ToolRisk) -> None:
    with pytest.raises(ValueError, match="trusted connector policy"):
        MCPServerConfig(server_id="untrusted", target=object(), default_risk=risk)


@pytest.mark.parametrize(
    "timeout_seconds",
    [
        0.0,
        -1.0,
        float("nan"),
        float("inf"),
        float("-inf"),
        True,
        "1",
        10**10000,
    ],
)
def test_mcp_config_rejects_invalid_deadline(timeout_seconds: object) -> None:
    with pytest.raises(ValueError, match="timeout_seconds"):
        MCPServerConfig(
            server_id="deadline",
            target=object(),
            timeout_seconds=timeout_seconds,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    ("field_name", "field_value"),
    [
        ("max_tool_pages", 0),
        ("max_tool_pages", -1),
        ("max_tool_pages", True),
        ("max_tool_pages", 1.5),
        ("max_tool_pages", "2"),
        ("max_tools", 0),
        ("max_tools", True),
        ("max_tools", 2.5),
    ],
)
def test_mcp_config_rejects_non_integral_catalog_bounds(
    field_name: str,
    field_value: object,
) -> None:
    kwargs = {field_name: field_value}
    with pytest.raises(ValueError, match=field_name):
        MCPServerConfig(
            server_id="catalog-bounds",
            target=object(),
            **kwargs,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    "server_id",
    ["", " leading", "trailing ", "inner space", "nested:server", "line\nbreak"],
)
def test_mcp_config_rejects_ambiguous_server_namespace(server_id: str) -> None:
    with pytest.raises(ValueError, match="server_id"):
        MCPServerConfig(server_id=server_id, target=object())


def test_mcp_config_repr_does_not_expose_transport_target() -> None:
    secret = "oauth-token=nika-mcp-secret-canary"

    class SecretTarget:
        def __repr__(self) -> str:
            return secret

    config = MCPServerConfig(server_id="secret-target", target=SecretTarget())

    rendered = repr(config)

    assert secret not in rendered
    assert "target=" not in rendered


def test_discovered_mcp_tool_inherits_exact_boundary_contract() -> None:
    server = MCPServer("nika-mcp-discovery-contract")

    @server.tool()
    async def echo(value: str) -> dict[str, str]:
        """Echo a value."""
        return {"value": value}

    adapter = MCPClientAdapter(
        MCPServerConfig(
            server_id="discovery-contract",
            target=server,
            timeout_seconds=1.25,
        )
    )

    specs = asyncio.run(adapter.list_tools())

    assert len(specs) == 1
    assert specs[0].tool_id == "mcp:discovery-contract:echo"
    assert specs[0].risk is ToolRisk.EXTERNAL_SIDE_EFFECT
    assert specs[0].timeout_seconds == 1.25
    assert specs[0].input_schema["type"] == "object"
    assert "value" in specs[0].input_schema["properties"]



@pytest.mark.parametrize(
    "tool_name",
    [
        "",
        "white space",
        "colon:name",
        "slash/name",
        "line\nbreak",
        "control\x01name",
        "a" * 129,
    ],
)
def test_mcp_discovery_rejects_untrusted_tool_name_identity(
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
) -> None:
    class FakeClient:
        def __init__(self, _target: object, *, read_timeout_seconds: float) -> None:
            del read_timeout_seconds

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def list_tools(self, *, cursor: str | None) -> object:
            del cursor
            return SimpleNamespace(
                tools=[_fake_tool(tool_name)],
                next_cursor=None,
            )

    monkeypatch.setattr(mcp_boundary, "Client", FakeClient)
    adapter = MCPClientAdapter(
        MCPServerConfig(server_id="tool-name", target=object())
    )

    with pytest.raises(MCPBoundaryError, match="invalid MCP tool name"):
        asyncio.run(adapter.list_tools())


@pytest.mark.parametrize("tool_name", ["alpha", "Alpha_2", "group.tool-v1"])
def test_mcp_discovery_accepts_protocol_compatible_tool_names(
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
) -> None:
    class FakeClient:
        def __init__(self, _target: object, *, read_timeout_seconds: float) -> None:
            del read_timeout_seconds

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def list_tools(self, *, cursor: str | None) -> object:
            del cursor
            return SimpleNamespace(
                tools=[_fake_tool(tool_name)],
                next_cursor=None,
            )

    monkeypatch.setattr(mcp_boundary, "Client", FakeClient)
    adapter = MCPClientAdapter(
        MCPServerConfig(server_id="tool-name", target=object())
    )

    specs = asyncio.run(adapter.list_tools())

    assert [spec.tool_id for spec in specs] == [f"mcp:tool-name:{tool_name}"]


def test_mcp_discovery_collects_all_paginated_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cursors: list[str | None] = []

    class FakeClient:
        def __init__(self, _target: object, *, read_timeout_seconds: float) -> None:
            assert read_timeout_seconds == 2.0

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def list_tools(self, *, cursor: str | None) -> object:
            cursors.append(cursor)
            if cursor is None:
                return SimpleNamespace(
                    tools=[_fake_tool("alpha")],
                    next_cursor="page-2",
                )
            assert cursor == "page-2"
            return SimpleNamespace(
                tools=[_fake_tool("beta")],
                next_cursor=None,
            )

    monkeypatch.setattr(mcp_boundary, "Client", FakeClient)
    adapter = MCPClientAdapter(
        MCPServerConfig(
            server_id="paged",
            target=object(),
            timeout_seconds=2.0,
        )
    )

    specs = asyncio.run(adapter.list_tools())

    assert [spec.tool_id for spec in specs] == ["mcp:paged:alpha", "mcp:paged:beta"]
    assert cursors == [None, "page-2"]


def test_mcp_discovery_rejects_repeated_pagination_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeClient:
        def __init__(self, _target: object, *, read_timeout_seconds: float) -> None:
            del read_timeout_seconds

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def list_tools(self, *, cursor: str | None) -> object:
            del cursor
            return SimpleNamespace(tools=[], next_cursor="repeat")

    monkeypatch.setattr(mcp_boundary, "Client", FakeClient)
    adapter = MCPClientAdapter(MCPServerConfig(server_id="loop", target=object()))

    with pytest.raises(ValueError, match="repeated pagination cursor"):
        asyncio.run(adapter.list_tools())


def test_mcp_discovery_rejects_duplicate_tool_ids_across_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeClient:
        def __init__(self, _target: object, *, read_timeout_seconds: float) -> None:
            del read_timeout_seconds

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def list_tools(self, *, cursor: str | None) -> object:
            next_cursor = "page-2" if cursor is None else None
            return SimpleNamespace(
                tools=[_fake_tool("same")],
                next_cursor=next_cursor,
            )

    monkeypatch.setattr(mcp_boundary, "Client", FakeClient)
    adapter = MCPClientAdapter(
        MCPServerConfig(server_id="duplicate", target=object())
    )

    with pytest.raises(ValueError, match="duplicate MCP tool id"):
        asyncio.run(adapter.list_tools())


def test_mcp_discovery_enforces_tool_and_page_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ToolBoundClient:
        def __init__(self, _target: object, *, read_timeout_seconds: float) -> None:
            del read_timeout_seconds

        async def __aenter__(self) -> ToolBoundClient:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def list_tools(self, *, cursor: str | None) -> object:
            del cursor
            return SimpleNamespace(
                tools=[_fake_tool("one"), _fake_tool("two")],
                next_cursor=None,
            )

    monkeypatch.setattr(mcp_boundary, "Client", ToolBoundClient)
    tool_bounded = MCPClientAdapter(
        MCPServerConfig(
            server_id="bounded-tools",
            target=object(),
            max_tools=1,
        )
    )
    with pytest.raises(ValueError, match="max_tools"):
        asyncio.run(tool_bounded.list_tools())

    class PageBoundClient:
        def __init__(self, _target: object, *, read_timeout_seconds: float) -> None:
            del read_timeout_seconds

        async def __aenter__(self) -> PageBoundClient:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def list_tools(self, *, cursor: str | None) -> object:
            del cursor
            return SimpleNamespace(tools=[], next_cursor="page-2")

    monkeypatch.setattr(mcp_boundary, "Client", PageBoundClient)
    page_bounded = MCPClientAdapter(
        MCPServerConfig(
            server_id="bounded-pages",
            target=object(),
            max_tool_pages=1,
        )
    )
    with pytest.raises(ValueError, match="max_tool_pages"):
        asyncio.run(page_bounded.list_tools())


def test_register_tools_binds_exact_discovery_to_canonical_executor(
    tmp_path,
) -> None:
    server = MCPServer("nika-mcp-canonical-registration")
    handler_calls = 0

    @server.tool()
    async def add(left: int, right: int) -> dict[str, int]:
        """Add two integers."""
        nonlocal handler_calls
        handler_calls += 1
        return {"sum": left + right}

    store = SQLiteStore(tmp_path / "mcp-registration.db")
    store.initialize()
    task_id = TaskQueue(store).create(
        workspace_id="mcp",
        agent_id="worker3",
    ).task_id

    approval_specs: list[ToolSpec] = []

    async def approve(spec: ToolSpec, call: ToolCall) -> ToolAuthorization:
        approval_specs.append(spec)
        return ToolAuthorization(
            tool_id=spec.tool_id,
            task_id=call.task_id or "",
            risk=spec.risk,
            arguments_fingerprint=tool_arguments_fingerprint(call.arguments),
            effect_fingerprint="mcp-add-effect-v1",
            approval_fingerprint="mcp-add-approval-v1",
        )

    executor = ToolExecutor(
        approval_policy=approve,
        effect_guard=ToolEffectGuard(IdempotencyLedger(store)),
    )
    adapter = MCPClientAdapter(
        MCPServerConfig(
            server_id="registered",
            target=server,
            timeout_seconds=2.0,
        )
    )

    specs = asyncio.run(adapter.register_tools(executor))
    result = asyncio.run(
        executor.execute(
            ToolCall(
                call_id="registered-add-1",
                tool_id="mcp:registered:add",
                task_id=task_id,
                arguments={"left": 2, "right": 3},
            )
        )
    )

    assert result.ok is True
    assert result.output == {"sum": 5}
    assert handler_calls == 1
    assert executor.specs() == specs
    assert approval_specs == [specs[0]]
    assert specs[0].input_schema["type"] == "object"
    assert set(specs[0].input_schema["properties"]) == {"left", "right"}


def test_register_tools_collision_is_preflighted_without_partial_catalog() -> None:
    server = MCPServer("nika-mcp-collision")

    @server.tool()
    async def alpha() -> dict[str, bool]:
        """Alpha."""
        return {"ok": True}

    @server.tool()
    async def beta() -> dict[str, bool]:
        """Beta."""
        return {"ok": True}

    async def existing_handler(_arguments: dict[str, object]) -> object:
        return None

    executor = ToolExecutor()
    executor.register(
        ToolSpec(
            tool_id="mcp:collision:beta",
            description="existing beta",
            risk=ToolRisk.EXTERNAL_SIDE_EFFECT,
        ),
        existing_handler,
    )
    adapter = MCPClientAdapter(
        MCPServerConfig(server_id="collision", target=server)
    )

    with pytest.raises(ValueError, match="already registered"):
        asyncio.run(adapter.register_tools(executor))

    assert [spec.tool_id for spec in executor.specs()] == ["mcp:collision:beta"]


def test_direct_call_uses_exact_discovered_spec_and_trusted_approval(
    tmp_path,
) -> None:
    server = MCPServer("nika-mcp-direct-exact")

    @server.tool()
    async def echo(value: str) -> dict[str, str]:
        """Echo exact schema."""
        return {"value": value}

    store = SQLiteStore(tmp_path / "mcp-direct.db")
    store.initialize()
    task_id = TaskQueue(store).create(
        workspace_id="mcp",
        agent_id="worker3",
    ).task_id
    observed_schema: dict[str, object] = {}

    async def approve(spec: ToolSpec, call: ToolCall) -> ToolAuthorization:
        observed_schema.update(spec.input_schema)
        return ToolAuthorization(
            tool_id=spec.tool_id,
            task_id=call.task_id or "",
            risk=spec.risk,
            arguments_fingerprint=tool_arguments_fingerprint(call.arguments),
            effect_fingerprint="mcp-echo-effect-v1",
            approval_fingerprint="mcp-echo-approval-v1",
        )

    adapter = MCPClientAdapter(
        MCPServerConfig(server_id="direct", target=server),
        approval_policy=approve,
        effect_guard=ToolEffectGuard(IdempotencyLedger(store)),
    )

    result = asyncio.run(
        adapter.call(
            ToolCall(
                call_id="direct-echo-1",
                tool_id="mcp:direct:echo",
                task_id=task_id,
                arguments={"value": "ok"},
                approved=False,
            )
        )
    )

    assert result.ok is True
    assert result.output == {"value": "ok"}
    assert observed_schema["type"] == "object"
    assert "value" in observed_schema["properties"]


def test_caller_approved_flag_cannot_bypass_missing_trusted_policy() -> None:
    server = MCPServer("nika-mcp-no-bypass")
    called = False

    @server.tool()
    async def publish(value: str) -> dict[str, str]:
        """External effect."""
        nonlocal called
        called = True
        return {"published": value}

    adapter = MCPClientAdapter(
        MCPServerConfig(server_id="no-bypass", target=server)
    )

    result = asyncio.run(
        adapter.call(
            ToolCall(
                call_id="no-bypass-1",
                tool_id="mcp:no-bypass:publish",
                task_id="task-1",
                arguments={"value": "blocked"},
                approved=True,
            )
        )
    )

    assert result.ok is False
    assert result.error == "approval required"
    assert called is False


def test_unknown_mcp_tool_fails_before_remote_invocation() -> None:
    server = MCPServer("nika-mcp-unknown")
    called = False

    @server.tool()
    async def known() -> dict[str, bool]:
        """Known tool."""
        nonlocal called
        called = True
        return {"ok": True}

    adapter = MCPClientAdapter(
        MCPServerConfig(server_id="unknown", target=server)
    )

    result = asyncio.run(
        adapter.call(
            ToolCall(
                call_id="unknown-1",
                tool_id="mcp:unknown:not-present",
                arguments={},
            )
        )
    )

    assert result.ok is False
    assert result.error == "unknown MCP tool"
    assert called is False


def test_discovery_transport_failure_is_normalized_without_raw_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "transport-secret-canary"

    class BrokenClient:
        def __init__(self, _target: object, *, read_timeout_seconds: float) -> None:
            del read_timeout_seconds

        async def __aenter__(self) -> BrokenClient:
            raise ValueError(secret)

        async def __aexit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(mcp_boundary, "Client", BrokenClient)
    adapter = MCPClientAdapter(
        MCPServerConfig(server_id="broken", target=object())
    )

    with pytest.raises(MCPBoundaryError) as caught:
        asyncio.run(adapter.list_tools())

    assert str(caught.value) == "MCP tool discovery failed"
    rendered_traceback = "".join(
        traceback.format_exception(
            type(caught.value),
            caught.value,
            caught.value.__traceback__,
        )
    )
    assert secret not in rendered_traceback
    assert caught.value.__suppress_context__ is True

    result = asyncio.run(
        adapter.call(
            ToolCall(
                call_id="broken-call",
                tool_id="mcp:broken:anything",
                arguments={},
            )
        )
    )
    assert result.error == "MCP tool discovery failed"
    assert secret not in (result.error or "")


def test_direct_mcp_timeout_is_normalized_by_tool_executor(tmp_path) -> None:
    server = MCPServer("nika-mcp-timeout")

    @server.tool()
    async def slow() -> dict[str, bool]:
        """Sleep beyond the configured deadline."""
        await asyncio.sleep(0.2)
        return {"completed": True}

    store = SQLiteStore(tmp_path / "mcp-timeout.db")
    store.initialize()
    task_id = TaskQueue(store).create(
        workspace_id="mcp",
        agent_id="worker3",
    ).task_id

    async def approve(spec: ToolSpec, call: ToolCall) -> ToolAuthorization:
        return ToolAuthorization(
            tool_id=spec.tool_id,
            task_id=call.task_id or "",
            risk=spec.risk,
            arguments_fingerprint=tool_arguments_fingerprint(call.arguments),
            effect_fingerprint="mcp-slow-effect-v1",
            approval_fingerprint="mcp-slow-approval-v1",
        )

    adapter = MCPClientAdapter(
        MCPServerConfig(
            server_id="timeout",
            target=server,
            timeout_seconds=0.01,
        ),
        approval_policy=approve,
        effect_guard=ToolEffectGuard(IdempotencyLedger(store)),
    )

    result = asyncio.run(
        adapter.call(
            ToolCall(
                call_id="timeout-1",
                tool_id="mcp:timeout:slow",
                task_id=task_id,
                arguments={},
            )
        )
    )

    assert result.ok is False
    assert result.error == "tool timed out"


def _fake_tool(name: str) -> object:
    return SimpleNamespace(
        name=name,
        description=f"Tool {name}",
        title=None,
        input_schema={"type": "object"},
    )
