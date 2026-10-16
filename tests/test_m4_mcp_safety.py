from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Self

import pytest
from mcp.server import MCPServer

from nika_core.mcp_boundary import MCPClientAdapter, MCPServerConfig
from nika_core.tools import ToolCall, ToolResult, ToolRisk


class _BehavioralDict(dict[str, object]):
    def __init__(self, events: list[str]) -> None:
        super().__init__({"value": "original"})
        self.events = events

    def items(self):
        self.events.append("items")
        return super().items()


class _BehavioralToolId(str):
    def __new__(cls, value: str, events: list[str]) -> Self:
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


def test_mcp_routing_segments_reject_delimiter_collisions() -> None:
    called: list[dict[str, object]] = []

    with pytest.raises(ValueError, match="server_id must not contain ':'"):
        MCPServerConfig(server_id="team:prod", target=_server(called))

    adapter = MCPClientAdapter(MCPServerConfig(server_id="team", target=_server(called)))
    with pytest.raises(ValueError, match="MCP tool name must not contain ':'"):
        asyncio.run(
            adapter.call(
                ToolCall(
                    call_id="mcp-delimiter-1",
                    tool_id="mcp:team:group:publish",
                    arguments={"value": "blocked"},
                )
            )
        )

    assert called == []


def test_mcp_server_identity_rejects_edge_whitespace() -> None:
    with pytest.raises(ValueError, match="server_id must not contain edge whitespace"):
        MCPServerConfig(server_id=" safety", target=object())


def test_incomplete_exact_tool_call_fails_before_routing() -> None:
    adapter = MCPClientAdapter(MCPServerConfig(server_id="safety", target=object()))
    incomplete = object.__new__(ToolCall)

    with pytest.raises(ValueError, match="call is incomplete"):
        asyncio.run(adapter.call(incomplete))


def test_incomplete_exact_config_fails_before_target_use() -> None:
    incomplete = object.__new__(MCPServerConfig)

    with pytest.raises(ValueError, match="config is incomplete"):
        MCPClientAdapter(incomplete)


def test_wrong_server_rejection_never_opens_transport() -> None:
    adapter = MCPClientAdapter(
        MCPServerConfig(
            server_id="safety",
            target=object(),
            default_risk=ToolRisk.READ_ONLY,
        )
    )

    result = asyncio.run(
        adapter.call(
            ToolCall(
                call_id="mcp-wrong-server-1",
                tool_id="mcp:other:publish",
                arguments={"value": "blocked"},
            )
        )
    )

    assert result.error == "wrong MCP server"


def _list_tools_client(
    pages: dict[str | None, tuple[list[SimpleNamespace], str | None]],
    seen_cursors: list[str | None] | None = None,
) -> type:
    class FakeClient:
        def __init__(self, _target: object) -> None:
            pass

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(
            self,
            _exc_type: object,
            _exc: object,
            _tb: object,
        ) -> None:
            pass

        async def list_tools(
            self,
            *,
            cursor: str | None = None,
        ) -> SimpleNamespace:
            if seen_cursors is not None:
                seen_cursors.append(cursor)
            tools, next_cursor = pages[cursor]
            return SimpleNamespace(tools=tools, next_cursor=next_cursor)

    return FakeClient


def _listed_tool(name: str) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        description=None,
        title=None,
        input_schema={},
    )


def test_mcp_routing_segments_are_bounded() -> None:
    with pytest.raises(ValueError, match="server_id must contain at most 128 characters"):
        MCPServerConfig(server_id="s" * 129, target=object())

    adapter = MCPClientAdapter(
        MCPServerConfig(
            server_id="safety",
            target=object(),
            default_risk=ToolRisk.READ_ONLY,
        )
    )
    with pytest.raises(ValueError, match="MCP tool name must contain at most 128 characters"):
        asyncio.run(
            adapter.call(
                ToolCall(
                    call_id="mcp-overlong-tool-1",
                    tool_id=f"mcp:safety:{'x' * 129}",
                    arguments={},
                )
            )
        )


