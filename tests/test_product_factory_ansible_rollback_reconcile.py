from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.product_factory_ansible_staging import (
    AnsibleRunnerConfig,
    AuthorizedAnsibleStagingAdapter,
    AuthorizedStagingTarget,
    RunnerExecution,
)
from nika_core.product_factory_deployment import (
    DeploymentFabricError,
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


def _sha(value: int) -> str:
    return f"{value:040x}"[-40:]


def _digest(value: int) -> str:
    return f"{value:064x}"[-64:]


def _release(value: int, version: str) -> ReleaseRef:
    return ReleaseRef("p-social", version, _sha(value), _digest(value))


def _intent(intent_id: str, release: ReleaseRef) -> DeploymentIntent:
    return DeploymentIntent(
        intent_id,
        "p-social",
        EnvironmentIdentity(
            "staging-eu",
            "p-social",
            EnvironmentTier.STAGING,
            "ansible:staging-eu",
        ),
        release,
    )


def _contract(release: ReleaseRef, *, healthy: bool | None = None) -> dict[str, object]:
    result: dict[str, object] = {
        "release_version": release.version,
        "release_sha": release.source_sha,
        "artifact_digest": release.artifact_digest,
    }
    if healthy is not None:
        result["healthy"] = healthy
    return result


class SequencedRunner:
    def __init__(self, outcomes: list[RunnerExecution | BaseException]) -> None:
        self.outcomes = list(outcomes)
        self.operations: list[str] = []

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
        self.operations.append(str(extravars["nika_pf3_operation"]))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _execution(operation: str, contract: Mapping[str, object]) -> RunnerExecution:
    return RunnerExecution(
        "successful",
        0,
        contract,
        f"ansible-runner:evidence-{operation}",
    )


def _adapter(runner: SequencedRunner) -> AuthorizedAnsibleStagingAdapter:
    return AuthorizedAnsibleStagingAdapter(
        AuthorizedStagingTarget(
            "p-social",
            "staging-eu",
            "ansible:staging-eu",
            "inventory/staging.ini",
            "approval-ref:pf3-staging-eu",
        ),
        AnsibleRunnerConfig(Path.cwd().resolve() / "trusted-nika-ansible"),
        runner,
    )


def _setup(tmp_path) -> tuple[SQLiteStore, str]:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    task = TaskQueue(store).create(
        workspace_id="ws-product",
        agent_id="product-factory",
        payload={"kind": "product_factory", "product_project_id": "p-social"},
    )
    return store, task.task_id


def test_restart_reconcile_accepts_exact_previous_release_after_rollback_ack_loss(
    tmp_path,
) -> None:
    release_a = _release(1, "1.0.0")
    release_b = _release(2, "2.0.0")
    runner = SequencedRunner(
        [
            _execution("deploy-a", {"applied": True}),
            _execution(
                "health-a",
                {
                    **_contract(release_a, healthy=True),
                    "observed_at": "2026-09-09T00:00:00Z",
                },
            ),
            _execution("deploy-b", {"applied": True}),
            _execution(
                "health-b",
                {
                    **_contract(release_b, healthy=False),
                    "observed_at": "2026-09-09T00:00:01Z",
                },
            ),
            ConnectionError("synthetic rollback acknowledgement loss"),
        ]
    )
    store, task_id = _setup(tmp_path)
    host = ProductFactoryDeploymentCheckpointHost(store)
    fabric = DurableDeploymentFabric(
        _adapter(runner),
        checkpoint_host=host,
        host_task_id=task_id,
        project_id="p-social",
    )

    assert fabric.deploy(_intent("deploy-a", release_a)).state is DeploymentState.HEALTHY
    uncertain = fabric.deploy(_intent("deploy-b", release_b))
    assert uncertain.state is DeploymentState.UNCERTAIN
    assert runner.operations == ["deploy", "health", "deploy", "health", "rollback"]

    restarted_store = SQLiteStore(store.path)
    restarted_store.initialize()
    inspect_runner = SequencedRunner(
        [
            _execution(
                "inspect-a",
                _contract(release_a, healthy=True),
            )
        ]
    )
    restarted = DurableDeploymentFabric.restore_latest(
        _adapter(inspect_runner),
        checkpoint_host=ProductFactoryDeploymentCheckpointHost(restarted_store),
        host_task_id=task_id,
        project_id="p-social",
    )

    reconciled = restarted.reconcile("deploy-b")
    assert reconciled.state is DeploymentState.ROLLED_BACK
    assert reconciled.previous_release == release_a
    assert reconciled.rollback is not None
    assert reconciled.rollback.restored_release == release_a
    assert inspect_runner.operations == ["inspect"]


def test_unexpected_exact_release_remains_fail_closed_in_fabric(tmp_path) -> None:
    release_b = _release(2, "2.0.0")
    unexpected = _release(3, "3.0.0")
    runner = SequencedRunner(
        [
            RunnerExecution("timeout", 254, None, "ansible-runner:deploy-uncertain"),
            _execution("inspect-unexpected", _contract(unexpected, healthy=True)),
        ]
    )
    store, task_id = _setup(tmp_path)
    fabric = DurableDeploymentFabric(
        _adapter(runner),
        checkpoint_host=ProductFactoryDeploymentCheckpointHost(store),
        host_task_id=task_id,
        project_id="p-social",
    )

    intent = _intent("deploy-b", release_b)
    assert fabric.deploy(intent).state is DeploymentState.UNCERTAIN

    with pytest.raises(DeploymentFabricError, match="different release"):
        fabric.reconcile(intent.intent_id)

    assert runner.operations == ["deploy", "inspect"]
