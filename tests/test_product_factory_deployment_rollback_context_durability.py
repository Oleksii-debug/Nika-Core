from __future__ import annotations

from datetime import UTC, datetime

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

SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40
SHA_D = "d" * 40
DIGEST_A = "1" * 64
DIGEST_B = "2" * 64
DIGEST_C = "3" * 64
DIGEST_D = "4" * 64


class MultiEnvironmentRollbackAckLossProvider:
    def __init__(self, previous_release: ReleaseRef) -> None:
        self.previous_release = previous_release
        self.deploy_calls = 0
        self.rollback_calls = 0
        self.inspect_calls = 0

    def deploy(self, intent: DeploymentIntent) -> ProviderDeploymentResult:
        self.deploy_calls += 1
        return ProviderDeploymentResult(True, False, (f"deploy:{intent.intent_id}",))

    def health(self, intent: DeploymentIntent) -> HealthEvidence:
        healthy = (
            intent.release == self.previous_release
            or intent.environment.environment_id != "staging-1"
        )
        return HealthEvidence(
            intent.environment.environment_id,
            intent.release.source_sha,
            healthy,
            (f"health:{intent.intent_id}",),
            datetime.now(UTC),
            release=intent.release,
        )

    def rollback(
        self, intent: DeploymentIntent, previous_release_sha: str | None
    ) -> RollbackEvidence:
        raise AssertionError("exact rollback must be used when prior exact release exists")

    def rollback_exact(
        self, intent: DeploymentIntent, previous_release: ReleaseRef | None
    ) -> RollbackEvidence:
        self.rollback_calls += 1
        assert intent.intent_id == "deploy:p1:b"
        assert previous_release == self.previous_release
        raise ConnectionError("synthetic acknowledgement loss after external rollback")

    def inspect(self, intent: DeploymentIntent) -> ProviderInspection:
        self.inspect_calls += 1
        assert intent.intent_id == "deploy:p1:b"
        return ProviderInspection(
            self.previous_release.source_sha,
            True,
            ("inspect:previous-release",),
            release=self.previous_release,
        )


def _intent(
    intent_id: str,
    release: ReleaseRef,
    *,
    environment_id: str,
) -> DeploymentIntent:
    environment = EnvironmentIdentity(
        environment_id,
        "p1",
        EnvironmentTier.STAGING,
        "fake",
    )
    return DeploymentIntent(intent_id, "p1", environment, release)


def _setup(tmp_path):
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    task = TaskQueue(store).create(
        workspace_id="ws-product",
        agent_id="product-factory",
        payload={"kind": "product_factory", "product_project_id": "p1"},
    )
    return store, task.task_id


def test_rollback_context_survives_multiple_later_checkpoints_until_terminal_resolution(
    tmp_path,
) -> None:
    previous = ReleaseRef("p1", "1.0.0", SHA_A, DIGEST_A)
    candidate = ReleaseRef("p1", "2.0.0", SHA_B, DIGEST_B)
    unrelated_c = ReleaseRef("p1", "3.0.0", SHA_C, DIGEST_C)
    unrelated_d = ReleaseRef("p1", "4.0.0", SHA_D, DIGEST_D)
    store, task_id = _setup(tmp_path)
    host = ProductFactoryDeploymentCheckpointHost(store)
    provider = MultiEnvironmentRollbackAckLossProvider(previous)
    fabric = DurableDeploymentFabric(
        provider,
        checkpoint_host=host,
        host_task_id=task_id,
        project_id="p1",
    )

    assert fabric.deploy(
        _intent("deploy:p1:a", previous, environment_id="staging-1")
    ).state is DeploymentState.HEALTHY
    uncertain = fabric.deploy(
        _intent("deploy:p1:b", candidate, environment_id="staging-1")
    )
    assert uncertain.state is DeploymentState.UNCERTAIN
    assert provider.rollback_calls == 1

    assert fabric.deploy(
        _intent("deploy:p1:c", unrelated_c, environment_id="staging-2")
    ).state is DeploymentState.HEALTHY
    assert fabric.deploy(
        _intent("deploy:p1:d", unrelated_d, environment_id="staging-3")
    ).state is DeploymentState.HEALTHY

    latest = host.latest_snapshot(host_task_id=task_id, project_id="p1")
    assert latest is not None
    unresolved = next(
        record for record in latest.records if record.intent.intent_id == "deploy:p1:b"
    )
    assert unresolved.state is DeploymentState.UNCERTAIN
    assert unresolved.health is not None
    assert not unresolved.health.healthy

    restarted_store = SQLiteStore(store.path)
    restarted_store.initialize()
    restarted_provider = MultiEnvironmentRollbackAckLossProvider(previous)
    restarted = DurableDeploymentFabric.restore_latest(
        restarted_provider,
        checkpoint_host=ProductFactoryDeploymentCheckpointHost(restarted_store),
        host_task_id=task_id,
        project_id="p1",
    )

    duplicate = restarted.deploy(
        _intent("deploy:p1:b", candidate, environment_id="staging-1")
    )
    assert duplicate.state is DeploymentState.UNCERTAIN
    assert restarted_provider.deploy_calls == 0
    assert restarted_provider.rollback_calls == 0

    reconciled = restarted.reconcile("deploy:p1:b")
    assert reconciled.state is DeploymentState.ROLLED_BACK
    assert reconciled.rollback is not None
    assert reconciled.rollback.restored_release == previous
    assert restarted_provider.inspect_calls == 1
    assert restarted_provider.deploy_calls == 0
    assert restarted_provider.rollback_calls == 0
    assert "deploy:p1:b" not in restarted._rollback_health_by_intent

    final_store = SQLiteStore(store.path)
    final_store.initialize()
    final_provider = MultiEnvironmentRollbackAckLossProvider(previous)
    final = DurableDeploymentFabric.restore_latest(
        final_provider,
        checkpoint_host=ProductFactoryDeploymentCheckpointHost(final_store),
        host_task_id=task_id,
        project_id="p1",
    )
    assert "deploy:p1:b" not in final._rollback_health_by_intent
    final_record = final.deploy(
        _intent("deploy:p1:b", candidate, environment_id="staging-1")
    )
    assert final_record.state is DeploymentState.ROLLED_BACK
    assert final_provider.deploy_calls == 0
    assert final_provider.rollback_calls == 0
    assert final_provider.inspect_calls == 0
