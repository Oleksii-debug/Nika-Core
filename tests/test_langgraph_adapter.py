from __future__ import annotations

import asyncio
from dataclasses import dataclass

from nika_core.runtime.contracts import (
    RuntimeCapability,
    RuntimeErrorCode,
    RuntimeOutcome,
    RuntimeRequest,
    RuntimeResumeMode,
    RuntimeResumeProbeStatus,
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
        self.checkpoint_id: str | None = None
        self.state_reads = 0
        self.state_error: Exception | None = None

    async def ainvoke(self, graph_input: object, *, config: object) -> object:
        self.calls.append((graph_input, config))
        if not self.responses:
            raise RuntimeError("No fake response configured")
        response = self.responses.pop(0)
        self.checkpoint_id = f"checkpoint-{len(self.calls)}"
        return response

    async def aget_state(self, config: object) -> object:
        self.state_reads += 1
        if self.state_error is not None:
            raise self.state_error
        configurable = dict(config["configurable"])
        if self.checkpoint_id is not None:
            configurable["checkpoint_id"] = self.checkpoint_id
        return {"config": {"configurable": configurable}}


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


class _PerThreadBlockingGraph:
    def __init__(self) -> None:
        self.started: dict[str, asyncio.Event] = {}
        self.release: dict[str, asyncio.Event] = {}
        self.calls: list[str] = []

    async def ainvoke(self, graph_input: object, *, config: object) -> object:
        thread_id = config["configurable"]["thread_id"]
        self.calls.append(thread_id)
        started = self.started.setdefault(thread_id, asyncio.Event())
        release = self.release.setdefault(thread_id, asyncio.Event())
        started.set()
        await release.wait()
        return {"thread_id": thread_id}


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
    assert graph.state_reads == 1
    assert graph.calls[1] == (
        ("resume", True),
        {"configurable": {"thread_id": "thread-2"}, "recursion_limit": 7},
    )


def test_langgraph_adapter_continue_resume_uses_none_input_after_checkpoint_probe() -> None:
    graph = _FakeGraph()
    graph.checkpoint_id = "checkpoint-existing"
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
    assert graph.state_reads == 1
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
    assert result.error_code == RuntimeErrorCode.INVALID_RESUME
    assert "resume token" in (result.error or "")
    assert graph.calls == []
    assert graph.state_reads == 0


def test_langgraph_adapter_blocks_missing_checkpoint_before_resume_graph_call() -> None:
    graph = _FakeGraph()
    graph.responses.append({"must_not_run": True})
    runtime = LangGraphRuntime(graph)

    result = asyncio.run(
        runtime.resume(
            RuntimeResumeRequest(
                task_id="task-missing",
                thread_id="thread-missing",
                resume_token="thread-missing",
            )
        )
    )

    assert result.outcome == RuntimeOutcome.FAILED
    assert result.error_code == RuntimeErrorCode.RESUME_UNAVAILABLE
    assert "no persisted" in (result.error or "")
    assert graph.calls == []
    assert graph.state_reads == 1


def test_langgraph_adapter_checkpoint_probe_normalizes_unreadable_failure() -> None:
    graph = _FakeGraph()
    graph.state_error = ValueError("bad checkpoint bytes")
    runtime = LangGraphRuntime(graph)

    probe = asyncio.run(
        runtime.probe_resume(
            task_id="task-corrupt",
            thread_id="thread-corrupt",
            resume_token="thread-corrupt",
        )
    )

    assert probe.status == RuntimeResumeProbeStatus.UNREADABLE
    assert probe.checkpoint_id is None
    assert probe.reason == "checkpoint lookup failed"
    assert "bad checkpoint bytes" not in probe.reason


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
        assert duplicate.error_code == RuntimeErrorCode.DUPLICATE_ACTIVE
        assert "already active" in (duplicate.error or "")
        assert graph.calls == 1

        assert await runtime.cancel(task_id="task-d", thread_id="thread-d") is True
        assert (await first).outcome == RuntimeOutcome.CANCELLED

    asyncio.run(scenario())


def test_langgraph_thread_id_has_single_task_owner_during_concurrent_execution() -> None:
    async def scenario() -> None:
        graph = _PerThreadBlockingGraph()
        runtime = LangGraphRuntime(graph)
        first = asyncio.create_task(runtime.run(RuntimeRequest("task-a", "shared-thread")))
        while "shared-thread" not in graph.started:
            await asyncio.sleep(0)
        await graph.started["shared-thread"].wait()

        second = await runtime.run(RuntimeRequest("task-b", "shared-thread"))
        assert second.outcome == RuntimeOutcome.FAILED
        assert second.error_code == RuntimeErrorCode.DUPLICATE_ACTIVE
        assert graph.calls == ["shared-thread"]
        assert await runtime.cancel(task_id="task-b", thread_id="shared-thread") is False
        assert await runtime.cancel(task_id="task-a", thread_id="shared-thread") is True
        assert (await first).outcome == RuntimeOutcome.CANCELLED

    asyncio.run(scenario())


def test_langgraph_adapter_allows_different_threads_to_run_concurrently() -> None:
    async def scenario() -> None:
        graph = _PerThreadBlockingGraph()
        runtime = LangGraphRuntime(graph)
        first = asyncio.create_task(runtime.run(RuntimeRequest("task-a", "thread-a")))
        second = asyncio.create_task(runtime.run(RuntimeRequest("task-b", "thread-b")))
        while len(graph.started) < 2:
            await asyncio.sleep(0)
        await asyncio.gather(*(event.wait() for event in graph.started.values()))

        graph.release["thread-a"].set()
        graph.release["thread-b"].set()
        first_result, second_result = await asyncio.gather(first, second)

        assert first_result.outcome == RuntimeOutcome.COMPLETED
        assert second_result.outcome == RuntimeOutcome.COMPLETED
        assert sorted(graph.calls) == ["thread-a", "thread-b"]

    asyncio.run(scenario())


def test_langgraph_adapter_converts_runtime_exception_without_fake_cursor() -> None:
    graph = _FakeGraph()
    runtime = LangGraphRuntime(graph)
    result = asyncio.run(runtime.run(RuntimeRequest("task-4", "thread-4")))
    assert result.outcome == RuntimeOutcome.FAILED
    assert result.error_code == RuntimeErrorCode.INTERNAL
    assert result.resume_token is None
    assert result.error == "runtime execution failed"
    assert "No fake response configured" not in (result.error or "")


def test_langgraph_adapter_does_not_leak_framework_objects() -> None:
    graph = _FakeGraph()
    graph.responses.append(
        {
            "opaque": _OpaqueValue(),
            "nested": {"value": _OpaqueValue()},
            "items": (_OpaqueValue(),),
            "set_items": {"z", "a"},
        }
    )
    runtime = LangGraphRuntime(graph)
    result = asyncio.run(runtime.run(RuntimeRequest("task-5", "thread-5")))
    assert result.output == {
        "opaque": "<opaque>",
        "nested": {"value": "<opaque>"},
        "items": ["<opaque>"],
        "set_items": ["a", "z"],
    }
