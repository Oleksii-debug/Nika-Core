from __future__ import annotations

import asyncio

from nika_core.runtime import contracts as runtime_contracts, langgraph_runtime

_CANARY = "P10_05_SYNTHETIC_BACKEND_SECRET_7f31c2"


class _FailingGraph:
    async def ainvoke(self, graph_input, *, config):  # type: ignore[no-untyped-def]
        del graph_input, config
        raise RuntimeError(f"Authorization: Bearer {_CANARY}")

    async def aget_state(self, config):  # type: ignore[no-untyped-def]
        del config
        raise RuntimeError(f"checkpoint backend token={_CANARY}")


def test_framework_exception_is_minimized_at_runtime_result_boundary() -> None:
    runtime = langgraph_runtime.LangGraphRuntime(_FailingGraph())

    result = asyncio.run(
        runtime.run(
            runtime_contracts.RuntimeRequest(
                task_id="runtime-minimized-error-task",
                thread_id="runtime-minimized-error-thread",
                payload={"safe": "value"},
            )
        )
    )

    assert result.outcome is runtime_contracts.RuntimeOutcome.FAILED
    assert result.error_code is runtime_contracts.RuntimeErrorCode.INTERNAL
    assert result.error == "runtime execution failed"
    assert _CANARY not in (result.error or "")


def test_checkpoint_exception_is_minimized_at_resume_probe_boundary() -> None:
    runtime = langgraph_runtime.LangGraphRuntime(_FailingGraph())

    probe = asyncio.run(
        runtime.probe_resume(
            task_id="runtime-minimized-error-task",
            thread_id="runtime-minimized-error-thread",
            resume_token="runtime-minimized-error-thread",
        )
    )

    assert probe.status is runtime_contracts.RuntimeResumeProbeStatus.UNREADABLE
    assert probe.reason == "checkpoint lookup failed"
    assert _CANARY not in probe.reason