@pytest.mark.parametrize(
    "tool_name",
    ["publish name", "publish\nshadow", "públish", "publish$"],
)
def test_direct_call_rejects_noncanonical_mcp_tool_names(tool_name: str) -> None:
    adapter = MCPClientAdapter(
        MCPServerConfig(
            server_id="safety",
            target=object(),
            default_risk=ToolRisk.READ_ONLY,
        )
    )

    with pytest.raises(ValueError, match="MCP tool name must use only"):
        asyncio.run(
            adapter.call(
                ToolCall(
                    call_id="mcp-invalid-tool-name-1",
                    tool_id=f"mcp:safety:{tool_name}",
                    arguments={},
                )
            )
        )


@pytest.mark.parametrize(
    "tool_name",
    ["publish name", "publish\nshadow", "públish", "publish$"],
)
def test_list_tools_rejects_noncanonical_mcp_tool_names(
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
) -> None:
    fake_client = _list_tools_client(
        {None: ([_listed_tool(tool_name)], None)}
    )
    monkeypatch.setattr("nika_core.mcp_boundary.Client", fake_client)
    adapter = MCPClientAdapter(
        MCPServerConfig(
            server_id="safety",
            target=object(),
            default_risk=ToolRisk.READ_ONLY,
        )
    )

    with pytest.raises(ValueError, match="MCP tool name must use only"):
        asyncio.run(adapter.list_tools())


def test_list_tools_rejects_duplicate_canonical_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _list_tools_client(
        {
            None: ([_listed_tool("publish")], "page-2"),
            "page-2": ([_listed_tool("publish")], None),
        }
    )
    monkeypatch.setattr("nika_core.mcp_boundary.Client", fake_client)
    adapter = MCPClientAdapter(
        MCPServerConfig(
            server_id="safety",
            target=object(),
            default_risk=ToolRisk.READ_ONLY,
        )
    )

    with pytest.raises(ValueError, match="duplicate MCP tool id: mcp:safety:publish"):
        asyncio.run(adapter.list_tools())


def test_list_tools_accepts_128_character_tool_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool_name = "x" * 128
    fake_client = _list_tools_client(
        {None: ([_listed_tool(tool_name)], None)}
    )
    monkeypatch.setattr("nika_core.mcp_boundary.Client", fake_client)
    adapter = MCPClientAdapter(
        MCPServerConfig(
            server_id="safety",
            target=object(),
            default_risk=ToolRisk.READ_ONLY,
        )
    )

    specs = asyncio.run(adapter.list_tools())

    assert [spec.tool_id for spec in specs] == [f"mcp:safety:{tool_name}"]



def test_list_tools_discovers_every_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_cursors: list[str | None] = []
    fake_client = _list_tools_client(
        {
            None: ([_listed_tool("first")], "page-2"),
            "page-2": ([_listed_tool("second")], None),
        },
        seen_cursors,
    )
    monkeypatch.setattr("nika_core.mcp_boundary.Client", fake_client)
    adapter = MCPClientAdapter(
        MCPServerConfig(
            server_id="safety",
            target=object(),
            default_risk=ToolRisk.READ_ONLY,
        )
    )

    specs = asyncio.run(adapter.list_tools())

    assert [spec.tool_id for spec in specs] == [
        "mcp:safety:first",
        "mcp:safety:second",
    ]
    assert seen_cursors == [None, "page-2"]


def test_list_tools_rejects_repeated_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _list_tools_client(
        {
            None: ([], "loop"),
            "loop": ([], "loop"),
        }
    )
    monkeypatch.setattr("nika_core.mcp_boundary.Client", fake_client)
    adapter = MCPClientAdapter(
        MCPServerConfig(
            server_id="safety",
            target=object(),
            default_risk=ToolRisk.READ_ONLY,
        )
    )

    with pytest.raises(ValueError, match="MCP tools pagination cursor repeated"):
        asyncio.run(adapter.list_tools())


