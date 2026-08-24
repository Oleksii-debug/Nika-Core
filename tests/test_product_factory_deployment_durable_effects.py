from __future__ import annotations

import inspect
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.product_command.deployment_adapter import deployment_status_entries
from nika_core.product_factory_deployment import (
    DeploymentFabric,
    DeploymentFabricError,
    DeploymentIntent,
    DeploymentProviderPort,
    DeploymentState,
    EnvironmentIdentity,
    EnvironmentTier,
    HealthEvidence,
    ProviderDeploymentResult,
    ProviderInspection,
    ReleaseRef,
    RollbackEvidence,
)
from nika_core.product_factory_deployment_journal import SQLiteDeploymentEffectJournal

SHA = "a" * 40
DIGEST_A = "1" * 64
DIGEST_B = "2" * 64
DIGEST_C = "3" * 64
NOW = datetime(2026, 8, 24, 6, 0, tzinfo=UTC)


def _release(version: str, digest: str) -> ReleaseRef:
    return ReleaseRef("project-durable", version, SHA, digest)


def _intent(intent_id: str, release: ReleaseRef) -> DeploymentIntent:
    return DeploymentIntent(
        intent_id,
        "project-durable",
        EnvironmentIdentity(
            "staging-durable",
            "project-durable",
            EnvironmentTier.STAGING,
            "provider://durable",
        ),
        release,
    )


def _store_and_task(tmp_path):
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    task = TaskQueue(store).create(
        workspace_id="ws-product",
        agent_id="product-factory",
        payload={
            "kind": "product_factory",
            "product_project_id": "project-durable",
        },
    )
    return store, task.task_id


@dataclass
class _CrashAfterApplyProvider:
    current: ReleaseRef | None = None
    deploy_calls: int = 0
    rollback_calls: int = 0
    crash_on_deploy: bool = True

    def deploy(self, intent: DeploymentIntent) -> ProviderDeploymentResult:
        self.deploy_calls += 1
        self.current = intent.release
        if self.crash_on_deploy:
            raise SystemExit("simulated process loss after provider mutation")
        return ProviderDeploymentResult(
            True,
            False,
            ("deploy://applied",),
            release=intent.release,
        )

    def health(self, intent: DeploymentIntent) -> HealthEvidence:
        return HealthEvidence(
            intent.environment.environment_id,
            intent.release.source_sha,
            True,
            ("health://exact",),
            NOW,
            release=intent.release,
        )

    def rollback(
        self,
        intent: DeploymentIntent,
        previous_release: ReleaseRef | None,
    ) -> RollbackEvidence:
        self.rollback_calls += 1
        self.current = previous_release
        return RollbackEvidence(
            intent.environment.environment_id,
            intent.release.source_sha,
            previous_release.source_sha if previous_release is not None else None,
            True,
            ("rollback://exact",),
            failed_release=intent.release,
            restored_release=previous_release,
        )

    def inspect(self, intent: DeploymentIntent) -> ProviderInspection:
        del intent
        if self.current is None:
            return ProviderInspection(None, None, ("inspect://missing",))
        return ProviderInspection(
            self.current.source_sha,
            True,
            ("inspect://exact",),
            release=self.current,
        )


@dataclass
class _ShaOnlyHealthProvider:
    deploy_calls: int = 0

    def deploy(self, intent: DeploymentIntent) -> ProviderDeploymentResult:
        self.deploy_calls += 1
        return ProviderDeploymentResult(
            True,
            False,
            ("deploy://exact",),
            release=intent.release,
        )

    def health(self, intent: DeploymentIntent) -> HealthEvidence:
        return HealthEvidence(
            intent.environment.environment_id,
            intent.release.source_sha,
            True,
            ("health://sha-only",),
            NOW,
        )

    def rollback(
        self,
        intent: DeploymentIntent,
        previous_release: ReleaseRef | None,
    ) -> RollbackEvidence:
        return RollbackEvidence(
            intent.environment.environment_id,
            intent.release.source_sha,
            previous_release.source_sha if previous_release is not None else None,
            True,
            ("rollback://should-not-run",),
            failed_release=intent.release,
            restored_release=previous_release,
        )

    def inspect(self, intent: DeploymentIntent) -> ProviderInspection:
        return ProviderInspection(
            intent.release.source_sha,
            True,
            ("inspect://exact",),
            release=intent.release,
        )


@dataclass
class _LostRollbackAckProvider:
    current: ReleaseRef | None = None
    unhealthy_version: str = "2.0.0"
    deploy_calls: int = 0
    rollback_calls: int = 0

    def deploy(self, intent: DeploymentIntent) -> ProviderDeploymentResult:
        self.deploy_calls += 1
        self.current = intent.release
        return ProviderDeploymentResult(
            True,
            False,
            (f"deploy://{intent.intent_id}",),
            release=intent.release,
        )

    def health(self, intent: DeploymentIntent) -> HealthEvidence:
        return HealthEvidence(
            intent.environment.environment_id,
            intent.release.source_sha,
            intent.release.version != self.unhealthy_version,
            (f"health://{intent.intent_id}",),
            NOW,
            release=intent.release,
        )

    def rollback(
        self,
        intent: DeploymentIntent,
        previous_release: ReleaseRef | None,
    ) -> RollbackEvidence:
        self.rollback_calls += 1
        self.current = previous_release
        raise RuntimeError("rollback applied but acknowledgement was lost")

    def inspect(self, intent: DeploymentIntent) -> ProviderInspection:
        del intent
        if self.current is None:
            return ProviderInspection(None, None, ("inspect://missing",))
        return ProviderInspection(
            self.current.source_sha,
            True,
            ("inspect://restored",),
            release=self.current,
        )


