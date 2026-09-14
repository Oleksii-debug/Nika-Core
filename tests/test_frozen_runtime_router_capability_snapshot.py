from __future__ import annotations

from nika_core.runtime.contracts import (
    RuntimeCapability,
    RuntimeOutcome,
    RuntimeRequest,
    RuntimeResult,
    RuntimeResumeRequest,
)
from nika_core.runtime.frozen_router import FrozenRuntimeRouter
from nika_core.runtime.registry import RuntimeRegistry


class _Resolver:
    def runtime_id_for_run(self, request: RuntimeRequest) -> str:
        del request
        return "runtime-a"

    def runtime_id_for_resume(self, request: RuntimeResumeRequest) -> str:
        del request
        return "runtime-a"

    def runtime_id_for_existing(self, *, task_id: str, thread_id: str) -> str:
        del task_id, thread_id
        return "runtime-a"


class _ThirdReadExplodesRuntime:
    """Proves cursor admission does not trust capabilities twice after fencing."""

    def __init__(self) -> None:
        self.capability_reads = 0

    @property
    def runtime_id(self) -> str:
        return "runtime-a"

    @property
    def capabilities(self) -> frozenset[RuntimeCapability]:
        self.capability_reads += 1
        if self.capability_reads >= 3:
            raise AssertionError("capabilities were re-read after the stability fence")
        return frozenset(
            {
                RuntimeCapability.DURABLE_RESUME,
                RuntimeCapability.PARALLELISM,
            }
        )

    async def run(self, request: RuntimeRequest) -> RuntimeResult:
        del request
        return RuntimeResult(outcome=RuntimeOutcome.COMPLETED)

    async def resume(self, request: RuntimeResumeRequest) -> RuntimeResult:
        del request
        return RuntimeResult(outcome=RuntimeOutcome.COMPLETED)

    async def cancel(self, *, task_id: str, thread_id: str) -> bool:
        del task_id, thread_id
        return False

    @staticmethod
    def initial_resume_token(*, task_id: str, thread_id: str) -> str:
        return f"token:{task_id}:{thread_id}"


def test_initial_resume_token_uses_router_admitted_capability_truth() -> None:
    runtime = _ThirdReadExplodesRuntime()
    registry = RuntimeRegistry()
    registry.register(runtime)
    router = FrozenRuntimeRouter(
        registry=registry,
        resolver=_Resolver(),
        allowed_runtime_ids=("runtime-a",),
    )

    token = router.initial_resume_token(task_id="task", thread_id="thread")

    assert token == "token:task:thread"
    assert runtime.capability_reads == 2
