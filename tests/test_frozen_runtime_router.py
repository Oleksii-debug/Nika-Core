from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from nika_core.runtime.contracts import (
    RuntimeCapability,
    RuntimeOutcome,
    RuntimeRequest,
    RuntimeResult,
    RuntimeResumeMode,
    RuntimeResumeProbe,
    RuntimeResumeProbeStatus,
    RuntimeResumeRequest,
)
from nika_core.runtime.frozen_router import FrozenRuntimeRouter
from nika_core.runtime.registry import RuntimeRegistry


@dataclass
class _Overlap:
    active: int = 0
    max_active: int = 0


class _Runtime:
    def __init__(
        self,
        runtime_id: str,
        *,
        capabilities: frozenset[RuntimeCapability],
        overlap: _Overlap | None = None,
        reached: asyncio.Event | None = None,
        release: asyncio.Event | None = None,
    ) -> None:
        self._runtime_id = runtime_id
        self._capabilities = capabilities
        self._overlap = overlap
        self._reached = reached
        self._release = release
        self.run_calls: list[tuple[str, str]] = []
        self.resume_calls: list[tuple[str, str, str]] = []
        self.cancel_calls: list[tuple[str, str]] = []
        self.probe_calls: list[tuple[str, str, str]] = []

    @property
    def runtime_id(self) -> str:
        return self._runtime_id

    @property
    def capabilities(self) -> frozenset[RuntimeCapability]:
        return self._capabilities

    async def run(self, request: RuntimeRequest) -> RuntimeResult:
        self.run_calls.append((request.task_id, request.thread_id))
        if self._overlap is not None:
            self._overlap.active += 1
            self._overlap.max_active = max(
                self._overlap.max_active,
                self._overlap.active,
            )
            if (
                self._reached is not None
                and self._overlap.active >= 2
            ):
                self._reached.set()
            try:
                if self._release is not None:
                    await self._release.wait()
            finally:
                self._overlap.active -= 1
        return RuntimeResult(
            outcome=RuntimeOutcome.COMPLETED,
            output={"runtime_id": self._runtime_id, "thread_id": request.thread_id},
        )

    async def resume(self, request: RuntimeResumeRequest) -> RuntimeResult:
        self.resume_calls.append(
            (request.task_id, request.thread_id, request.resume_token)
        )
        return RuntimeResult(
            outcome=RuntimeOutcome.COMPLETED,
            output={"runtime_id": self._runtime_id, "resumed": True},
        )

    async def cancel(self, *, task_id: str, thread_id: str) -> bool:
        self.cancel_calls.append((task_id, thread_id))
        return RuntimeCapability.CANCELLATION in self._capabilities

    @staticmethod
    def _token(runtime_id: str, task_id: str, thread_id: str) -> str:
        return f"resume:{runtime_id}:{task_id}:{thread_id}"

    def initial_resume_token(self, *, task_id: str, thread_id: str) -> str:
        return self._token(self._runtime_id, task_id, thread_id)

    async def probe_resume(
        self,
        *,
        task_id: str,
        thread_id: str,
        resume_token: str,
    ) -> RuntimeResumeProbe:
        self.probe_calls.append((task_id, thread_id, resume_token))
        expected = self._token(self._runtime_id, task_id, thread_id)
        if resume_token != expected:
            return RuntimeResumeProbe(
                status=RuntimeResumeProbeStatus.INVALID,
                reason="Wrong frozen runtime cursor.",
            )
        return RuntimeResumeProbe(
            status=RuntimeResumeProbeStatus.READY,
            reason="Frozen runtime cursor verified.",
            checkpoint_id=f"checkpoint:{self._runtime_id}:{thread_id}",
        )


class _Resolver:
    def __init__(self, routes: dict[tuple[str, str], str]) -> None:
        self._routes = dict(routes)

    def runtime_id_for_run(self, request: RuntimeRequest) -> str:
        return self._routes[(request.task_id, request.thread_id)]

    def runtime_id_for_resume(self, request: RuntimeResumeRequest) -> str:
        return self._routes[(request.task_id, request.thread_id)]

    def runtime_id_for_existing(self, *, task_id: str, thread_id: str) -> str:
        return self._routes[(task_id, thread_id)]


