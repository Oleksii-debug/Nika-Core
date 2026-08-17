from __future__ import annotations

import asyncio
from dataclasses import dataclass

from nika_core.runtime.contracts import (
    RuntimeCapability,
    RuntimeOutcome,
    RuntimeRequest,
    RuntimeResumeMode,
    RuntimeResumeRequest,
)
from nika_core.runtime.langgraph_runtime import LangGraphRuntime


@dataclass
class _Interrupt:
    value: object


class _FakeGraph:
    def __init__(self) -> None:
        self.calls: list[tuple[object, object]] = []
        self.responses: list[object] = []

    async def ainvoke(self, graph_input: object, *, config: object) -> object:
        self.calls.append((graph_input, config))
        if not self.responses:
            raise RuntimeError("No fake response configured")
        return self.responses.pop(0)


def test_langgraph_adapter_normalizes_completed_result() -> None:
    graph = _FakeGraph()
    graph.responses.append({"answer": "done"})
    runtime = LangGraphRuntime(graph)
    result = asyncio.run(runtime.run(RuntimeRequest("task-1", "thread-1", {"goal": "x"})))
    assert result.outcome == RuntimeOutcome.COMPLETED
    assert result.output == {"answer": "done"}
    assert graph.calls[0] == (
        {"goal": "x"},
        {"configurable": {"thread_id": "thread-1"}},
    )


def test_langgraph_adapter_normalizes_interrupt_and_resume() -> None:
    graph = _FakeGraph()
    graph.responses.extend(
        [
            {"prepared": True, "__interrupt__": (_Interrupt({"question": "approve?"}),)},
            {"prepared": True, "approved": True},
        ]
    )
    runtime = LangGraphRuntime(graph, resume_command_factory=lambda value: ("resume", value))

    waiting = asyncio.run(runtime.run(RuntimeRequest("task-2", "thread-2")))
    assert waiting.outcome == RuntimeOutcome.WAITING_APPROVAL
    assert waiting.resume_token == "thread-2"
    assert waiting.output == {"prepared": True}
    assert waiting.events[0].payload == {"value": {"question": "approve?"}}

    resumed = asyncio.run(
        runtime.resume(
            RuntimeResumeRequest(
                task_id="task-2",
                thread_id="thread-2",
                resume_token="thread-2",
                mode=RuntimeResumeMode.APPROVAL,
                value=True,
            )
        )
    )
    assert resumed.outcome == RuntimeOutcome.COMPLETED
    assert resumed.output["approved"] is True
    assert graph.calls[1][0] == ("resume", True)


def test_langgraph_adapter_continue_resume_uses_none_input() -> None:
    graph = _FakeGraph()
    graph.responses.append({"resumed": True})
    runtime = LangGraphRuntime(graph)
    result = asyncio.run(
        runtime.resume(
            RuntimeResumeRequest(
                task_id="task-3",
                thread_id="thread-3",
                resume_token="thread-3",
                mode=RuntimeResumeMode.CONTINUE,
            )
        )
    )
    assert result.outcome == RuntimeOutcome.COMPLETED
    assert graph.calls[0][0] is None


def test_langgraph_adapter_does_not_claim_unproven_cancellation() -> None:
    runtime = LangGraphRuntime(_FakeGraph())
    assert RuntimeCapability.CANCELLATION not in runtime.capabilities
    assert asyncio.run(runtime.cancel(task_id="task", thread_id="thread")) is False


def test_langgraph_adapter_converts_runtime_exception_to_typed_failure() -> None:
    graph = _FakeGraph()
    runtime = LangGraphRuntime(graph)
    result = asyncio.run(runtime.run(RuntimeRequest("task-4", "thread-4")))
    assert result.outcome == RuntimeOutcome.FAILED
    assert "No fake response configured" in (result.error or "")
