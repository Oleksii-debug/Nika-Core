from __future__ import annotations

import asyncio

import pytest

from nika_core.runtime.contracts import (
    AgentRuntimePort,
    RuntimeCapability,
    RuntimeOutcome,
    RuntimeRequest,
    RuntimeResult,
    RuntimeResumeRequest,
)
from nika_core.runtime.reference import ReferenceRuntime
from nika_core.runtime.registry import RuntimeRegistry
from nika_core.runtime.selection import (
    LANGGRAPH_2026_08,
    MICROSOFT_AGENT_FRAMEWORK_2026_08,
    choose_primary,
)


def test_runtime_request_fails_closed_for_invalid_limits() -> None:
    with pytest.raises(ValueError):
        RuntimeRequest(task_id="task", thread_id="thread", max_steps=0)
    with pytest.raises(ValueError):
        RuntimeRequest(task_id="", thread_id="thread")
    with pytest.raises(ValueError):
        RuntimeResumeRequest(
            task_id="task",
            thread_id="thread",
            resume_token="token",
            max_steps=0,
        )


def test_runtime_result_requires_resume_token_for_approval() -> None:
    with pytest.raises(ValueError):
        RuntimeResult(outcome=RuntimeOutcome.WAITING_APPROVAL)
    with pytest.raises(ValueError):
        RuntimeResult(outcome=RuntimeOutcome.FAILED)


def test_reference_runtime_satisfies_port_and_keeps_framework_types_out() -> None:
    runtime = ReferenceRuntime()
    assert isinstance(runtime, AgentRuntimePort)
    result = asyncio.run(
        runtime.run(RuntimeRequest("task-1", "thread-1", {"goal": "verify"}, max_steps=8))
    )
    assert result.outcome == RuntimeOutcome.COMPLETED
    assert result.output == {"echo": {"goal": "verify"}, "max_steps": 8}
    assert result.events[0].event_type == "reference.completed"


def test_runtime_registry_selects_only_truthful_capabilities() -> None:
    registry = RuntimeRegistry()
    reference = ReferenceRuntime()
    registry.register(reference)
    assert registry.get("reference") is reference
    assert registry.select({RuntimeCapability.DETERMINISTIC_NO_LLM}) is reference
    with pytest.raises(LookupError):
        registry.select({RuntimeCapability.DURABLE_RESUME})
    with pytest.raises(LookupError):
        registry.select({RuntimeCapability.CANCELLATION})
    with pytest.raises(ValueError):
        registry.register(reference)


def test_selection_evidence_prefers_langgraph_for_current_local_desktop_target() -> None:
    selected = choose_primary((LANGGRAPH_2026_08, MICROSOFT_AGENT_FRAMEWORK_2026_08))
    assert selected.runtime_id == "langgraph"
    assert LANGGRAPH_2026_08.score > MICROSOFT_AGENT_FRAMEWORK_2026_08.score