def _registry(*runtimes: _Runtime) -> RuntimeRegistry:
    registry = RuntimeRegistry()
    for runtime in runtimes:
        registry.register(runtime)
    return registry


def _durable_caps(*, cancellation: bool = True) -> frozenset[RuntimeCapability]:
    values = {
        RuntimeCapability.DURABLE_RESUME,
        RuntimeCapability.PARALLELISM,
    }
    if cancellation:
        values.add(RuntimeCapability.CANCELLATION)
    return frozenset(values)


def test_two_durable_children_dispatch_to_distinct_runtimes_concurrently() -> None:
    async def scenario() -> tuple[int, str, str]:
        overlap = _Overlap()
        reached = asyncio.Event()
        release = asyncio.Event()
        runtime_a = _Runtime(
            "runtime-a",
            capabilities=_durable_caps(),
            overlap=overlap,
            reached=reached,
            release=release,
        )
        runtime_b = _Runtime(
            "runtime-b",
            capabilities=_durable_caps(),
            overlap=overlap,
            reached=reached,
            release=release,
        )
        router = FrozenRuntimeRouter(
            registry=_registry(runtime_a, runtime_b),
            resolver=_Resolver(
                {
                    ("task", "thread-a"): "runtime-a",
                    ("task", "thread-b"): "runtime-b",
                }
            ),
            allowed_runtime_ids=("runtime-a", "runtime-b"),
        )

        child_a = asyncio.create_task(
            router.run(RuntimeRequest(task_id="task", thread_id="thread-a"))
        )
        child_b = asyncio.create_task(
            router.run(RuntimeRequest(task_id="task", thread_id="thread-b"))
        )
        await asyncio.wait_for(reached.wait(), timeout=1.0)
        observed = overlap.max_active
        release.set()
        result_a, result_b = await asyncio.gather(child_a, child_b)
        return (
            observed,
            str(result_a.output["runtime_id"]),
            str(result_b.output["runtime_id"]),
        )

    overlap, route_a, route_b = asyncio.run(scenario())

    assert overlap == 2
    assert route_a == "runtime-a"
    assert route_b == "runtime-b"


def test_restart_reconstruction_resumes_each_child_on_same_frozen_runtime() -> None:
    runtime_a = _Runtime("runtime-a", capabilities=_durable_caps())
    runtime_b = _Runtime("runtime-b", capabilities=_durable_caps())
    registry = _registry(runtime_a, runtime_b)
    durable_routes = {
        ("task", "thread-a"): "runtime-a",
        ("task", "thread-b"): "runtime-b",
    }

    first_process = FrozenRuntimeRouter(
        registry=registry,
        resolver=_Resolver(durable_routes),
        allowed_runtime_ids=("runtime-a", "runtime-b"),
    )
    token_a = first_process.initial_resume_token(
        task_id="task",
        thread_id="thread-a",
    )
    token_b = first_process.initial_resume_token(
        task_id="task",
        thread_id="thread-b",
    )

    # Fresh router instance represents process reconstruction. Route truth comes
    # from the durable resolver again; no process-local affinity is reused.
    restarted = FrozenRuntimeRouter(
        registry=registry,
        resolver=_Resolver(durable_routes),
        allowed_runtime_ids=("runtime-a", "runtime-b"),
    )
    result_a = asyncio.run(
        restarted.resume(
            RuntimeResumeRequest(
                task_id="task",
                thread_id="thread-a",
                resume_token=token_a or "",
                mode=RuntimeResumeMode.CONTINUE,
            )
        )
    )
    result_b = asyncio.run(
        restarted.resume(
            RuntimeResumeRequest(
                task_id="task",
                thread_id="thread-b",
                resume_token=token_b or "",
                mode=RuntimeResumeMode.CONTINUE,
            )
        )
    )

    assert result_a.output["runtime_id"] == "runtime-a"
    assert result_b.output["runtime_id"] == "runtime-b"
    assert runtime_a.resume_calls == [("task", "thread-a", token_a)]
    assert runtime_b.resume_calls == [("task", "thread-b", token_b)]


