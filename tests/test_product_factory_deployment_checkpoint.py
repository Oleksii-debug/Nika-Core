from __future__ import annotations

from datetime import UTC, datetime

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.product_factory_deployment import (
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
    ProductFactoryDeploymentCheckpointError,
    ProductFactoryDeploymentCheckpointHost,
)

SHA = "a" * 40
DIGEST = "b" * 64


class CrashAfterDispatchProvider:
    def __init__(self) -> None:
        self.deploy_calls = 0
        self.inspect_calls = 0

    def deploy(self, intent: DeploymentIntent) -> ProviderDeploymentResult:
        self.deploy_calls += 1
        raise SystemExit("synthetic process death after provider dispatch began")

    def health(self, intent: DeploymentIntent) -> HealthEvidence:
        raise AssertionError("health must not run after process death")

    def rollback(
        self, intent: DeploymentIntent, previous_release_sha: str | None
    ) -> RollbackEvidence:
        raise AssertionError("rollback must not run after process death")

    def inspect(self, intent: DeploymentIntent) -> ProviderInspection:
        self.inspect_calls += 1
        return ProviderInspection(
            intent.release.source_sha,
            None,
            ("inspect:uncertain",),
            release=intent.release,
        )


class HealthyProvider:
    def __init__(self) -> None:
        self.deploy_calls = 0

    def deploy(self, intent: DeploymentIntent) -> ProviderDeploymentResult:
        self.deploy_calls += 1
        return ProviderDeploymentResult(True, False, ("deploy:ok",))

    def health(self, intent: DeploymentIntent) -> HealthEvidence:
        return HealthEvidence(
            intent.environment.environment_id,
            intent.release.source_sha,
            True,
            ("health:ok",),
            datetime.now(UTC),
            release=intent.release,
        )

    def rollback(
        self, intent: DeploymentIntent, previous_release_sha: str | None
    ) -> RollbackEvidence:
        raise AssertionError("healthy deployment must not rollback")

    def inspect(self, intent: DeploymentIntent) -> ProviderInspection:
        return ProviderInspection(
            intent.release.source_sha,
            True,
            ("inspect:healthy",),
            release=intent.release,
        )


def _intent() -> DeploymentIntent:
    release = ReleaseRef("p1", "1.0.0", SHA, DIGEST)
    environment = EnvironmentIdentity("staging-1", "p1", EnvironmentTier.STAGING, "fake")
    return DeploymentIntent("deploy:p1:1", "p1", environment, release)


def _setup(tmp_path, *, task_project: str = "p1"):
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    task = TaskQueue(store).create(
        workspace_id="ws-product",
        agent_id="product-factory",
        payload={"kind": "product_factory", "product_project_id": task_project},
    )
    return store, task.task_id


def test_predispatch_uncertain_survives_process_death_and_blocks_replay(tmp_path) -> None:
    store, task_id = _setup(tmp_path)
    provider = CrashAfterDispatchProvider()
    host = ProductFactoryDeploymentCheckpointHost(store)
    fabric = DurableDeploymentFabric(
        provider,
        checkpoint_host=host,
        host_task_id=task_id,
        project_id="p1",
    )
    intent = _intent()

    with pytest.raises(SystemExit, match="synthetic process death"):
        fabric.deploy(intent)

    assert provider.deploy_calls == 1
    persisted = host.latest_snapshot(host_task_id=task_id, project_id="p1")
    assert persisted is not None
    assert len(persisted.records) == 1
    assert persisted.records[0].state is DeploymentState.UNCERTAIN
    assert persisted.records[0].provider_evidence_refs == ()

    restarted_store = SQLiteStore(store.path)
    restarted_store.initialize()
    restarted_provider = CrashAfterDispatchProvider()
    restarted = DurableDeploymentFabric.restore_latest(
        restarted_provider,
        checkpoint_host=ProductFactoryDeploymentCheckpointHost(restarted_store),
        host_task_id=task_id,
        project_id="p1",
    )

    restored = restarted.deploy(intent)
    assert restored.state is DeploymentState.UNCERTAIN
    assert restarted_provider.deploy_calls == 0
    inspected = restarted.reconcile(intent.intent_id)
    assert inspected.state is DeploymentState.UNCERTAIN
    assert restarted_provider.inspect_calls == 1


def test_checkpoint_failure_prevents_provider_dispatch(tmp_path) -> None:
    store, task_id = _setup(tmp_path, task_project="another-project")
    provider = HealthyProvider()
    fabric = DurableDeploymentFabric(
        provider,
        checkpoint_host=ProductFactoryDeploymentCheckpointHost(store),
        host_task_id=task_id,
        project_id="p1",
    )

    with pytest.raises(
        ProductFactoryDeploymentCheckpointError,
        match="host task ProductProject identity",
    ):
        fabric.deploy(_intent())

    assert provider.deploy_calls == 0


def test_healthy_state_round_trips_and_duplicate_does_not_redispatch(tmp_path) -> None:
    store, task_id = _setup(tmp_path)
    provider = HealthyProvider()
    host = ProductFactoryDeploymentCheckpointHost(store)
    fabric = DurableDeploymentFabric(
        provider,
        checkpoint_host=host,
        host_task_id=task_id,
        project_id="p1",
    )
    intent = _intent()

    completed = fabric.deploy(intent)
    assert completed.state is DeploymentState.HEALTHY
    assert provider.deploy_calls == 1

    restarted_store = SQLiteStore(store.path)
    restarted_store.initialize()
    restarted_provider = HealthyProvider()
    restarted = DurableDeploymentFabric.restore_latest(
        restarted_provider,
        checkpoint_host=ProductFactoryDeploymentCheckpointHost(restarted_store),
        host_task_id=task_id,
        project_id="p1",
    )

    duplicate = restarted.deploy(intent)
    assert duplicate.state is DeploymentState.HEALTHY
    assert duplicate.intent.release == intent.release
    assert restarted_provider.deploy_calls == 0

    with restarted_store.connection() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM checkpoints WHERE task_id = ? AND stage = ?",
            (task_id, "product_factory.deployment.v1"),
        ).fetchone()[0]
    assert count >= 2
