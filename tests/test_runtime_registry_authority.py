from __future__ import annotations

import pytest

from nika_core.runtime.contracts import RuntimeCapability
from nika_core.runtime.reference import ReferenceRuntime
from nika_core.runtime.registry import RuntimeRegistry


class _TextSubclass(str):
    pass


def test_registry_rejects_incomplete_runtime_adapter() -> None:
    class _IncompleteRuntime:
        runtime_id = "incomplete"
        capabilities = frozenset({RuntimeCapability.DETERMINISTIC_NO_LLM})

    registry = RuntimeRegistry()

    with pytest.raises(TypeError, match="AgentRuntimePort"):
        registry.register(_IncompleteRuntime())


@pytest.mark.parametrize("effect_name", ("run", "resume", "cancel"))
def test_registry_rejects_noncallable_effect_members_before_admission(
    effect_name: str,
) -> None:
    class _RuntimeWithEffects:
        runtime_id = "noncallable"
        capabilities = frozenset({RuntimeCapability.DETERMINISTIC_NO_LLM})

        async def run(self, request) -> None:
            del request

        async def resume(self, request) -> None:
            del request

        async def cancel(self, *, task_id: str, thread_id: str) -> bool:
            del task_id, thread_id
            return False

    runtime = _RuntimeWithEffects()
    setattr(runtime, effect_name, 1)
    registry = RuntimeRegistry()

    with pytest.raises(TypeError, match=f"runtime effect must be callable: {effect_name}"):
        registry.register(runtime)

    assert registry.describe() == ()
    with pytest.raises(KeyError, match="Unknown runtime"):
        registry.get("noncallable")
    with pytest.raises(LookupError):
        registry.select({RuntimeCapability.DETERMINISTIC_NO_LLM})


def test_registry_rejects_noncanonical_runtime_id_carriers() -> None:
    registry = RuntimeRegistry()
    runtime = ReferenceRuntime()
    runtime.runtime_id = _TextSubclass("reference")

    with pytest.raises(TypeError, match="exact string"):
        registry.register(runtime)

    with pytest.raises(KeyError, match="Unknown runtime identifier"):
        registry.get(_TextSubclass("reference"))


def test_registry_malformed_lookup_preserves_unknown_runtime_contract() -> None:
    registry = RuntimeRegistry()
    registry.register(ReferenceRuntime())

    for runtime_id in (
        " reference",
        "reference\nforged",
        "reference\u0085forged",
        "reference\u200bforged",
        "reference\u2028forged",
        "reference\u202eforged",
        "reference\ud800forged",
        "r" * 129,
    ):
        with pytest.raises(KeyError, match="Unknown runtime identifier"):
            registry.get(runtime_id)


def test_registry_rejects_unbounded_or_control_runtime_ids() -> None:
    registry = RuntimeRegistry()

    too_long = ReferenceRuntime()
    too_long.runtime_id = "r" * 129
    with pytest.raises(ValueError, match="at most 128"):
        registry.register(too_long)

    control = ReferenceRuntime()
    control.runtime_id = "reference\nforged"
    with pytest.raises(ValueError, match="control characters"):
        registry.register(control)


@pytest.mark.parametrize(
    "runtime_id",
    (
        "reference\u0085forged",
        "reference\u200bforged",
        "reference\u2028forged",
        "reference\u2029forged",
        "reference\u202eforged",
    ),
)
def test_registry_rejects_unsafe_unicode_runtime_ids(runtime_id: str) -> None:
    registry = RuntimeRegistry()
    runtime = ReferenceRuntime()
    runtime.runtime_id = runtime_id

    with pytest.raises(ValueError, match="control, format, or line-separator"):
        registry.register(runtime)

    assert registry.describe() == ()


@pytest.mark.parametrize(
    "runtime_id",
    (
        "reference\ud800forged",
        "reference\udfffforged",
    ),
)
def test_registry_rejects_non_utf8_runtime_ids(runtime_id: str) -> None:
    registry = RuntimeRegistry()
    runtime = ReferenceRuntime()
    runtime.runtime_id = runtime_id

    with pytest.raises(ValueError, match="runtime_id must be valid UTF-8"):
        registry.register(runtime)

    assert registry.describe() == ()


def test_registry_preserves_printable_unicode_runtime_id() -> None:
    registry = RuntimeRegistry()
    runtime = ReferenceRuntime()
    runtime.runtime_id = "локальний-рушій"
    registry.register(runtime)

    assert registry.get("локальний-рушій") is runtime
    assert registry.describe()[0].runtime_id == "локальний-рушій"


def test_registry_rejects_noncanonical_capability_carriers() -> None:
    registry = RuntimeRegistry()
    runtime = ReferenceRuntime()
    runtime.capabilities = frozenset({"deterministic_no_llm"})

    with pytest.raises(TypeError, match="exact RuntimeCapability"):
        registry.register(runtime)

    clean = ReferenceRuntime()
    registry.register(clean)
    with pytest.raises(TypeError, match="exact RuntimeCapability"):
        registry.select({"deterministic_no_llm"})


