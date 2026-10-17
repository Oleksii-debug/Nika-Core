"""Plan 2 §1: Agent Builder draft ingress must be bounded before Model Gateway effects."""

from __future__ import annotations

import asyncio

import pytest

from nika_core.builder.drafting import AgentDraftService
from nika_core.builder.spec import AgentDefinition


class _Gateway:
    def __init__(self) -> None:
        self.requests: list[object] = []

    async def complete(self, request: object) -> object:
        self.requests.append(request)
        definition = AgentDefinition(
            agent_id="fixture.agent",
            name="Fixture",
            goal="Read data",
            instructions="Use approved tools only.",
        )
        return type("_ModelOutput", (), {"text": definition.export_json()})()


class _HostileString(str):
    def strip(self, *args: object) -> str:
        raise AssertionError("untrusted string subclass was invoked")


@pytest.mark.parametrize(
    ("request", "error_type", "message"),
    (
        (None, TypeError, "must be text"),
        (42, TypeError, "must be text"),
        (b"create an agent", TypeError, "must be text"),
        (_HostileString("create an agent"), TypeError, "must be text"),
        ("", ValueError, "must not be empty"),
        (" \t\n ", ValueError, "must not be empty"),
        ("a" * (64 * 1024 + 1), ValueError, "byte limit"),
        ("é" * (32 * 1024 + 1), ValueError, "byte limit"),
        (chr(0xD800), ValueError, "invalid Unicode"),
    ),
)
def test_invalid_request_never_calls_model_gateway(
    request: object, error_type: type[Exception], message: str,
) -> None:
    gateway = _Gateway()
    with pytest.raises(error_type, match=message):
        asyncio.run(AgentDraftService(gateway).draft(request))  # type: ignore[arg-type]
    assert gateway.requests == []


def test_exact_byte_budget_is_admitted_without_changing_schema_authority() -> None:
    gateway = _Gateway()
    definition = asyncio.run(AgentDraftService(gateway).draft("a" * (64 * 1024)))
    assert definition.agent_id == "fixture.agent"
    assert len(gateway.requests) == 1


def test_valid_request_is_trimmed_once_for_existing_model_gateway_path() -> None:
    gateway = _Gateway()
    definition = asyncio.run(AgentDraftService(gateway).draft("  Create an agent  "))
    assert definition.name == "Fixture"
    assert len(gateway.requests) == 1
    request = gateway.requests[0]
    assert "User request:\nCreate an agent" in request.messages[1].content