def test_list_tools_rejects_oversized_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _list_tools_client(
        {None: ([], "x" * 4097)}
    )
    monkeypatch.setattr("nika_core.mcp_boundary.Client", fake_client)
    adapter = MCPClientAdapter(
        MCPServerConfig(
            server_id="safety",
            target=object(),
            default_risk=ToolRisk.READ_ONLY,
        )
    )

    with pytest.raises(ValueError, match="MCP next cursor must contain at most 4096 UTF-8 bytes"):
        asyncio.run(adapter.list_tools())


def test_list_tools_bounds_unique_cursor_pagination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_cursors: list[str | None] = []
    fake_client = _list_tools_client(
        {
            None: ([], "page-2"),
            "page-2": ([], "page-3"),
            "page-3": ([], None),
        },
        seen_cursors,
    )
    monkeypatch.setattr("nika_core.mcp_boundary.Client", fake_client)
    monkeypatch.setattr("nika_core.mcp_boundary._MAX_MCP_LIST_PAGES", 2)
    adapter = MCPClientAdapter(
        MCPServerConfig(
            server_id="safety",
            target=object(),
            default_risk=ToolRisk.READ_ONLY,
        )
    )

    with pytest.raises(ValueError, match="MCP tools pagination exceeded safe page limit"):
        asyncio.run(adapter.list_tools())

    assert seen_cursors == [None, "page-2"]


def test_mcp_call_deep_snapshots_nested_arguments_before_approval_wait() -> None:
    original_items = ["original"]
    original_arguments: dict[str, object] = {
        "payload": {"items": original_items},
    }
    seen_arguments: list[dict[str, object]] = []

    async def exercise() -> ToolResult:
        started = asyncio.Event()
        release = asyncio.Event()

        async def approval(_spec: object, call: ToolCall) -> None:
            started.set()
            await release.wait()
            seen_arguments.append(call.arguments)

        adapter = MCPClientAdapter(
            MCPServerConfig(server_id="safety", target=object()),
            approval_policy=approval,
        )
        task = asyncio.create_task(
            adapter.call(
                ToolCall(
                    call_id="mcp-nested-snapshot-1",
                    tool_id="mcp:safety:publish",
                    arguments=original_arguments,
                )
            )
        )
        await started.wait()
        original_items[0] = "mutated"
        original_items.append("late")
        release.set()
        return await task

    result = asyncio.run(exercise())

    assert result.error == "approval required"
    assert seen_arguments == [{"payload": {"items": ["original"]}}]
    assert original_arguments == {"payload": {"items": ["mutated", "late"]}}


def test_mcp_call_rejects_behavioral_nested_argument_before_transport() -> None:
    events: list[str] = []
    adapter = MCPClientAdapter(
        MCPServerConfig(
            server_id="safety",
            target=object(),
            default_risk=ToolRisk.READ_ONLY,
        )
    )

    with pytest.raises(TypeError, match="arguments"):
        asyncio.run(
            adapter.call(
                ToolCall(
                    call_id="mcp-behavioral-argument-1",
                    tool_id="mcp:safety:publish",
                    arguments={"nested": _BehavioralDict(events)},
                )
            )
        )

    assert events == []


def test_mcp_call_rejects_recursive_nested_argument_before_transport() -> None:
    recursive: list[object] = []
    recursive.append(recursive)
    adapter = MCPClientAdapter(
        MCPServerConfig(
            server_id="safety",
            target=object(),
            default_risk=ToolRisk.READ_ONLY,
        )
    )

    with pytest.raises(ValueError, match="recursive containers"):
        asyncio.run(
            adapter.call(
                ToolCall(
                    call_id="mcp-recursive-argument-1",
                    tool_id="mcp:safety:publish",
                    arguments={"nested": recursive},
                )
            )
        )

