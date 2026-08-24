from __future__ import annotations

import asyncio

from nika_core.runtime.contracts import RuntimeOutcome, RuntimeRequest, RuntimeResumeProbeStatus
from nika_core.runtime.langgraph_runtime import LangGraphRuntime


_CANARY = "QA53_SYNTHETIC_RUNTIME_EXCEPTION_SECRET_61d4a8c2"


class _FailingGraph:
    async def ainvoke(self, graph_input, *, config):  # type: ignore[no-untyped-def]
        del graph_input, config
        raise RuntimeError(f"Authorization: Bearer {_CANARY}")

    async def aget_state(self, config):  # type: ignore[no-untyped-def]
        del config
        raise RuntimeError(f"checkpoint backend token={_CANARY}")


def test_framework_exception_secret_cannot_escape_runtime_result_error() -> None:
    """QA_ONLY: arbitrary framework diagnostics must not become public RuntimeResult.error."""

    runtime = LangGraphRuntime(_FailingGraph())
    result = asyncio.run(
        runtime.run(
            RuntimeRequest(
                task_id="qa53-runtime-task",
                thread_id="qa53-runtime-thread",
                payload={"safe": "value"},
            )
        )
    )

    assert result.outcome is RuntimeOutcome.FAILED
    assert _CANARY not in (result.error or "")


def test_checkpoint_probe_secret_cannot_escape_public_reason() -> None:
    """QA_ONLY: checkpoint backend exceptions must be normalized without secret text."""

    runtime = LangGraphRuntime(_FailingGraph())
    probe = asyncio.run(
        runtime.probe_resume(
            task_id="qa53-runtime-task",
            thread_id="qa53-runtime-thread",
            resume_token="qa53-runtime-thread",
        )
    )

    assert probe.status is RuntimeResumeProbeStatus.UNREADABLE
    assert _CANARY not in probe.reason
