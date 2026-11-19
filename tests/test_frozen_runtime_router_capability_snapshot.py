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


class _MixedResolver(_Resolver):
    def runtime_id_for_existing(self, *, task_id: str, thread_id: str) -> str:
        del thread_id
        return {
            "durable-task": "runtime-a",
            "plain-task": "runtime-b",
        }[task_id]


class _ThirdReadExplodesRuntime:
    """Proves cursor admission does not trust capabilities twice after fencing."""

    def __init__(
        self,
        *,
        runtime_id: str = "runtime-a",
        durable_resume: bool = True,
    ) -> None:
        self._runtime_id = runtime_id
        self._durable_resume = durable_resume
        self.capability_reads = 0

    @property
    def runtime_id(self) -> str:
        return self._runtime_id

    @property
    def capabilities(self) -> frozenset[RuntimeCapability]:
        self.capability_reads += 1
        if self.capability_reads >= 3:
            raise AssertionError("capabilities were re-read after the stability fence")
        values = {RuntimeCapability.PARALLELISM}
        if self._durable_resume:
            values.add(RuntimeCapability.DURABLE_RESUME)
        return frozenset(values)

    async def run(self, request: RuntimeRequest) -> RuntimeResult:
        del request
        return RuntimeResult(outcome=RuntimeOutcome.COMPLETED)

    async def resume(self, request: RuntimeResumeRequest) -> RuntimeResult:
        del request
        return RuntimeResult(outcome=RuntimeOutcome.COMPLETED)

    async def cancel(self, *, task_id: str, thread_id: str) -> bool:
        del task_id, thread_id
        return False

    def initial_resume_token(self, *, task_id: str, thread_id: str) -> str:
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


def test_initial_resume_token_uses_exact_route_capability_in_mixed_portfolio() -> None:
    durable = _ThirdReadExplodesRuntime(
        runtime_id="runtime-a",
        durable_resume=True,
    )
    plain = _ThirdReadExplodesRuntime(
        runtime_id="runtime-b",
        durable_resume=False,
    )
    registry = RuntimeRegistry()
    registry.register(durable)
    registry.register(plain)
    router = FrozenRuntimeRouter(
        registry=registry,
        resolver=_MixedResolver(),
        allowed_runtime_ids=("runtime-a", "runtime-b"),
    )

    assert router.capabilities == frozenset({RuntimeCapability.PARALLELISM})
    assert RuntimeCapability.DURABLE_RESUME not in router.capabilities

    durable_token = router.initial_resume_token(
        task_id="durable-task",
        thread_id="durable-thread",
    )
    plain_token = router.initial_resume_token(
        task_id="plain-task",
        thread_id="plain-thread",
    )

    assert durable_token == "token:durable-task:durable-thread"
    assert plain_token is None
    assert durable.capability_reads == 2
    assert plain.capability_reads == 2