def test_registry_fails_closed_after_runtime_identity_drift() -> None:
    registry = RuntimeRegistry()
    runtime = ReferenceRuntime()
    registry.register(runtime)

    runtime.runtime_id = "mutated-runtime"

    with pytest.raises(RuntimeError, match="identity or capabilities changed"):
        registry.get("reference")
    with pytest.raises(RuntimeError, match="identity or capabilities changed"):
        registry.describe()


def test_registry_normalizes_malformed_post_registration_drift() -> None:
    registry = RuntimeRegistry()
    runtime = ReferenceRuntime()
    registry.register(runtime)

    runtime.capabilities = frozenset({"deterministic_no_llm"})

    with pytest.raises(RuntimeError, match="became invalid"):
        registry.get("reference")


def test_registry_blocks_post_registration_capability_expansion() -> None:
    registry = RuntimeRegistry()
    runtime = ReferenceRuntime()
    registry.register(runtime)

    runtime.capabilities = frozenset(
        {
            RuntimeCapability.DETERMINISTIC_NO_LLM,
            RuntimeCapability.DURABLE_RESUME,
        }
    )

    with pytest.raises(LookupError):
        registry.select({RuntimeCapability.DURABLE_RESUME})
    with pytest.raises(RuntimeError, match="identity or capabilities changed"):
        registry.select({RuntimeCapability.DETERMINISTIC_NO_LLM})


@pytest.mark.parametrize("effect_name", ("run", "resume", "cancel"))
def test_registry_blocks_post_registration_noncallable_effect_drift(
    effect_name: str,
) -> None:
    class _MutableRuntime:
        runtime_id = "mutable"
        capabilities = frozenset({RuntimeCapability.DETERMINISTIC_NO_LLM})

        async def run(self, request) -> None:
            del request

        async def resume(self, request) -> None:
            del request

        async def cancel(self, *, task_id: str, thread_id: str) -> bool:
            del task_id, thread_id
            return False

    runtime = _MutableRuntime()
    registry = RuntimeRegistry()
    registry.register(runtime)
    setattr(runtime, effect_name, 1)

    with pytest.raises(RuntimeError, match="runtime effects became invalid"):
        registry.get("mutable")
    with pytest.raises(RuntimeError, match="runtime effects became invalid"):
        registry.select({RuntimeCapability.DETERMINISTIC_NO_LLM})
    with pytest.raises(RuntimeError, match="runtime effects became invalid"):
        registry.describe()


@pytest.mark.parametrize("effect_name", ("run", "resume", "cancel"))
def test_registry_blocks_post_registration_callable_effect_replacement(
    effect_name: str,
) -> None:
    class _MutableRuntime:
        runtime_id = "mutable-callable"
        capabilities = frozenset({RuntimeCapability.DETERMINISTIC_NO_LLM})

        async def run(self, request) -> None:
            del request

        async def resume(self, request) -> None:
            del request

        async def cancel(self, *, task_id: str, thread_id: str) -> bool:
            del task_id, thread_id
            return False

    async def replacement(*args, **kwargs) -> None:
        del args, kwargs

    runtime = _MutableRuntime()
    registry = RuntimeRegistry()
    registry.register(runtime)
    setattr(runtime, effect_name, replacement)

    with pytest.raises(RuntimeError, match="runtime effects changed"):
        registry.get("mutable-callable")
    with pytest.raises(RuntimeError, match="runtime effects changed"):
        registry.select({RuntimeCapability.DETERMINISTIC_NO_LLM})
    with pytest.raises(RuntimeError, match="runtime effects changed"):
        registry.describe()


def test_describe_runtime_id_mutation_cannot_rewrite_registry_snapshot() -> None:
    registry = RuntimeRegistry()
    runtime = ReferenceRuntime()
    registry.register(runtime)

    descriptor = registry.describe()[0]
    object.__setattr__(descriptor, "runtime_id", "forged-runtime")

    assert registry.get("reference") is runtime
    assert registry.describe()[0].runtime_id == "reference"


def test_describe_capability_mutation_cannot_rewrite_registry_snapshot() -> None:
    registry = RuntimeRegistry()
    runtime = ReferenceRuntime()
    registry.register(runtime)

    descriptor = registry.describe()[0]
    object.__setattr__(
        descriptor,
        "capabilities",
        frozenset({RuntimeCapability.DURABLE_RESUME}),
    )

    assert registry.select({RuntimeCapability.DETERMINISTIC_NO_LLM}) is runtime
    with pytest.raises(LookupError, match="No runtime satisfies"):
        registry.select({RuntimeCapability.DURABLE_RESUME})


def test_registry_returns_stable_snapshot_descriptors() -> None:
    registry = RuntimeRegistry()
    first = ReferenceRuntime()
    second = ReferenceRuntime()
    second.runtime_id = "reference-z"
    registry.register(second)
    registry.register(first)

    descriptors = registry.describe()

    assert tuple(item.runtime_id for item in descriptors) == (
        "reference",
        "reference-z",
    )
    assert descriptors[0].capabilities == frozenset(
        {RuntimeCapability.DETERMINISTIC_NO_LLM}
    )
