from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from nika_core.product_factory_deployment import (
    DeploymentFabric,
    DeploymentFabricError,
    DeploymentFabricSnapshot,
    DeploymentIntent,
    DeploymentState,
    EnvironmentIdentity,
    EnvironmentTier,
    ExecutionNodeRegistry,
    ExecutionRegistrySnapshot,
    HealthEvidence,
    ProviderDeploymentResult,
    ProviderInspection,
    ReleaseRef,
    RollbackEvidence,
    WorkLease,
    local_windows_node,
)

NOW = datetime(2026, 8, 21, 1, 0, tzinfo=UTC)


def _sha(value: int) -> str:
    return f"{value:040x}"[-40:]


def _digest(value: int) -> str:
    return f"{value:064x}"[-64:]


def _intent(
    project_id: str,
    intent_id: str,
    sha: int,
    *,
    environment_id: str = "shared-staging",
    tier: EnvironmentTier = EnvironmentTier.STAGING,
) -> DeploymentIntent:
    return DeploymentIntent(
        intent_id,
        project_id,
        EnvironmentIdentity(
            environment_id,
            project_id,
            tier,
            f"provider:{project_id}:{environment_id}",
        ),
        ReleaseRef(project_id, f"release-{sha}", _sha(sha), _digest(sha)),
    )


@dataclass
class FakeProvider:
    unhealthy: set[str] = field(default_factory=set)
    uncertain: set[str] = field(default_factory=set)
    inspections: dict[str, ProviderInspection] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.rollback_previous: list[tuple[str, str | None]] = []

    def deploy(self, intent: DeploymentIntent) -> ProviderDeploymentResult:
        if intent.intent_id in self.uncertain:
            return ProviderDeploymentResult(False, True, (f"deploy:{intent.intent_id}",))
        return ProviderDeploymentResult(True, False, (f"deploy:{intent.intent_id}",))

    def health(self, intent: DeploymentIntent) -> HealthEvidence:
        return HealthEvidence(
            intent.environment.environment_id,
            intent.release.source_sha,
            intent.release.source_sha not in self.unhealthy,
            (f"health:{intent.intent_id}",),
            NOW,
        )

    def rollback(
        self,
        intent: DeploymentIntent,
        previous_release_sha: str | None,
    ) -> RollbackEvidence:
        self.rollback_previous.append((intent.project_id, previous_release_sha))
        return RollbackEvidence(
            intent.environment.environment_id,
            intent.release.source_sha,
            previous_release_sha,
            True,
            (f"rollback:{intent.intent_id}",),
        )

    def inspect(self, intent: DeploymentIntent) -> ProviderInspection:
        return self.inspections.get(
            intent.intent_id,
            ProviderInspection(
                intent.release.source_sha,
                True,
                (f"inspect:{intent.intent_id}",),
            ),
        )


def test_current_release_is_scoped_by_project_and_environment() -> None:
    provider = FakeProvider()
    fabric = DeploymentFabric(provider)

    first = fabric.deploy(_intent("project-a", "deploy-a", 1))
    second = fabric.deploy(_intent("project-b", "deploy-b", 2))

    assert first.previous_release_sha is None
    assert second.previous_release_sha is None
    assert fabric.snapshot().current_releases == (
        ("project-a", "shared-staging", _sha(1)),
        ("project-b", "shared-staging", _sha(2)),
    )


def test_rollback_never_uses_another_projects_release() -> None:
    provider = FakeProvider(unhealthy={_sha(2)})
    fabric = DeploymentFabric(provider)
    fabric.deploy(_intent("project-a", "deploy-a", 1))
    failed = fabric.deploy(_intent("project-b", "deploy-b", 2))

    assert failed.state is DeploymentState.ROLLED_BACK
    assert failed.previous_release_sha is None
    assert provider.rollback_previous == [("project-b", None)]


def test_exact_previous_release_survives_restart_for_same_project_environment() -> None:
    provider = FakeProvider(unhealthy={_sha(2)})
    fabric = DeploymentFabric(provider)
    fabric.deploy(_intent("project-a", "deploy-v1", 1))
    snapshot = fabric.snapshot()

    restarted = DeploymentFabric(provider)
    restarted.restore(snapshot)
    failed = restarted.deploy(_intent("project-a", "deploy-v2", 2))

    assert failed.previous_release_sha == _sha(1)
    assert provider.rollback_previous == [("project-a", _sha(1))]


def test_restore_rejects_current_release_not_backed_by_healthy_record() -> None:
    provider = FakeProvider()
    fabric = DeploymentFabric(provider)
    fabric.deploy(_intent("project-a", "deploy-a", 1))
    snapshot = fabric.snapshot()
    corrupt = DeploymentFabricSnapshot(
        snapshot.records,
        snapshot.healthy_staging,
        (("project-a", "shared-staging", _sha(9)),),
    )

    with pytest.raises(DeploymentFabricError, match="not backed"):
        DeploymentFabric(provider).restore(corrupt)


