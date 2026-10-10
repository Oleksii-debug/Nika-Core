from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from inspect import getattr_static

from nika_core.runtime.contracts import AgentRuntimePort, RuntimeCapability

MAX_RUNTIME_ID_CHARS = 128


@dataclass(frozen=True, slots=True)
class RuntimeDescriptor:
    runtime_id: str
    capabilities: frozenset[RuntimeCapability]


@dataclass(frozen=True, slots=True)
class _RegisteredRuntime:
    runtime: AgentRuntimePort
    descriptor: RuntimeDescriptor
    effects: tuple[object, object, object]


def _canonical_runtime_id(value: object) -> str:
    if type(value) is not str:
        raise TypeError("runtime_id must be an exact string")
    if not value:
        raise ValueError("runtime_id must not be empty")
    if value != value.strip():
        raise ValueError("runtime_id must not contain surrounding whitespace")
    if len(value) > MAX_RUNTIME_ID_CHARS:
        raise ValueError(
            f"runtime_id must contain at most {MAX_RUNTIME_ID_CHARS} characters"
        )
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("runtime_id must not contain control characters")
    return value


def _canonical_capabilities(value: object) -> frozenset[RuntimeCapability]:
    if type(value) is not frozenset:
        raise TypeError("runtime capabilities must be an exact frozenset")
    capabilities = value
    if any(type(item) is not RuntimeCapability for item in capabilities):
        raise TypeError("runtime capabilities must contain exact RuntimeCapability values")
    return capabilities


def _snapshot_runtime(runtime: AgentRuntimePort) -> RuntimeDescriptor:
    return RuntimeDescriptor(
        runtime_id=_canonical_runtime_id(runtime.runtime_id),
        capabilities=_canonical_capabilities(runtime.capabilities),
    )


def _require_effect_callables(runtime: AgentRuntimePort) -> None:
    for effect_name in ("run", "resume", "cancel"):
        if not callable(getattr(runtime, effect_name, None)):
            raise TypeError(f"runtime effect must be callable: {effect_name}")


_EFFECT_NOT_STATIC = object()


def _snapshot_effects(runtime: AgentRuntimePort) -> tuple[object, object, object]:
    snapshots: list[object] = []
    for effect_name in ("run", "resume", "cancel"):
        effect = getattr(runtime, effect_name, None)
        if not callable(effect):
            raise TypeError(f"runtime effect must be callable: {effect_name}")
        static_effect = getattr_static(runtime, effect_name, _EFFECT_NOT_STATIC)
        snapshots.append(effect if static_effect is _EFFECT_NOT_STATIC else static_effect)
    return snapshots[0], snapshots[1], snapshots[2]


class RuntimeRegistry:
    def __init__(self) -> None:
        self._runtimes: dict[str, _RegisteredRuntime] = {}

    def register(self, runtime: AgentRuntimePort) -> None:
        if not isinstance(runtime, AgentRuntimePort):
            raise TypeError("runtime must implement AgentRuntimePort")
        descriptor = _snapshot_runtime(runtime)
        effects = _snapshot_effects(runtime)
        if descriptor.runtime_id in self._runtimes:
            raise ValueError(f"Runtime already registered: {descriptor.runtime_id}")
        self._runtimes[descriptor.runtime_id] = _RegisteredRuntime(
            runtime=runtime,
            descriptor=descriptor,
            effects=effects,
        )

    def get(self, runtime_id: str) -> AgentRuntimePort:
        try:
            canonical_id = _canonical_runtime_id(runtime_id)
        except (TypeError, ValueError) as exc:
            raise KeyError("Unknown runtime identifier") from exc
        try:
            registered = self._runtimes[canonical_id]
        except KeyError as exc:
            raise KeyError(f"Unknown runtime: {canonical_id}") from exc
        return self._verified_runtime(registered)

    def select(self, required: Iterable[RuntimeCapability]) -> AgentRuntimePort:
        required_items = tuple(required)
        if any(type(item) is not RuntimeCapability for item in required_items):
            raise TypeError("required capabilities must be exact RuntimeCapability values")
        required_set = frozenset(required_items)
        candidates = [
            registered
            for registered in self._runtimes.values()
            if required_set <= registered.descriptor.capabilities
        ]
        if not candidates:
            names = ", ".join(sorted(item.value for item in required_set))
            raise LookupError(f"No runtime satisfies required capabilities: {names}")
        selected = min(candidates, key=lambda item: item.descriptor.runtime_id)
        return self._verified_runtime(selected)

    def describe(self) -> tuple[RuntimeDescriptor, ...]:
        registered = tuple(
            sorted(self._runtimes.values(), key=lambda item: item.descriptor.runtime_id)
        )
        for item in registered:
            self._verified_runtime(item)
        return tuple(item.descriptor for item in registered)

    @staticmethod
    def _verified_runtime(registered: _RegisteredRuntime) -> AgentRuntimePort:
        try:
            current = _snapshot_runtime(registered.runtime)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                "registered runtime identity or capabilities became invalid"
            ) from exc
        try:
            current_effects = _snapshot_effects(registered.runtime)
        except TypeError as exc:
            raise RuntimeError("registered runtime effects became invalid") from exc
        if current != registered.descriptor:
            raise RuntimeError("registered runtime identity or capabilities changed")
        if any(
            current_effect is not registered_effect
            for current_effect, registered_effect in zip(
                current_effects,
                registered.effects,
                strict=True,
            )
        ):
            raise RuntimeError("registered runtime effects changed")
        return registered.runtime
