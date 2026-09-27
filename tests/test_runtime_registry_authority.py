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


def test_registry_rejects_noncallable_effect_members_before_admission() -> None:
    class _NonCallableRuntime:
        runtime_id = "noncallable"
        capabilities = frozenset({RuntimeCapability.DETERMINISTIC_NO_LLM})
        run = 1
        resume = 2
        cancel = 3

    registry = RuntimeRegistry()

    with pytest.raises(TypeError, match="runtime effect must be callable: run"):
        registry.register(_NonCallableRuntime())

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

    for runtime_id in (" reference", "reference\nforged", "r" * 129):
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