def test_restore_accepts_unambiguous_legacy_current_release_shape() -> None:
    provider = FakeProvider()
    fabric = DeploymentFabric(provider)
    fabric.deploy(_intent("project-a", "deploy-a", 1))
    snapshot = fabric.snapshot()
    legacy = DeploymentFabricSnapshot(
        snapshot.records,
        snapshot.healthy_staging,
        (("shared-staging", _sha(1)),),
    )

    restarted = DeploymentFabric(provider)
    restarted.restore(legacy)

    assert restarted.snapshot().current_releases == (
        ("project-a", "shared-staging", _sha(1)),
    )


def test_restore_rejects_ambiguous_legacy_environment_identity() -> None:
    provider = FakeProvider()
    fabric = DeploymentFabric(provider)
    fabric.deploy(_intent("project-a", "deploy-a", 1))
    fabric.deploy(_intent("project-b", "deploy-b", 1))
    snapshot = fabric.snapshot()
    legacy = DeploymentFabricSnapshot(
        snapshot.records,
        snapshot.healthy_staging,
        (("shared-staging", _sha(1)),),
    )

    with pytest.raises(DeploymentFabricError, match="ambiguous"):
        DeploymentFabric(provider).restore(legacy)


def test_uncertain_reconcile_after_restart_is_project_isolated() -> None:
    provider = FakeProvider(uncertain={"uncertain-a"})
    fabric = DeploymentFabric(provider)
    healthy_b = fabric.deploy(_intent("project-b", "healthy-b", 2))
    uncertain_a = fabric.deploy(_intent("project-a", "uncertain-a", 1))
    assert healthy_b.state is DeploymentState.HEALTHY
    assert uncertain_a.state is DeploymentState.UNCERTAIN

    restarted = DeploymentFabric(provider)
    restarted.restore(fabric.snapshot())
    reconciled = restarted.reconcile("uncertain-a")

    assert reconciled.state is DeploymentState.HEALTHY
    assert restarted.snapshot().current_releases == (
        ("project-a", "shared-staging", _sha(1)),
        ("project-b", "shared-staging", _sha(2)),
    )


def test_fifty_projects_same_environment_survive_restart_without_collision() -> None:
    provider = FakeProvider()
    fabric = DeploymentFabric(provider)

    for index in range(1, 51):
        project_id = f"project-{index:02d}"
        record = fabric.deploy(_intent(project_id, f"deploy-{index:02d}", index))
        assert record.previous_release_sha is None
        assert record.state is DeploymentState.HEALTHY

    snapshot = fabric.snapshot()
    assert len(snapshot.current_releases) == 50

    restarted = DeploymentFabric(provider)
    restarted.restore(snapshot)
    assert restarted.snapshot() == snapshot


def test_execution_restore_rejects_two_active_leases_on_one_node() -> None:
    node = local_windows_node()
    snapshot = ExecutionRegistrySnapshot(
        (node,),
        (
            WorkLease(
                "lease-1",
                "project-a",
                "work-a",
                node.identity.node_id,
                NOW,
                NOW + timedelta(minutes=5),
            ),
            WorkLease(
                "lease-2",
                "project-b",
                "work-b",
                node.identity.node_id,
                NOW,
                NOW + timedelta(minutes=5),
            ),
        ),
        3,
    )

    with pytest.raises(DeploymentFabricError, match="multiple active leases"):
        ExecutionNodeRegistry().restore(snapshot)


def test_execution_restore_rejects_invalid_lease_time_order() -> None:
    node = local_windows_node()
    snapshot = ExecutionRegistrySnapshot(
        (node,),
        (
            WorkLease(
                "lease-1",
                "project-a",
                "work-a",
                node.identity.node_id,
                NOW,
                NOW - timedelta(seconds=1),
            ),
        ),
        2,
    )

    with pytest.raises(DeploymentFabricError, match="expiry"):
        ExecutionNodeRegistry().restore(snapshot)


@pytest.mark.parametrize(
    ("environment_id", "project_id", "provider_ref"),
    [
        ("", "project-a", "provider:a"),
        ("staging", "", "provider:a"),
        ("staging", "project-a", ""),
    ],
)
def test_environment_identity_rejects_empty_scope(
    environment_id: str,
    project_id: str,
    provider_ref: str,
) -> None:
    with pytest.raises(DeploymentFabricError, match="must not be empty"):
        EnvironmentIdentity(
            environment_id,
            project_id,
            EnvironmentTier.STAGING,
            provider_ref,
        )