def test_canonical_provider_contract_uses_full_release_identity() -> None:
    deploy_fields = ProviderDeploymentResult.__dataclass_fields__
    rollback_parameters = inspect.signature(DeploymentProviderPort.rollback).parameters

    assert "release" in deploy_fields
    assert "previous_release" in rollback_parameters
    assert "previous_release_sha" not in rollback_parameters


def test_sha_only_health_cannot_earn_exact_healthy_authority() -> None:
    provider = _ShaOnlyHealthProvider()
    fabric = DeploymentFabric(provider)
    intent = _intent("deploy-sha-only", _release("1.0.0", DIGEST_A))

    record = fabric.deploy(intent)

    assert record.state is DeploymentState.UNCERTAIN
    assert fabric.snapshot().healthy_staging == ()
    assert provider.deploy_calls == 1


def test_process_loss_after_apply_is_durably_reserved_and_never_replayed(tmp_path) -> None:
    store, task_id = _store_and_task(tmp_path)
    provider = _CrashAfterApplyProvider()
    release_a = _release("1.0.0", DIGEST_A)
    intent_a = _intent("deploy-a", release_a)

    first = DeploymentFabric(
        provider,
        SQLiteDeploymentEffectJournal(store, task_id),
    )
    with pytest.raises(SystemExit, match="process loss"):
        first.deploy(intent_a)
    assert provider.deploy_calls == 1
    assert provider.current == release_a

    restarted = DeploymentFabric(
        provider,
        SQLiteDeploymentEffectJournal(store, task_id),
    )
    duplicate = restarted.deploy(intent_a)
    assert duplicate.state is DeploymentState.UNCERTAIN
    assert duplicate.provider_evidence_refs == ()
    assert provider.deploy_calls == 1

    other = DeploymentFabric(
        provider,
        SQLiteDeploymentEffectJournal(store, task_id),
    )
    with pytest.raises(
        DeploymentFabricError,
        match="durable unresolved deployment effect",
    ):
        other.deploy(_intent("deploy-b", _release("2.0.0", DIGEST_B)))
    assert provider.deploy_calls == 1

    reconciled = restarted.reconcile(intent_a.intent_id)
    assert reconciled.state is DeploymentState.HEALTHY
    assert provider.deploy_calls == 1
    assert restarted.snapshot().healthy_staging == (
        ("project-durable", "1.0.0", SHA, DIGEST_A),
    )


def test_lost_rollback_ack_reconciles_after_restart_without_replay(tmp_path) -> None:
    store, task_id = _store_and_task(tmp_path)
    provider = _LostRollbackAckProvider()
    release_a = _release("1.0.0", DIGEST_A)
    release_b = _release("2.0.0", DIGEST_B)

    fabric = DeploymentFabric(
        provider,
        SQLiteDeploymentEffectJournal(store, task_id),
    )
    first = fabric.deploy(_intent("deploy-a", release_a))
    second_intent = _intent("deploy-b", release_b)
    second = fabric.deploy(second_intent)

    assert first.state is DeploymentState.HEALTHY
    assert second.state is DeploymentState.UNCERTAIN
    assert second.health is not None and not second.health.healthy
    assert provider.current == release_a
    assert provider.deploy_calls == 2
    assert provider.rollback_calls == 1

    snapshot = fabric.snapshot()
    restarted = DeploymentFabric(
        provider,
        SQLiteDeploymentEffectJournal(store, task_id),
    )
    restarted.restore(snapshot)

    duplicate = restarted.deploy(second_intent)
    assert duplicate.state is DeploymentState.UNCERTAIN
    assert provider.deploy_calls == 2
    assert provider.rollback_calls == 1

    reconciled = restarted.reconcile(second_intent.intent_id)
    assert reconciled.state is DeploymentState.ROLLED_BACK
    assert reconciled.rollback is not None
    assert reconciled.rollback.restored_release == release_a
    assert provider.deploy_calls == 2
    assert provider.rollback_calls == 1
    assert restarted.snapshot().current_releases == (
        ("project-durable", "staging-durable", "1.0.0", SHA, DIGEST_A),
    )


def test_product_command_consumes_exact_current_snapshot_shape() -> None:
    provider = _CrashAfterApplyProvider(crash_on_deploy=False)
    fabric = DeploymentFabric(provider)
    intent = _intent("deploy-a", _release("1.0.0", DIGEST_A))

    record = fabric.deploy(intent)
    assert record.state is DeploymentState.HEALTHY

    entries = deployment_status_entries(fabric.snapshot())
    release_entry = next(item for item in entries if item.item_id == "release:deploy-a")
    assert SHA in release_entry.detail
    assert DIGEST_A in release_entry.detail
