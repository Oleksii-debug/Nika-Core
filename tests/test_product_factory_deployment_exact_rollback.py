from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from nika_core.product_factory_ansible_staging import (
    AnsibleRunnerConfig,
    AuthorizedAnsibleStagingAdapter,
    AuthorizedStagingTarget,
    RunnerExecution,
)
from nika_core.product_factory_deployment import (
    DeploymentFabric,
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

SHA = "a" * 40
DIGEST_A = "1" * 64
DIGEST_B = "2" * 64
DIGEST_C = "3" * 64
NOW = datetime(2026, 8, 23, 21, 0, tzinfo=UTC)


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
class _LegacyShaProvider:
    unhealthy_versions: set[str] = field(default_factory=set)
    deploy_calls: int = 0
    rollback_calls: int = 0
    current: ReleaseRef | None = None

    def deploy(self, intent: DeploymentIntent) -> ProviderDeploymentResult:
        self.deploy_calls += 1
        self.current = intent.release
        return ProviderDeploymentResult(True, False, (f"deploy://{intent.intent_id}",))

    def health(self, intent: DeploymentIntent) -> HealthEvidence:
        return HealthEvidence(
            intent.environment.environment_id,
            intent.release.source_sha,
            intent.release.version not in self.unhealthy_versions,
            (f"health://{intent.intent_id}",),
            NOW,
            release=intent.release,
        )

    def rollback(
        self,
        intent: DeploymentIntent,
        previous_release_sha: str | None,
    ) -> RollbackEvidence:
        self.rollback_calls += 1
        return RollbackEvidence(
            intent.environment.environment_id,
            intent.release.source_sha,
            previous_release_sha,
            True,
            (f"rollback://{intent.intent_id}",),
        )

    def inspect(self, intent: DeploymentIntent) -> ProviderInspection:
        del intent
        if self.current is None:
            return ProviderInspection(None, None, ("inspect://none",))
        return ProviderInspection(
            self.current.source_sha,
            True,
            ("inspect://current",),
            release=self.current,
        )


@dataclass
class _ExactRollbackProvider(_LegacyShaProvider):
    exact_targets: list[ReleaseRef | None] = field(default_factory=list)

    def rollback_exact(
        self,
        intent: DeploymentIntent,
        previous_release: ReleaseRef | None,
    ) -> RollbackEvidence:
        self.rollback_calls += 1
        self.exact_targets.append(previous_release)
        self.current = previous_release
        return RollbackEvidence(
            intent.environment.environment_id,
            intent.release.source_sha,
            previous_release.source_sha if previous_release is not None else None,
            True,
            (f"rollback-exact://{intent.intent_id}",),
            failed_release=intent.release,
            restored_release=previous_release,
        )


def test_legacy_sha_only_provider_is_not_asked_to_restore_ambiguous_previous_release() -> None:
    release_a = _release("1.0.0", DIGEST_A)
    release_b = _release("2.0.0", DIGEST_B)
    release_c = _release("3.0.0", DIGEST_C)
    provider = _LegacyShaProvider(unhealthy_versions={"2.0.0"})
    fabric = DeploymentFabric(provider)

    first = fabric.deploy(_intent("deploy-a", release_a))
    second_intent = _intent("deploy-b", release_b)
    second = fabric.deploy(second_intent)

    assert first.state is DeploymentState.HEALTHY
    assert second.state is DeploymentState.UNCERTAIN
    assert second.previous_release == release_a
    assert provider.rollback_calls == 0
    assert provider.current == release_b
    assert fabric.snapshot().healthy_staging == ()

    duplicate = fabric.deploy(second_intent)
    assert duplicate == second
    assert provider.deploy_calls == 2
    assert provider.rollback_calls == 0

    with pytest.raises(DeploymentFabricError, match="unresolved deployment effect"):
        fabric.deploy(_intent("deploy-c", release_c))
    assert provider.deploy_calls == 2


def test_exact_capable_provider_must_restore_full_previous_release_identity() -> None:
    release_a = _release("1.0.0", DIGEST_A)
    release_b = _release("2.0.0", DIGEST_B)
    provider = _ExactRollbackProvider(unhealthy_versions={"2.0.0"})
    fabric = DeploymentFabric(provider)

    first = fabric.deploy(_intent("deploy-a", release_a))
    second = fabric.deploy(_intent("deploy-b", release_b))

    assert first.state is DeploymentState.HEALTHY
    assert second.state is DeploymentState.ROLLED_BACK
    assert second.previous_release == release_a
    assert second.rollback is not None
    assert second.rollback.failed_release == release_b
    assert second.rollback.restored_release == release_a
    assert provider.exact_targets == [release_a]
    assert provider.current == release_a
    assert fabric.snapshot().current_releases == (
        ("project-exact", "staging-exact", "1.0.0", SHA, DIGEST_A),
    )


def test_current_release_snapshot_uses_full_release_identity_and_unique_legacy_migration() -> None:
    release_a = _release("1.0.0", DIGEST_A)
    provider = _LegacyShaProvider()
    fabric = DeploymentFabric(provider)
    fabric.deploy(_intent("deploy-a", release_a))

    snapshot = fabric.snapshot()
    assert snapshot.current_releases == (
        ("project-exact", "staging-exact", "1.0.0", SHA, DIGEST_A),
    )

    legacy = replace(
        snapshot,
        current_releases=(("project-exact", "staging-exact", SHA),),
    )
    restarted = DeploymentFabric(provider)
    restarted.restore(legacy)
    assert restarted.snapshot().current_releases == snapshot.current_releases


def test_legacy_current_release_snapshot_fails_closed_when_same_sha_is_ambiguous() -> None:
    release_a = _release("1.0.0", DIGEST_A)
    release_b = _release("2.0.0", DIGEST_B)
    provider = _LegacyShaProvider()
    fabric = DeploymentFabric(provider)
    fabric.deploy(_intent("deploy-a", release_a))
    fabric.deploy(_intent("deploy-b", release_b))

    legacy = replace(
        fabric.snapshot(),
        current_releases=(("project-exact", "staging-exact", SHA),),
    )
    restarted = DeploymentFabric(provider)
    with pytest.raises(
        DeploymentFabricError,
        match="legacy current release snapshot is ambiguous",
    ):
        restarted.restore(legacy)


@dataclass
class _SingleExecutionRunner:
    execution: RunnerExecution
    calls: list[dict[str, object]] = field(default_factory=list)

    def execute(
        self,
        *,
        private_data_dir: Path,
        playbook: str,
        inventory: str,
        ident: str,
        extravars: dict[str, object],
    ) -> RunnerExecution:
        self.calls.append(
            {
                "private_data_dir": private_data_dir,
                "playbook": playbook,
                "inventory": inventory,
                "ident": ident,
                "extravars": dict(extravars),
            }
        )
        return self.execution


def test_ansible_exact_rollback_binds_requested_and_restored_release_identity() -> None:
    previous = ReleaseRef("project-exact", "1.0.0", SHA, DIGEST_A)
    failed = ReleaseRef("project-exact", "2.0.0", SHA, DIGEST_B)
    runner = _SingleExecutionRunner(
        RunnerExecution(
            "successful",
            0,
            {
                "succeeded": True,
                "restored_release_version": previous.version,
                "restored_release_sha": previous.source_sha,
                "restored_artifact_digest": previous.artifact_digest,
            },
            "ansible-runner:rollback-exact",
        )
    )
    adapter = AuthorizedAnsibleStagingAdapter(
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
    intent = _intent("deploy-b", failed)

    evidence = adapter.rollback_exact(intent, previous)

    assert evidence.succeeded is True
    assert evidence.failed_release == failed
    assert evidence.restored_release == previous
    extravars = runner.calls[0]["extravars"]
    assert isinstance(extravars, dict)
    assert extravars["nika_previous_release_version"] == previous.version
    assert extravars["nika_previous_release_sha"] == previous.source_sha
    assert extravars["nika_previous_artifact_digest"] == previous.artifact_digest


def test_ansible_exact_rollback_rejects_partial_restored_release_identity() -> None:
    previous = ReleaseRef("project-exact", "1.0.0", SHA, DIGEST_A)
    failed = ReleaseRef("project-exact", "2.0.0", SHA, DIGEST_B)
    runner = _SingleExecutionRunner(
        RunnerExecution(
            "successful",
            0,
            {
                "succeeded": True,
                "restored_release_version": previous.version,
                "restored_release_sha": previous.source_sha,
            },
            "ansible-runner:rollback-partial",
        )
    )
    adapter = AuthorizedAnsibleStagingAdapter(
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

    with pytest.raises(
        DeploymentFabricError,
        match="must report version, SHA and artifact digest together",
    ):
        adapter.rollback_exact(_intent("deploy-b", failed), previous)