def test_cancel_and_probe_follow_exact_frozen_child_route() -> None:
    runtime_a = _Runtime("runtime-a", capabilities=_durable_caps())
    runtime_b = _Runtime("runtime-b", capabilities=_durable_caps())
    router = FrozenRuntimeRouter(
        registry=_registry(runtime_a, runtime_b),
        resolver=_Resolver(
            {
                ("task", "thread-a"): "runtime-a",
                ("task", "thread-b"): "runtime-b",
            }
        ),
        allowed_runtime_ids=("runtime-a", "runtime-b"),
    )

    token = router.initial_resume_token(task_id="task", thread_id="thread-b")
    probe = asyncio.run(
        router.probe_resume(
            task_id="task",
            thread_id="thread-b",
            resume_token=token or "",
        )
    )
    cancelled = asyncio.run(router.cancel(task_id="task", thread_id="thread-a"))

    assert probe.status is RuntimeResumeProbeStatus.READY
    assert runtime_a.cancel_calls == [("task", "thread-a")]
    assert runtime_b.cancel_calls == []
    assert runtime_b.probe_calls == [("task", "thread-b", token)]
    assert cancelled is True


def test_router_capabilities_are_only_common_capabilities() -> None:
    runtime_a = _Runtime("runtime-a", capabilities=_durable_caps(cancellation=True))
    runtime_b = _Runtime("runtime-b", capabilities=_durable_caps(cancellation=False))
    router = FrozenRuntimeRouter(
        registry=_registry(runtime_a, runtime_b),
        resolver=_Resolver({("task", "thread"): "runtime-a"}),
        allowed_runtime_ids=("runtime-a", "runtime-b"),
    )

    assert router.capabilities == frozenset(
        {RuntimeCapability.DURABLE_RESUME, RuntimeCapability.PARALLELISM}
    )
    assert RuntimeCapability.CANCELLATION not in router.capabilities


def test_unallowed_resolved_runtime_fails_before_any_runtime_effect() -> None:
    runtime_a = _Runtime("runtime-a", capabilities=_durable_caps())
    runtime_b = _Runtime("runtime-b", capabilities=_durable_caps())
    router = FrozenRuntimeRouter(
        registry=_registry(runtime_a, runtime_b),
        resolver=_Resolver({("task", "thread"): "runtime-b"}),
        allowed_runtime_ids=("runtime-a",),
    )

    with pytest.raises(LookupError, match="not allowed"):
        asyncio.run(router.run(RuntimeRequest(task_id="task", thread_id="thread")))

    assert runtime_a.run_calls == []
    assert runtime_b.run_calls == []


def test_runtime_capability_drift_fails_before_effect() -> None:
    runtime = _Runtime("runtime-a", capabilities=_durable_caps())
    router = FrozenRuntimeRouter(
        registry=_registry(runtime),
        resolver=_Resolver({("task", "thread"): "runtime-a"}),
        allowed_runtime_ids=("runtime-a",),
    )
    runtime._capabilities = frozenset({RuntimeCapability.PARALLELISM})

    with pytest.raises(RuntimeError, match="capabilities changed"):
        asyncio.run(router.run(RuntimeRequest(task_id="task", thread_id="thread")))

    assert runtime.run_calls == []


def test_resolved_runtime_id_subclass_is_rejected_before_effect() -> None:
    class _HostileRuntimeId(str):
        pass

    class _HostileResolver(_Resolver):
        def runtime_id_for_run(self, request: RuntimeRequest) -> str:
            del request
            return _HostileRuntimeId("runtime-a")

    runtime = _Runtime("runtime-a", capabilities=_durable_caps())
    router = FrozenRuntimeRouter(
        registry=_registry(runtime),
        resolver=_HostileResolver({("task", "thread"): "runtime-a"}),
        allowed_runtime_ids=("runtime-a",),
    )

    with pytest.raises(TypeError, match="resolved runtime_id must be exact text"):
        asyncio.run(router.run(RuntimeRequest(task_id="task", thread_id="thread")))

    assert runtime.run_calls == []
