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

SHA_OLD = "a" * 40
SHA_NEW = "b" * 40
DIGEST_OLD = "1" * 64
DIGEST_NEW = "2" * 64
NOW = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)


class CrashOnSuccessorProvider:
    def __init__(self) -> None:
        self.deploy_calls: list[str] = []

    def deploy(self, intent: DeploymentIntent) -> ProviderDeploymentResult:
        self.deploy_calls.append(intent.release.source_sha)
        if intent.release.source_sha == SHA_NEW:
            raise SystemExit("synthetic process death after successor dispatch began")
        return ProviderDeploymentResult(True, False, ("deploy:old",))

    def health(self, intent: DeploymentIntent) -> HealthEvidence:
        return HealthEvidence(
            intent.environment.environment_id,
            intent.release.source_sha,
            True,
            ("health:old",),
            NOW,
            release=intent.release,
        )

    def rollback(
        self, intent: DeploymentIntent, previous_release_sha: str | None
    ) -> RollbackEvidence:
        raise AssertionError("rollback is not part of this crash boundary")

    def inspect(self, intent: DeploymentIntent) -> ProviderInspection:
        raise AssertionError("reconciliation is not part of this crash boundary")


def _intent(intent_id: str, *, sha: str, digest: str, version: str) -> DeploymentIntent:
    return DeploymentIntent(
        intent_id,
        "p1",
        EnvironmentIdentity("staging-1", "p1", EnvironmentTier.STAGING, "fake"),
        ReleaseRef("p1", version, sha, digest),
    )


def test_uncertain_successor_checkpoint_revokes_stale_release_authority(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    task = TaskQueue(store).create(
        workspace_id="ws-product",
        agent_id="product-factory",
        payload={"kind": "product_factory", "product_project_id": "p1"},
    )
    provider = CrashOnSuccessorProvider()
    host = ProductFactoryDeploymentCheckpointHost(store)
    fabric = DurableDeploymentFabric(
        provider,
        checkpoint_host=host,
        host_task_id=task.task_id,
        project_id="p1",
    )
    old_intent = _intent(
        "deploy:p1:old", sha=SHA_OLD, digest=DIGEST_OLD, version="1.0.0"
    )
    new_intent = _intent(
        "deploy:p1:new", sha=SHA_NEW, digest=DIGEST_NEW, version="2.0.0"
    )

    old = fabric.deploy(old_intent)
    assert old.state is DeploymentState.HEALTHY

    with pytest.raises(SystemExit, match="synthetic process death"):
        fabric.deploy(new_intent)

    persisted = host.latest_snapshot(host_task_id=task.task_id, project_id="p1")
    assert persisted is not None
    assert persisted.current_releases == ()
    assert persisted.exact_current_releases == ()
    assert persisted.healthy_staging == ()
    assert persisted.exact_healthy_staging == ()

    uncertain = next(
        record for record in persisted.records if record.intent.intent_id == new_intent.intent_id
    )
    assert uncertain.state is DeploymentState.UNCERTAIN
    assert uncertain.previous_release == old_intent.release
    assert uncertain.previous_release_sha == SHA_OLD

    restarted_store = SQLiteStore(store.path)
    restarted_store.initialize()
    restarted_provider = CrashOnSuccessorProvider()
    restarted = DurableDeploymentFabric.restore_latest(
        restarted_provider,
        checkpoint_host=ProductFactoryDeploymentCheckpointHost(restarted_store),
        host_task_id=task.task_id,
        project_id="p1",
    )

    replay = restarted.deploy(new_intent)
    assert replay.state is DeploymentState.UNCERTAIN
    assert replay.previous_release == old_intent.release
    assert restarted_provider.deploy_calls == []
    assert restarted.snapshot().current_releases == ()
    assert restarted.snapshot().healthy_staging == ()
