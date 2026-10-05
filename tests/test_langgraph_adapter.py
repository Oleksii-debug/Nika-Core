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


class _OpaqueValue:
    def __repr__(self) -> str:
        return "<opaque>"


class _FakeGraph:
    def __init__(self) -> None:
        self.calls: list[tuple[object, object]] = []
        self.responses: list[object] = []

    async def ainvoke(self, graph_input: object, *, config: object) -> object:
        self.calls.append((graph_input, config))
        if not self.responses:
            raise RuntimeError("No fake response configured")
        return self.responses.pop(0)


class _BlockingGraph:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = False
        self.calls = 0

    async def ainvoke(self, graph_input: object, *, config: object) -> object:
        self.calls += 1
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return {"answer": "done"}


def test_langgraph_adapter_normalizes_completed_result_and_step_limit() -> None:
    graph = _FakeGraph()
    graph.responses.append({"answer": "done"})
    runtime = LangGraphRuntime(graph)
    result = asyncio.run(
        runtime.run(RuntimeRequest("task-1", "thread-1", {"goal": "x"}, max_steps=12))
    )
    assert result.outcome == RuntimeOutcome.COMPLETED
    assert result.output == {"answer": "done"}
    assert graph.calls[0] == (
        {"goal": "x"},
        {"configurable": {"thread_id": "thread-1"}, "recursion_limit": 12},
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
                max_steps=7,
            )
        )
    )
    assert resumed.outcome == RuntimeOutcome.COMPLETED
    assert resumed.output["approved"] is True
    assert graph.calls[1] == (
        ("resume", True),
        {"configurable": {"thread_id": "thread-2"}, "recursion_limit": 7},
    )


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


def test_langgraph_adapter_rejects_resume_token_thread_mismatch_before_graph_call() -> None:
    graph = _FakeGraph()
    runtime = LangGraphRuntime(graph)
    result = asyncio.run(
        runtime.resume(
            RuntimeResumeRequest(
                task_id="task-mismatch",
                thread_id="thread-a",
                resume_token="thread-b",
                mode=RuntimeResumeMode.CONTINUE,
            )
        )
    )
    assert result.outcome == RuntimeOutcome.FAILED
    assert "resume token" in (result.error or "")
    assert graph.calls == []


def test_langgraph_adapter_cancels_active_execution_and_advertises_capability() -> None:
    async def scenario() -> None:
        graph = _BlockingGraph()
        runtime = LangGraphRuntime(graph)
        run_task = asyncio.create_task(runtime.run(RuntimeRequest("task-c", "thread-c")))
        await graph.started.wait()

        assert RuntimeCapability.CANCELLATION in runtime.capabilities
        assert await runtime.cancel(task_id="task-c", thread_id="thread-c") is True
        result = await run_task

        assert result.outcome == RuntimeOutcome.CANCELLED
        assert graph.cancelled is True
        assert await runtime.cancel(task_id="task-c", thread_id="thread-c") is False

    asyncio.run(scenario())


def test_langgraph_adapter_rejects_duplicate_active_task_thread() -> None:
    async def scenario() -> None:
        graph = _BlockingGraph()
        runtime = LangGraphRuntime(graph)
        first = asyncio.create_task(runtime.run(RuntimeRequest("task-d", "thread-d")))
        await graph.started.wait()

        duplicate = await runtime.run(RuntimeRequest("task-d", "thread-d"))
        assert duplicate.outcome == RuntimeOutcome.FAILED
        assert "already active" in (duplicate.error or "")
        assert graph.calls == 1

        assert await runtime.cancel(task_id="task-d", thread_id="thread-d") is True
        assert (await first).outcome == RuntimeOutcome.CANCELLED

    asyncio.run(scenario())


def test_langgraph_adapter_converts_runtime_exception_to_typed_failure() -> None:
    graph = _FakeGraph()
    runtime = LangGraphRuntime(graph)
    result = asyncio.run(runtime.run(RuntimeRequest("task-4", "thread-4")))
    assert result.outcome == RuntimeOutcome.FAILED
    assert "No fake response configured" in (result.error or "")


def test_langgraph_adapter_does_not_leak_framework_objects() -> None:
    graph = _FakeGraph()
    graph.responses.append(
        {
            "opaque": _OpaqueValue(),
            "nested": {"value": _OpaqueValue()},
            "items": (_OpaqueValue(),),
        }
    )
    runtime = LangGraphRuntime(graph)
    result = asyncio.run(runtime.run(RuntimeRequest("task-5", "thread-5")))
    assert result.output == {
        "opaque": "<opaque>",
        "nested": {"value": "<opaque>"},
        "items": ["<opaque>"],
    }
