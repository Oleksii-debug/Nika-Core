from __future__ import annotations

from datetime import UTC, datetime

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.product_factory_deployment import (
    DeploymentIntent,
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
