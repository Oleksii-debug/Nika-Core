from __future__ import annotations

from nika_core.runtime.contracts import (
    RuntimeCapability,
    RuntimeEvent,
    RuntimeOutcome,
    RuntimeRequest,
    RuntimeResult,
)


class ReferenceRuntime:
    """Deterministic no-LLM runtime used to prove Nika's framework-neutral contract."""

    runtime_id = "reference"
    capabilities = frozenset({RuntimeCapability.CANCELLATION, RuntimeCapability.LOCAL_MODELS})

    async def run(self, request: RuntimeRequest) -> RuntimeResult:
        event = RuntimeEvent(
            sequence=0,
            event_type="reference.completed",
            payload={"task_id": request.task_id, "thread_id": request.thread_id},
        )
        return RuntimeResult(
            outcome=RuntimeOutcome.COMPLETED,
            events=(event,),
            output={"echo": dict(request.payload), "max_steps": request.max_steps},
        )
