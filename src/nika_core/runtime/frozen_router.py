from __future__ import annotations

from collections.abc import Sequence
from itertools import islice
from typing import Protocol, runtime_checkable

from .contracts import (
    AgentRuntimePort,
    RuntimeCapability,
    RuntimeRequest,
    RuntimeResult,
    RuntimeResumeProbe,
    RuntimeResumeProbePort,
    RuntimeResumeProbeStatus,
    RuntimeResumeRequest,
)
from .registry import RuntimeRegistry

MAX_FROZEN_RUNTIME_ROUTES = 256
MAX_FROZEN_RUNTIME_ID_CHARS = 128
_DEFAULT_ROUTER_ID = "nika.runtime.frozen-router"


@runtime_checkable
class FrozenRuntimeRouteResolver(Protocol):
    """Trusted durable authority that resolves one execution to its frozen runtime.

    The router deliberately owns no persistence. Product/task/team composition must
    resolve these methods from canonical durable state rather than mutable current
    settings or process-local caches.
    """

    def runtime_id_for_run(self, request: RuntimeRequest) -> str: ...

    def runtime_id_for_resume(self, request: RuntimeResumeRequest) -> str: ...

    def runtime_id_for_existing(self, *, task_id: str, thread_id: str) -> str: ...


class FrozenRuntimeRouter(AgentRuntimePort):
    """Delegate durable child work to an exact pre-authorized RuntimeRegistry route.

    This is a thin runtime-composition seam. It does not choose a model, persist a
    binding, schedule work, retry, or silently fall back. The caller supplies a
    trusted resolver backed by already-durable task/team state. Each dispatch
    re-resolves the exact route so process restart cannot accidentally substitute
    mutable current settings.

    Capabilities are the intersection of the explicitly allowed runtimes. The
    router therefore never advertises cancellation, durable resume, parallelism or
    another capability more strongly than every route it may dispatch to.
    """

    def __init__(
        self,
        *,
        registry: RuntimeRegistry,
        resolver: FrozenRuntimeRouteResolver,
        allowed_runtime_ids: Sequence[str],
        runtime_id: str = _DEFAULT_ROUTER_ID,
    ) -> None:
        if not isinstance(registry, RuntimeRegistry):
            raise TypeError("registry must be a RuntimeRegistry")
        if not isinstance(resolver, FrozenRuntimeRouteResolver):
            raise TypeError("resolver must implement FrozenRuntimeRouteResolver")
        self._runtime_id = _canonical_runtime_id(runtime_id, label="router runtime_id")

        if isinstance(allowed_runtime_ids, (str, bytes, bytearray)):
            raise TypeError("allowed_runtime_ids must be a sequence of runtime IDs")
        expected_count = len(allowed_runtime_ids)
        if expected_count < 1:
            raise ValueError("allowed_runtime_ids must not be empty")
        if expected_count > MAX_FROZEN_RUNTIME_ROUTES:
            raise ValueError(
                "allowed_runtime_ids must contain at most "
                f"{MAX_FROZEN_RUNTIME_ROUTES} entries"
            )
        frozen_ids = tuple(islice(allowed_runtime_ids, MAX_FROZEN_RUNTIME_ROUTES + 1))
        if len(frozen_ids) != expected_count:
            raise ValueError("allowed_runtime_ids changed during admission")
        if len(frozen_ids) > MAX_FROZEN_RUNTIME_ROUTES:
            raise ValueError(
                "allowed_runtime_ids must contain at most "
                f"{MAX_FROZEN_RUNTIME_ROUTES} entries"
            )

        canonical_ids = tuple(
            _canonical_runtime_id(value, label="allowed runtime_id")
            for value in frozen_ids
        )
        if len(set(canonical_ids)) != len(canonical_ids):
            raise ValueError("allowed_runtime_ids must be unique")
        if self._runtime_id in canonical_ids:
            raise ValueError("router runtime_id must differ from delegated runtime IDs")

        self._registry = registry
        self._resolver = resolver
        self._allowed = frozenset(canonical_ids)
        self._runtimes: dict[str, AgentRuntimePort] = {}
        self._capability_snapshots: dict[str, frozenset[RuntimeCapability]] = {}

        common_capabilities: frozenset[RuntimeCapability] | None = None
        for delegated_id in canonical_ids:
            runtime = registry.get(delegated_id)
            capabilities = _snapshot_runtime(
                runtime,
                expected_runtime_id=delegated_id,
            )
            self._runtimes[delegated_id] = runtime
            self._capability_snapshots[delegated_id] = capabilities
            common_capabilities = (
                capabilities
                if common_capabilities is None
                else common_capabilities.intersection(capabilities)
            )

        self._capabilities = common_capabilities or frozenset()

    @property
    def runtime_id(self) -> str:
        return self._runtime_id

    @property
    def capabilities(self) -> frozenset[RuntimeCapability]:
        return self._capabilities

    async def run(self, request: RuntimeRequest) -> RuntimeResult:
        runtime = self._resolve_runtime(self._resolver.runtime_id_for_run(request))
        return await runtime.run(request)

    async def resume(self, request: RuntimeResumeRequest) -> RuntimeResult:
        runtime = self._resolve_runtime(self._resolver.runtime_id_for_resume(request))
        return await runtime.resume(request)

    async def cancel(self, *, task_id: str, thread_id: str) -> bool:
        runtime = self._resolve_existing(task_id=task_id, thread_id=thread_id)
        return await runtime.cancel(task_id=task_id, thread_id=thread_id)

    def initial_resume_token(self, *, task_id: str, thread_id: str) -> str | None:
        """Delegate initial cursor creation when route authority already exists.

        Some supervisors ask for an initial cursor before persisting a newly spawned
        child. Such compositions must resolve the frozen route from an already
        authoritative parent plan/binding, or must not advertise DURABLE_RESUME for
        this router. This method never manufactures route affinity from current
        settings or process-local state.
        """

        if RuntimeCapability.DURABLE_RESUME not in self._capabilities:
            return None
        runtime = self._resolve_existing(task_id=task_id, thread_id=thread_id)
        factory = getattr(runtime, "initial_resume_token", None)
        if not callable(factory):
            raise TypeError(
                "runtime advertises durable resume without initial_resume_token"
            )
        token = factory(task_id=task_id, thread_id=thread_id)
        if token is None:
            raise TypeError("durable runtime initial_resume_token returned None")
        if type(token) is not str or not token or token != token.strip():
            raise TypeError("durable runtime returned a malformed initial resume token")
        return token

    async def probe_resume(
        self,
        *,
        task_id: str,
        thread_id: str,
        resume_token: str,
    ) -> RuntimeResumeProbe:
        runtime = self._resolve_existing(task_id=task_id, thread_id=thread_id)
        if not isinstance(runtime, RuntimeResumeProbePort):
            return RuntimeResumeProbe(
                status=RuntimeResumeProbeStatus.UNVERIFIABLE,
                reason="Frozen runtime route has no restart verification probe.",
            )
        return await runtime.probe_resume(
            task_id=task_id,
            thread_id=thread_id,
            resume_token=resume_token,
        )

    def _resolve_existing(self, *, task_id: str, thread_id: str) -> AgentRuntimePort:
        runtime_id = self._resolver.runtime_id_for_existing(
            task_id=task_id,
            thread_id=thread_id,
        )
        return self._resolve_runtime(runtime_id)

    def _resolve_runtime(self, value: str) -> AgentRuntimePort:
        runtime_id = _canonical_runtime_id(value, label="resolved runtime_id")
        if runtime_id not in self._allowed:
            raise LookupError("durable runtime route is not allowed by this router")

        runtime = self._registry.get(runtime_id)
        expected_runtime = self._runtimes[runtime_id]
        if runtime is not expected_runtime:
            raise RuntimeError("registered runtime identity changed after router admission")
        capabilities = _snapshot_runtime(runtime, expected_runtime_id=runtime_id)
        if capabilities != self._capability_snapshots[runtime_id]:
            raise RuntimeError("registered runtime capabilities changed after router admission")
        return runtime


def _snapshot_runtime(
    runtime: AgentRuntimePort,
    *,
    expected_runtime_id: str,
) -> frozenset[RuntimeCapability]:
    observed_id = runtime.runtime_id
    if type(observed_id) is not str or observed_id != expected_runtime_id:
        raise TypeError("registered runtime identity is not exact or stable")

    capabilities = runtime.capabilities
    if type(capabilities) is not frozenset:
        raise TypeError("registered runtime capabilities must be an exact frozenset")
    for capability in capabilities:
        if type(capability) is not RuntimeCapability:
            raise TypeError("registered runtime capability must be RuntimeCapability")
    return frozenset(capabilities)


def _canonical_runtime_id(value: str, *, label: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{label} must be exact text")
    if not value or value != value.strip():
        raise ValueError(f"{label} must be non-empty canonical text")
    if len(value) > MAX_FROZEN_RUNTIME_ID_CHARS:
        raise ValueError(
            f"{label} must contain at most {MAX_FROZEN_RUNTIME_ID_CHARS} characters"
        )
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"{label} must not contain control characters")
    return value
