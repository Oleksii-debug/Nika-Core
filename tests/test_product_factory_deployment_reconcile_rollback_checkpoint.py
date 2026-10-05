from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.product_factory_deployment import (
    DeploymentFabricError,
    DeploymentIntent,
    DeploymentState,
    EnvironmentIdentity,
    EnvironmentTier,
    HealthEvidence,
    ProviderDeploymentResult,
    ProviderInspection,
    ReleaseRef,
    RollbackEvidence,
)
from nika_core.product_factory_deployment_checkpoint import (
    DurableDeploymentFabric,
    ProductFactoryDeploymentCheckpointHost,
)

SHA_A = "a" * 40
SHA_B = "b" * 40
DIGEST_A = "1" * 64
DIGEST_B = "2" * 64
NOW = datetime(2026, 9, 9, 9, 0, tzinfo=UTC)


def _release(version: str, sha: str, digest: str) -> ReleaseRef:
    return ReleaseRef("p1", version, sha, digest)


def _intent(intent_id: str, release: ReleaseRef) -> DeploymentIntent:
    return DeploymentIntent(
        intent_id,
        "p1",
        EnvironmentIdentity("staging-1", "p1", EnvironmentTier.STAGING, "fake"),
        release,
    )


def _setup(tmp_path):
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    task = TaskQueue(store).create(
        workspace_id="ws-product",
        agent_id="product-factory",
        payload={"kind": "product_factory", "product_project_id": "p1"},
    )
    return store, task.task_id


@dataclass
class _ReconcileRollbackProvider:
    uncertain_versions: set[str] = field(default_factory=lambda: {"2.0.0"})
    current: ReleaseRef | None = None
    deploy_calls: int = 0
    inspect_calls: int = 0
    rollback_targets: list[ReleaseRef | None] = field(default_factory=list)

    def deploy(self, intent: DeploymentIntent) -> ProviderDeploymentResult:
        self.deploy_calls += 1
        self.current = intent.release
        if intent.release.version in self.uncertain_versions:
            return ProviderDeploymentResult(True, True, ("deploy:uncertain",))
        return ProviderDeploymentResult(True, False, ("deploy:ok",))

    def health(self, intent: DeploymentIntent) -> HealthEvidence:
        return HealthEvidence(
            intent.environment.environment_id,
            intent.release.source_sha,
            True,
            ("health:ok",),
            NOW,
            release=intent.release,
        )

    def inspect(self, intent: DeploymentIntent) -> ProviderInspection:
        self.inspect_calls += 1
        assert self.current == intent.release
        return ProviderInspection(
            intent.release.source_sha,
            False,
            ("inspect:unhealthy",),
            release=intent.release,
        )

    def rollback(
        self,
        intent: DeploymentIntent,
        previous_release_sha: str | None,
    ) -> RollbackEvidence:
        raise AssertionError("exact previous release must use rollback_exact")

    def rollback_exact(
        self,
        intent: DeploymentIntent,
        previous_release: ReleaseRef | None,
    ) -> RollbackEvidence:
        self.rollback_targets.append(previous_release)
        self.current = previous_release
        return RollbackEvidence(
            intent.environment.environment_id,
            intent.release.source_sha,
            previous_release.source_sha if previous_release is not None else None,
            True,
            ("rollback:exact",),
            failed_release=intent.release,
            restored_release=previous_release,
        )


