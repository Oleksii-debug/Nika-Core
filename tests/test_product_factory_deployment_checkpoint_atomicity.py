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
    ProductFactoryDeploymentCheckpointHost,
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


class UnhealthyExactRollbackProvider:
    def __init__(self) -> None:
        self.deploy_calls = 0
        self.rollback_calls = 0
        self.inspect_calls = 0
        self.current_release: ReleaseRef | None = None

    def deploy(self, intent: DeploymentIntent) -> ProviderDeploymentResult:
        self.deploy_calls += 1
        self.current_release = intent.release
        return ProviderDeploymentResult(True, False, ("deploy:ok",))

    def health(self, intent: DeploymentIntent) -> HealthEvidence:
        healthy = intent.release.version != "2.0.0"
        return HealthEvidence(
            intent.environment.environment_id,
            intent.release.source_sha,
            healthy,
            ("health:ok",),
            datetime.now(UTC),
            release=intent.release,
        )

    def rollback(
        self, intent: DeploymentIntent, previous_release_sha: str | None
    ) -> RollbackEvidence:
        raise AssertionError("exact rollback path is required")

    def rollback_exact(
        self,
        intent: DeploymentIntent,
        previous_release: ReleaseRef | None,
    ) -> RollbackEvidence:
        self.rollback_calls += 1
        self.current_release = previous_release
        return RollbackEvidence(
            intent.environment.environment_id,
            intent.release.source_sha,
            previous_release.source_sha if previous_release is not None else None,
            True,
            ("rollback:ok",),
            failed_release=intent.release,
            restored_release=previous_release,
        )

    def inspect(self, intent: DeploymentIntent) -> ProviderInspection:
        self.inspect_calls += 1
        release = self.current_release
        return ProviderInspection(
            release.source_sha if release is not None else None,
            None if release is None else release.version != "2.0.0",
            ("inspect:ok",),
            release=release,
        )


def _intent(intent_id: str, version: str, sha: str, digest: str) -> DeploymentIntent:
    return DeploymentIntent(
        intent_id,
        "p1",
        EnvironmentIdentity("staging-1", "p1", EnvironmentTier.STAGING, "fake"),
        ReleaseRef("p1", version, sha, digest),
    )


def test_failed_predispatch_checkpoint_preserves_predecessor_authority(
    tmp_path, monkeypatch
) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    task = TaskQueue(store).create(
        workspace_id="ws-product",
        agent_id="product-factory",
        payload={"kind": "product_factory", "product_project_id": "p1"},
    )
    provider = HealthyProvider()
    host = ProductFactoryDeploymentCheckpointHost(store)
    fabric = DurableDeploymentFabric(
        provider,
        checkpoint_host=host,
        host_task_id=task.task_id,
        project_id="p1",
    )
    predecessor = _intent("deploy:p1:1", "1.0.0", "a" * 40, "b" * 64)
    successor = _intent("deploy:p1:2", "2.0.0", "c" * 40, "d" * 64)

    fabric.deploy(predecessor)
    before = fabric.snapshot()
    assert provider.deploy_calls == 1

    def fail_save(**_kwargs) -> str:
        raise RuntimeError("synthetic checkpoint write failure")

    monkeypatch.setattr(host, "save", fail_save)
    with pytest.raises(RuntimeError, match="synthetic checkpoint write failure"):
        fabric.deploy(successor)

    assert provider.deploy_calls == 1
    assert fabric.snapshot() == before

def test_failed_prerollback_checkpoint_does_not_create_rollback_ack_loss_context(
    tmp_path, monkeypatch
) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    task = TaskQueue(store).create(
        workspace_id="ws-product",
        agent_id="product-factory",
        payload={"kind": "product_factory", "product_project_id": "p1"},
    )
    provider = UnhealthyExactRollbackProvider()
    host = ProductFactoryDeploymentCheckpointHost(store)
    fabric = DurableDeploymentFabric(
        provider,
        checkpoint_host=host,
        host_task_id=task.task_id,
        project_id="p1",
    )
    predecessor = _intent("deploy:p1:1", "1.0.0", "a" * 40, "b" * 64)
    successor = _intent("deploy:p1:2", "2.0.0", "c" * 40, "d" * 64)

    first = fabric.deploy(predecessor)
    assert first.state is DeploymentState.HEALTHY
    original_save = host.save

    def fail_prerollback_save(**kwargs) -> str:
        snapshot = kwargs["snapshot"]
        if any(
            record.intent.intent_id == successor.intent_id
            and record.state is DeploymentState.UNCERTAIN
            and record.health is not None
            and not record.health.healthy
            for record in snapshot.records
        ):
            raise RuntimeError("synthetic pre-rollback checkpoint failure")
        return original_save(**kwargs)

    monkeypatch.setattr(host, "save", fail_prerollback_save)
    with pytest.raises(RuntimeError, match="synthetic pre-rollback checkpoint failure"):
        fabric.deploy(successor)

    assert provider.deploy_calls == 2
    assert provider.rollback_calls == 0

    monkeypatch.setattr(host, "save", original_save)
    recovered = fabric.reconcile(successor.intent_id)

    assert recovered.state is DeploymentState.ROLLED_BACK
    assert provider.deploy_calls == 2
    assert provider.inspect_calls == 1
    assert provider.rollback_calls == 1
    assert provider.current_release == predecessor.release

