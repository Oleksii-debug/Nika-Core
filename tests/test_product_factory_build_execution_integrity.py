from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime

import pytest

from nika_core.product_factory_build_execution import (
    BuildExecutionCoordinator,
    BuildExecutionDispatch,
    BuildExecutionError,
    BuildExecutionResult,
    BuildExecutionSpec,
    ProjectExecutionAuthority,
)
from nika_core.product_factory_deployment import (
    ExecutionNode,
    ExecutionNodeRegistry,
    ExecutionRequest,
    NodeCapabilities,
    NodeIdentity,
    Platform,
    ResourceEnvelope,
)

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
SHA = "a" * 40
DIGEST = "1" * 64


@dataclass
class AlwaysAvailable:
    def is_available(self, node_id: str) -> bool:
        return True


def _node(
    node_id: str,
    *,
    features: frozenset[str] = frozenset({"build"}),
) -> ExecutionNode:
    return ExecutionNode(
        NodeIdentity(node_id, Platform.LINUX, "x86_64", f"instance-{node_id}"),
        NodeCapabilities(features, frozenset({"python"}), False),
        ResourceEnvelope(8, 16384, 65536),
    )


def _authority(*node_ids: str) -> ProjectExecutionAuthority:
    return ProjectExecutionAuthority(
        "project-1",
        "repo-main",
        "products/build",
        tuple(node_ids),
        ("pypi.org:443",),
        ("credref:package-index",),
    )


def _spec(*node_ids: str, lease_seconds: int = 120) -> BuildExecutionSpec:
    return BuildExecutionSpec(
        ExecutionRequest(
            "project-1",
            "work-1",
            Platform.LINUX,
            frozenset({"build"}),
            frozenset({"python"}),
            ResourceEnvelope(2, 2048, 4096),
        ),
        SHA,
        ("python", "-m", "build"),
        _authority(*node_ids),
        lease_seconds,
    )


def _prepared() -> tuple[
    BuildExecutionCoordinator,
    ExecutionNodeRegistry,
    BuildExecutionSpec,
]:
    registry = ExecutionNodeRegistry()
    registry.register(_node("linux-1"))
    registry.register(_node("linux-2"))
    coordinator = BuildExecutionCoordinator(registry, AlwaysAvailable())
    spec = _spec("linux-1", "linux-2")
    coordinator.submit(spec, now=NOW)
    coordinator.prepare("work-1", now=NOW)
    return coordinator, registry, spec


def test_restore_rejects_same_work_with_substituted_lease_identity() -> None:
    coordinator, registry, spec = _prepared()
    coordinator_snapshot = coordinator.snapshot()
    registry_snapshot = registry.snapshot()
    old_lease = registry_snapshot.leases[0]

    restarted_registry = ExecutionNodeRegistry()
    restarted_registry.restore(registry_snapshot)
    restarted_registry.release(old_lease.lease_id)
    replacement = restarted_registry.acquire(spec.request, now=NOW, lease_seconds=120)
    assert replacement.lease_id != old_lease.lease_id

    restarted = BuildExecutionCoordinator(restarted_registry, AlwaysAvailable())
    with pytest.raises(BuildExecutionError, match="active lease identity does not match"):
        restarted.restore(coordinator_snapshot, now=NOW)


def test_restore_rejects_same_lease_identity_rebound_to_other_node() -> None:
    coordinator, registry, _ = _prepared()
    coordinator_snapshot = coordinator.snapshot()
    registry_snapshot = registry.snapshot()
    old_lease = registry_snapshot.leases[0]
    forged_lease = replace(old_lease, node_id="linux-2")
    forged_registry_snapshot = replace(registry_snapshot, leases=(forged_lease,))

    restarted_registry = ExecutionNodeRegistry()
    restarted_registry.restore(forged_registry_snapshot)
    restarted = BuildExecutionCoordinator(restarted_registry, AlwaysAvailable())

    with pytest.raises(BuildExecutionError, match="active lease identity does not match"):
        restarted.restore(coordinator_snapshot, now=NOW)


def test_restore_rejects_authorized_node_capability_drift() -> None:
    coordinator, registry, _ = _prepared()
    coordinator_snapshot = coordinator.snapshot()
    registry_snapshot = registry.snapshot()
    drifted_nodes = tuple(
        _node(node.identity.node_id, features=frozenset())
        if node.identity.node_id == "linux-1"
        else node
        for node in registry_snapshot.nodes
    )
    drifted_registry_snapshot = replace(registry_snapshot, nodes=drifted_nodes)

    restarted_registry = ExecutionNodeRegistry()
    restarted_registry.restore(drifted_registry_snapshot)
    restarted = BuildExecutionCoordinator(restarted_registry, AlwaysAvailable())

    with pytest.raises(BuildExecutionError, match="no longer satisfies request contract"):
        restarted.restore(coordinator_snapshot, now=NOW)


def test_restore_rejects_node_outside_project_authority() -> None:
    coordinator, registry, spec = _prepared()
    prepared = coordinator.get("work-1")
    forged_spec = replace(spec, authority=_authority("linux-2"))
    forged_record = replace(prepared, spec=forged_spec)
    forged_snapshot = replace(coordinator.snapshot(), records=(forged_record,))

    restarted_registry = ExecutionNodeRegistry()
    restarted_registry.restore(registry.snapshot())
    restarted = BuildExecutionCoordinator(restarted_registry, AlwaysAvailable())

    with pytest.raises(BuildExecutionError, match="outside project authority"):
        restarted.restore(forged_snapshot, now=NOW)


def test_restore_rejects_forged_dispatch_identity() -> None:
    coordinator, registry, _ = _prepared()
    coordinator.begin_dispatch("work-1", now=NOW)
    record = coordinator.get("work-1")
    assert record.dispatch is not None
    forged_dispatch = replace(record.dispatch, dispatch_id="dispatch:forged")
    forged_record = replace(record, dispatch=forged_dispatch)
    forged_snapshot = replace(coordinator.snapshot(), records=(forged_record,))

    restarted_registry = ExecutionNodeRegistry()
    restarted_registry.restore(registry.snapshot())
    restarted = BuildExecutionCoordinator(restarted_registry, AlwaysAvailable())

    with pytest.raises(BuildExecutionError, match="dispatch does not match"):
        restarted.restore(forged_snapshot, now=NOW)


@pytest.mark.parametrize("value", [True, 1.0, "120"])
def test_execution_lease_duration_requires_exact_integer(value: object) -> None:
    with pytest.raises(BuildExecutionError, match="positive integer"):
        replace(_spec("linux-1"), lease_seconds=value)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("succeeded", "uncertain"),
    [(1, False), (False, 0), ("yes", False)],
)
def test_execution_result_status_requires_exact_booleans(
    succeeded: object,
    uncertain: object,
) -> None:
    with pytest.raises(BuildExecutionError, match="exact booleans"):
        BuildExecutionResult(
            SHA,
            DIGEST,
            succeeded,  # type: ignore[arg-type]
            uncertain,  # type: ignore[arg-type]
            ("evidence://build",),
            NOW,
        )


def test_dispatch_attempt_rejects_bool_alias() -> None:
    with pytest.raises(BuildExecutionError, match="positive integer"):
        BuildExecutionDispatch(
            "dispatch:project-1:work-1:1",
            "project-1",
            "work-1",
            "linux-1",
            Platform.LINUX,
            SHA,
            ("python", "-m", "build"),
            _authority("linux-1"),
            True,
        )
