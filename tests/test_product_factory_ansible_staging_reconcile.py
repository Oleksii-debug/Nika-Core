from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.product_factory_ansible_staging import (
    AnsibleRunnerConfig,
    AuthorizedAnsibleStagingAdapter,
    AuthorizedStagingTarget,
    RunnerExecution,
)
from nika_core.product_factory_deployment import (
    DeploymentIntent,
    DeploymentState,
    EnvironmentIdentity,
    EnvironmentTier,
    ReleaseRef,
)
from nika_core.product_factory_deployment_checkpoint import (
    DurableDeploymentFabric,
    ProductFactoryDeploymentCheckpointHost,
)

SHA = "a" * 40
DIGEST_A = "1" * 64
DIGEST_B = "2" * 64
NOW = datetime(2026, 9, 9, 18, 0, tzinfo=UTC)


def _release(version: str, digest: str) -> ReleaseRef:
    return ReleaseRef("project-exact", version, SHA, digest)


def _intent(intent_id: str, release: ReleaseRef) -> DeploymentIntent:
    return DeploymentIntent(
        intent_id,
        "project-exact",
        EnvironmentIdentity(
            "staging-exact",
            "project-exact",
            EnvironmentTier.STAGING,
            "provider://exact",
        ),
        release,
    )


@dataclass
class _AckLossRunner:
    current: ReleaseRef | None = None
    deploy_calls: int = 0
    rollback_calls: int = 0
    inspect_calls: int = 0

    def execute(
        self,
        *,
        private_data_dir: Path,
        playbook: str,
        inventory: str,
        ident: str,
        extravars: Mapping[str, object],
    ) -> RunnerExecution:
        del private_data_dir, playbook, inventory, ident
        operation = extravars["nika_pf3_operation"]
        release = ReleaseRef(
            str(extravars["nika_project_id"]),
            str(extravars["nika_release_version"]),
            str(extravars["nika_release_sha"]),
            str(extravars["nika_artifact_digest"]),
        )
        if operation == "deploy":
            self.deploy_calls += 1
            self.current = release
            return RunnerExecution(
                "successful",
                0,
                {"applied": True},
                f"runner:deploy:{release.version}",
            )
        if operation == "health":
            return RunnerExecution(
                "successful",
                0,
                {
                    "release_version": release.version,
                    "release_sha": release.source_sha,
                    "artifact_digest": release.artifact_digest,
                    "healthy": release.version == "1.0.0",
                    "observed_at": NOW.isoformat(),
                },
                f"runner:health:{release.version}",
            )
        if operation == "rollback":
            self.rollback_calls += 1
            self.current = ReleaseRef(
                str(extravars["nika_project_id"]),
                str(extravars["nika_previous_release_version"]),
                str(extravars["nika_previous_release_sha"]),
                str(extravars["nika_previous_artifact_digest"]),
            )
            raise ConnectionError("synthetic rollback acknowledgement loss")
        if operation == "inspect":
            self.inspect_calls += 1
            assert self.current is not None
            return RunnerExecution(
                "successful",
                0,
                {
                    "release_version": self.current.version,
                    "release_sha": self.current.source_sha,
                    "artifact_digest": self.current.artifact_digest,
                    "healthy": True,
                },
                "runner:inspect:previous",
            )
        raise AssertionError(f"unexpected operation: {operation}")


def _adapter(runner: _AckLossRunner) -> AuthorizedAnsibleStagingAdapter:
    return AuthorizedAnsibleStagingAdapter(
        AuthorizedStagingTarget(
            "project-exact",
            "staging-exact",
            "provider://exact",
            "inventory/staging.ini",
            "approval-ref:staging-exact",
        ),
        AnsibleRunnerConfig(Path.cwd().resolve() / "trusted-ansible"),
        runner,
    )


def test_same_sha_previous_release_reconciles_after_rollback_ack_loss_and_restart(
    tmp_path: Path,
) -> None:
    release_a = _release("1.0.0", DIGEST_A)
    release_b = _release("2.0.0", DIGEST_B)
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    task = TaskQueue(store).create(
        workspace_id="ws-product",
        agent_id="product-factory",
        payload={"kind": "product_factory", "product_project_id": "project-exact"},
    )
    host = ProductFactoryDeploymentCheckpointHost(store)
    runner = _AckLossRunner()
    fabric = DurableDeploymentFabric(
        _adapter(runner),
        checkpoint_host=host,
        host_task_id=task.task_id,
        project_id="project-exact",
    )

    first = fabric.deploy(_intent("deploy-a", release_a))
    second = fabric.deploy(_intent("deploy-b", release_b))

    assert first.state is DeploymentState.HEALTHY
    assert second.state is DeploymentState.UNCERTAIN
    assert second.previous_release == release_a
    assert runner.current == release_a
    assert runner.rollback_calls == 1

    restarted_store = SQLiteStore(store.path)
    restarted_store.initialize()
    restarted_runner = _AckLossRunner(current=release_a)
    restarted = DurableDeploymentFabric.restore_latest(
        _adapter(restarted_runner),
        checkpoint_host=ProductFactoryDeploymentCheckpointHost(restarted_store),
        host_task_id=task.task_id,
        project_id="project-exact",
    )

    reconciled = restarted.reconcile("deploy-b")

    assert reconciled.state is DeploymentState.ROLLED_BACK
    assert reconciled.previous_release == release_a
    assert reconciled.rollback is not None
    assert reconciled.rollback.restored_release == release_a
    assert restarted_runner.deploy_calls == 0
    assert restarted_runner.rollback_calls == 0
    assert restarted_runner.inspect_calls == 1
    assert restarted.snapshot().exact_current_releases == (
        ("project-exact", "staging-exact", "1.0.0", SHA, DIGEST_A),
    )
