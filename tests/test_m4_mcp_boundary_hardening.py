from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from mcp.server import MCPServer

import nika_core.mcp_boundary as mcp_boundary
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


@pytest.mark.parametrize(
    ("field_name", "field_value"),
    [("max_tool_pages", 0), ("max_tools", 0)],
)
def test_mcp_config_rejects_non_positive_catalog_bounds(
    field_name: str,
    field_value: int,
) -> None:
    kwargs = {field_name: field_value}
    with pytest.raises(ValueError, match=field_name):
        MCPServerConfig(server_id="catalog-bounds", target=object(), **kwargs)


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


def test_mcp_discovery_collects_all_paginated_tools(monkeypatch: pytest.MonkeyPatch) -> None:
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
        MCPServerConfig(server_id="paged", target=object(), timeout_seconds=2.0)
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
            return SimpleNamespace(tools=[_fake_tool("same")], next_cursor=next_cursor)

    monkeypatch.setattr(mcp_boundary, "Client", FakeClient)
    adapter = MCPClientAdapter(MCPServerConfig(server_id="duplicate", target=object()))

    with pytest.raises(ValueError, match="duplicate MCP tool id"):
        asyncio.run(adapter.list_tools())


def test_mcp_discovery_enforces_tool_catalog_bound(monkeypatch: pytest.MonkeyPatch) -> None:
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
                tools=[_fake_tool("one"), _fake_tool("two")],
                next_cursor=None,
            )

    monkeypatch.setattr(mcp_boundary, "Client", FakeClient)
    adapter = MCPClientAdapter(
        MCPServerConfig(server_id="bounded-tools", target=object(), max_tools=1)
    )

    with pytest.raises(ValueError, match="max_tools"):
        asyncio.run(adapter.list_tools())


def test_mcp_discovery_enforces_page_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeClient:
        def __init__(self, _target: object, *, read_timeout_seconds: float) -> None:
            del read_timeout_seconds

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def list_tools(self, *, cursor: str | None) -> object:
            del cursor
            return SimpleNamespace(tools=[], next_cursor="page-2")

    monkeypatch.setattr(mcp_boundary, "Client", FakeClient)
    adapter = MCPClientAdapter(
        MCPServerConfig(server_id="bounded-pages", target=object(), max_tool_pages=1)
    )

    with pytest.raises(ValueError, match="max_tool_pages"):
        asyncio.run(adapter.list_tools())


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


def test_direct_mcp_call_normalizes_transport_failure_without_raw_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "transport-secret-canary"

    class BrokenClient:
        def __init__(self, _target: object, *, read_timeout_seconds: float) -> None:
            del read_timeout_seconds

        async def __aenter__(self) -> BrokenClient:
            raise RuntimeError(secret)

        async def __aexit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(mcp_boundary, "Client", BrokenClient)
    adapter = MCPClientAdapter(MCPServerConfig(server_id="broken", target=object()))

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


def _fake_tool(name: str) -> object:
    return SimpleNamespace(
        name=name,
        description=f"Tool {name}",
        title=None,
        input_schema={"type": "object"},
    )