def test_reconcile_exact_rollback_restores_durable_previous_release_authority(tmp_path) -> None:
    release_a = _release("1.0.0", SHA_A, DIGEST_A)
    release_b = _release("2.0.0", SHA_B, DIGEST_B)
    store, task_id = _setup(tmp_path)
    provider = _ReconcileRollbackProvider()
    host = ProductFactoryDeploymentCheckpointHost(store)
    fabric = DurableDeploymentFabric(
        provider,
        checkpoint_host=host,
        host_task_id=task_id,
        project_id="p1",
    )

    first = fabric.deploy(_intent("deploy-a", release_a))
    uncertain = fabric.deploy(_intent("deploy-b", release_b))

    assert first.state is DeploymentState.HEALTHY
    assert uncertain.state is DeploymentState.UNCERTAIN
    assert uncertain.previous_release == release_a
    assert fabric.snapshot().exact_current_releases == ()
    assert fabric.snapshot().exact_healthy_staging == ()

    restarted_store = SQLiteStore(store.path)
    restarted_store.initialize()
    restarted_provider = _ReconcileRollbackProvider(current=release_b)
    restarted = DurableDeploymentFabric.restore_latest(
        restarted_provider,
        checkpoint_host=ProductFactoryDeploymentCheckpointHost(restarted_store),
        host_task_id=task_id,
        project_id="p1",
    )

    rolled_back = restarted.reconcile("deploy-b")

    assert rolled_back.state is DeploymentState.ROLLED_BACK
    assert rolled_back.previous_release == release_a
    assert restarted_provider.inspect_calls == 1
    assert restarted_provider.rollback_targets == [release_a]
    assert restarted_provider.current == release_a
    snapshot = restarted.snapshot()
    assert snapshot.exact_current_releases == (
        ("p1", "staging-1", "1.0.0", SHA_A, DIGEST_A),
    )
    assert snapshot.exact_healthy_staging == (
        ("p1", "1.0.0", SHA_A, DIGEST_A),
    )

    second_restart_store = SQLiteStore(store.path)
    second_restart_store.initialize()
    second_restart = DurableDeploymentFabric.restore_latest(
        _ReconcileRollbackProvider(current=release_a),
        checkpoint_host=ProductFactoryDeploymentCheckpointHost(second_restart_store),
        host_task_id=task_id,
        project_id="p1",
    )
    assert second_restart.snapshot().exact_current_releases == snapshot.exact_current_releases
    assert second_restart.snapshot().exact_healthy_staging == snapshot.exact_healthy_staging


class _ForgedInspection:
    def __init__(self, release: ReleaseRef) -> None:
        self.release_sha = release.source_sha
        self.release = release
        self.healthy = True
        self.evidence_refs = ("inspect:forged",)


@dataclass
class _RollbackAckLossForgedInspectionProvider(_ReconcileRollbackProvider):
    uncertain_versions: set[str] = field(default_factory=set)

    def health(self, intent: DeploymentIntent) -> HealthEvidence:
        return HealthEvidence(
            intent.environment.environment_id,
            intent.release.source_sha,
            intent.release.version == "1.0.0",
            ("health:exact",),
            NOW,
            release=intent.release,
        )

    def rollback_exact(
        self,
        intent: DeploymentIntent,
        previous_release: ReleaseRef | None,
    ) -> RollbackEvidence:
        self.rollback_targets.append(previous_release)
        self.current = previous_release
        raise RuntimeError("synthetic rollback acknowledgement loss")

    def inspect(self, intent: DeploymentIntent) -> ProviderInspection:
        self.inspect_calls += 1
        assert self.current is not None
        return _ForgedInspection(self.current)  # type: ignore[return-value]


def test_durable_reconcile_rejects_forged_inspection_carrier_after_restart(
    tmp_path,
) -> None:
    release_a = _release("1.0.0", SHA_A, DIGEST_A)
    release_b = _release("2.0.0", SHA_B, DIGEST_B)
    store, task_id = _setup(tmp_path)
    provider = _RollbackAckLossForgedInspectionProvider()
    host = ProductFactoryDeploymentCheckpointHost(store)
    fabric = DurableDeploymentFabric(
        provider,
        checkpoint_host=host,
        host_task_id=task_id,
        project_id="p1",
    )

    assert fabric.deploy(_intent("deploy-a-forged", release_a)).state is DeploymentState.HEALTHY
    uncertain = fabric.deploy(_intent("deploy-b-forged", release_b))
    assert uncertain.state is DeploymentState.UNCERTAIN
    assert provider.current == release_a

    restarted_store = SQLiteStore(store.path)
    restarted_store.initialize()
    restarted_provider = _RollbackAckLossForgedInspectionProvider(current=release_a)
    restarted = DurableDeploymentFabric.restore_latest(
        restarted_provider,
        checkpoint_host=ProductFactoryDeploymentCheckpointHost(restarted_store),
        host_task_id=task_id,
        project_id="p1",
    )

    with pytest.raises(DeploymentFabricError, match="inspection carrier is invalid"):
        restarted.reconcile("deploy-b-forged")

    record = next(
        item
        for item in restarted.snapshot().records
        if item.intent.intent_id == "deploy-b-forged"
    )
    assert record.state is DeploymentState.UNCERTAIN
    assert restarted_provider.inspect_calls == 1
    assert restarted_provider.rollback_targets == []

