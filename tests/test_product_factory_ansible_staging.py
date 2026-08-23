from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from nika_core.product_factory_ansible_staging import (
    AnsibleRunnerClient,
    AnsibleRunnerConfig,
    AuthorizedAnsibleStagingAdapter,
    AuthorizedStagingTarget,
    RunnerExecution,
    StagingAdapterError,
)
from nika_core.product_factory_deployment import (
    DeploymentFabric,
    DeploymentFabricError,
    DeploymentIntent,
    DeploymentState,
    EnvironmentIdentity,
    EnvironmentTier,
    ReleaseRef,
)


def _sha(value: int) -> str:
    return f"{value:040x}"[-40:]


def _digest(value: int) -> str:
    return f"{value:064x}"[-64:]


def _trusted_data_dir() -> Path:
    return Path.cwd().resolve() / "trusted-nika-ansible"


def _intent(
    *,
    tier: EnvironmentTier = EnvironmentTier.STAGING,
    project_id: str = "p-social",
    environment_id: str = "staging-eu",
    provider_ref: str = "ansible:staging-eu",
    sha: int = 1,
    version: str = "1.0.0",
    digest: int | None = None,
) -> DeploymentIntent:
    digest_value = sha if digest is None else digest
    return DeploymentIntent(
        "intent-1",
        project_id,
        EnvironmentIdentity(
            environment_id,
            project_id,
            tier,
            provider_ref,
        ),
        ReleaseRef(
            project_id,
            version,
            _sha(sha),
            _digest(digest_value),
        ),
    )


def _exact_release_contract(
    *,
    sha: int = 1,
    version: str = "1.0.0",
    digest: int | None = None,
) -> dict[str, object]:
    digest_value = sha if digest is None else digest
    return {
        "release_version": version,
        "release_sha": _sha(sha),
        "artifact_digest": _digest(digest_value),
    }


@dataclass
class FakeRunner:
    executions: list[RunnerExecution]

    def __post_init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def execute(
        self,
        *,
        private_data_dir: Path,
        playbook: str,
        inventory: str,
        ident: str,
        extravars: Mapping[str, object],
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
        return self.executions.pop(0)


def _adapter(
    *executions: RunnerExecution,
) -> tuple[AuthorizedAnsibleStagingAdapter, FakeRunner]:
    runner = FakeRunner(list(executions))
    adapter = AuthorizedAnsibleStagingAdapter(
        AuthorizedStagingTarget(
            "p-social",
            "staging-eu",
            "ansible:staging-eu",
            "inventory/staging.ini",
            "approval-ref:pf3-staging-eu",
        ),
        AnsibleRunnerConfig(_trusted_data_dir()),
        runner,
    )
    return adapter, runner


def _execution(
    operation: str,
    contract: Mapping[str, object] | None,
    *,
    status: str = "successful",
    rc: int | None = 0,
) -> RunnerExecution:
    return RunnerExecution(
        status,
        rc,
        contract,
        f"ansible-runner:evidence-{operation}",
    )


def test_deploy_uses_exact_authorized_staging_identity_and_safe_extravars() -> None:
    adapter, runner = _adapter(
        _execution("deploy", {"applied": True})
    )
    result = adapter.deploy(_intent())
    assert result.applied is True
    assert result.uncertain is False
    assert result.evidence_refs == (
        "ansible-runner:evidence-deploy",
    )
    call = runner.calls[0]
    assert call["playbook"] == "nika_pf3_deploy.yml"
    assert call["inventory"] == "inventory/staging.ini"
    extravars = call["extravars"]
    assert isinstance(extravars, dict)
    assert extravars["nika_project_id"] == "p-social"
    assert extravars["nika_release_version"] == "1.0.0"
    assert extravars["nika_release_sha"] == _sha(1)
    assert extravars["nika_artifact_digest"] == _digest(1)
    assert "password" not in extravars
    assert "token" not in extravars
    assert "secret" not in extravars


def test_production_intent_is_rejected_before_runner_call() -> None:
    adapter, runner = _adapter()
    with pytest.raises(
        StagingAdapterError,
        match="restricted to staging",
    ):
        adapter.deploy(
            _intent(tier=EnvironmentTier.PRODUCTION)
        )
    assert runner.calls == []


@pytest.mark.parametrize(
    ("field", "kwargs"),
    [
        ("project", {"project_id": "other"}),
        ("environment", {"environment_id": "other"}),
        ("provider", {"provider_ref": "ansible:other"}),
    ],
)
def test_target_identity_mismatch_is_rejected(
    field: str,
    kwargs: dict[str, str],
) -> None:
    adapter, runner = _adapter()
    with pytest.raises(
        StagingAdapterError,
        match="outside the authorized staging target",
    ):
        adapter.deploy(_intent(**kwargs))
    assert runner.calls == [], field


def test_failed_or_timed_out_deploy_is_uncertain_not_rejected_as_safe() -> None:
    adapter, _ = _adapter(
        _execution(
            "deploy",
            None,
            status="timeout",
            rc=254,
        )
    )
    result = adapter.deploy(_intent())
    assert result.applied is False
    assert result.uncertain is True


def test_successful_deploy_requires_explicit_applied_contract() -> None:
    adapter, _ = _adapter(_execution("deploy", {}))
    with pytest.raises(
        StagingAdapterError,
        match="applied must be boolean",
    ):
        adapter.deploy(_intent())


def test_health_binds_exact_release_and_timestamp() -> None:
    observed_at = "2026-08-21T00:00:00Z"
    contract = {
        **_exact_release_contract(),
        "healthy": True,
        "observed_at": observed_at,
    }
    adapter, _ = _adapter(
        _execution("health", contract)
    )
    evidence = adapter.health(_intent())
    assert evidence.release_sha == _sha(1)
    assert evidence.healthy is True
    assert evidence.checked_at == datetime(
        2026,
        8,
        21,
        tzinfo=UTC,
    )


@pytest.mark.parametrize(
    "contract",
    [
        {
            **_exact_release_contract(sha=2),
            "healthy": True,
            "observed_at": "2026-08-21T00:00:00Z",
        },
        {
            **_exact_release_contract(digest=2),
            "healthy": True,
            "observed_at": "2026-08-21T00:00:00Z",
        },
        {
            **_exact_release_contract(version="1.0.1"),
            "healthy": True,
            "observed_at": "2026-08-21T00:00:00Z",
        },
    ],
)
def test_health_rejects_exact_release_substitution(
    contract: dict[str, object],
) -> None:
    adapter, _ = _adapter(_execution("health", contract))
    with pytest.raises(
        StagingAdapterError,
        match="different exact release identity",
    ):
        adapter.health(_intent())


@pytest.mark.parametrize(
    "missing_field",
    ["release_version", "artifact_digest"],
)
def test_health_requires_complete_exact_release_contract(
    missing_field: str,
) -> None:
    contract = {
        **_exact_release_contract(),
        "healthy": True,
        "observed_at": "2026-08-21T00:00:00Z",
    }
    del contract[missing_field]
    adapter, _ = _adapter(_execution("health", contract))
    with pytest.raises(
        StagingAdapterError,
        match=f"contract field {missing_field}",
    ):
        adapter.health(_intent())


def test_health_transport_failure_does_not_claim_unhealthy_release() -> None:
    adapter, _ = _adapter(
        _execution(
            "health",
            None,
            status="failed",
            rc=2,
        )
    )
    with pytest.raises(
        StagingAdapterError,
        match="did not complete successfully",
    ):
        adapter.health(_intent())


def test_rollback_must_restore_exact_requested_previous_release() -> None:
    previous = _sha(9)
    adapter, runner = _adapter(
        _execution(
            "rollback",
            {
                "succeeded": True,
                "restored_release_sha": previous,
            },
        )
    )
    evidence = adapter.rollback(_intent(), previous)
    assert evidence.succeeded is True
    assert evidence.restored_release_sha == previous
    extravars = runner.calls[0]["extravars"]
    assert isinstance(extravars, dict)
    assert extravars["nika_previous_release_sha"] == previous


def test_rollback_rejects_wrong_restored_sha() -> None:
    adapter, _ = _adapter(
        _execution(
            "rollback",
            {
                "succeeded": True,
                "restored_release_sha": _sha(8),
            },
        )
    )
    with pytest.raises(
        StagingAdapterError,
        match="did not restore",
    ):
        adapter.rollback(_intent(), _sha(9))


def test_failed_rollback_reports_failure_without_false_success() -> None:
    adapter, _ = _adapter(
        _execution(
            "rollback",
            None,
            status="failed",
            rc=2,
        )
    )
    evidence = adapter.rollback(_intent(), _sha(9))
    assert evidence.succeeded is False
    assert evidence.restored_release_sha == _sha(9)


def test_inspect_returns_only_exact_release_health_and_evidence() -> None:
    adapter, _ = _adapter(
        _execution(
            "inspect",
            {
                **_exact_release_contract(),
                "healthy": False,
                "ignored": "raw",
            },
        )
    )
    inspection = adapter.inspect(_intent())
    assert inspection.release_sha == _sha(1)
    assert inspection.healthy is False
    assert inspection.evidence_refs == (
        "ansible-runner:evidence-inspect",
    )


@pytest.mark.parametrize(
    "contract",
    [
        {
            **_exact_release_contract(digest=2),
            "healthy": True,
        },
        {
            **_exact_release_contract(version="1.0.1"),
            "healthy": True,
        },
    ],
)
def test_inspection_rejects_exact_release_substitution(
    contract: dict[str, object],
) -> None:
    adapter, _ = _adapter(
        _execution("inspect", contract)
    )
    with pytest.raises(
        StagingAdapterError,
        match="different exact release identity",
    ):
        adapter.inspect(_intent())


def test_inspection_missing_release_has_no_partial_identity() -> None:
    adapter, _ = _adapter(
        _execution(
            "inspect",
            {
                "release_sha": None,
                "release_version": "1.0.0",
                "artifact_digest": _digest(1),
                "healthy": None,
            },
        )
    )
    with pytest.raises(
        StagingAdapterError,
        match="missing release SHA",
    ):
        adapter.inspect(_intent())


def test_inspection_failure_preserves_uncertainty_by_raising() -> None:
    adapter, _ = _adapter(
        _execution(
            "inspect",
            None,
            status="timeout",
            rc=254,
        )
    )
    with pytest.raises(
        StagingAdapterError,
        match="did not complete successfully",
    ):
        adapter.inspect(_intent())


def test_uncertain_reconcile_rejects_same_sha_wrong_artifact() -> None:
    adapter, _ = _adapter(
        _execution(
            "deploy",
            None,
            status="timeout",
            rc=254,
        ),
        _execution(
            "inspect",
            {
                **_exact_release_contract(digest=2),
                "healthy": True,
            },
        ),
    )
    fabric = DeploymentFabric(adapter)
    intent = _intent()
    uncertain = fabric.deploy(intent)
    assert uncertain.state is DeploymentState.UNCERTAIN

    with pytest.raises(
        StagingAdapterError,
        match="different exact release identity",
    ):
        fabric.reconcile(intent.intent_id)

    assert (
        fabric.snapshot().records[0].state
        is DeploymentState.UNCERTAIN
    )


def test_rejected_health_becomes_uncertain_without_staging_authority() -> None:
    adapter, runner = _adapter(
        _execution("deploy", {"applied": True}),
        _execution(
            "health",
            {
                **_exact_release_contract(digest=2),
                "healthy": True,
                "observed_at": "2026-08-21T00:00:00Z",
            },
        ),
    )
    fabric = DeploymentFabric(adapter)
    intent = _intent()

    uncertain = fabric.deploy(intent)
    assert uncertain.state is DeploymentState.UNCERTAIN
    assert fabric.snapshot().healthy_staging == ()
    assert len(runner.calls) == 2

    duplicate = fabric.deploy(intent)
    assert duplicate == uncertain
    assert len(runner.calls) == 2

    production = _intent(
        tier=EnvironmentTier.PRODUCTION,
        environment_id="production-eu",
        provider_ref="ansible:production-eu",
    )
    with pytest.raises(
        DeploymentFabricError,
        match="healthy staging proof for exact release",
    ):
        fabric.deploy(production)


def test_authorization_reference_rejects_raw_secret_shape() -> None:
    with pytest.raises(
        StagingAdapterError,
        match="opaque reference",
    ):
        AuthorizedStagingTarget(
            "p-social",
            "staging-eu",
            "ansible:staging-eu",
            "inventory/staging.ini",
            "ghp_not-a-valid-place-for-a-secret",
        )


def test_playbook_names_cannot_escape_trusted_runner_project() -> None:
    with pytest.raises(
        StagingAdapterError,
        match="trusted relative leaf",
    ):
        AnsibleRunnerConfig(
            _trusted_data_dir(),
            deploy_playbook="../deploy.yml",
        )


def test_private_data_dir_must_be_absolute_trusted_configuration() -> None:
    with pytest.raises(
        StagingAdapterError,
        match="absolute trusted local path",
    ):
        AnsibleRunnerConfig(Path("relative/runner-data"))


def test_ansible_runner_client_extracts_only_named_contract_and_hash_evidence() -> None:
    events = [
        {
            "event": "runner_on_ok",
            "event_data": {
                "task": "unrelated",
                "res": {"password": "must-not-escape"},
            },
        },
        {
            "event": "runner_on_ok",
            "event_data": {
                "task": "nika_pf3_result",
                "res": {
                    "nika_pf3": {"applied": True},
                    "stdout": "must-not-escape",
                },
            },
        },
    ]
    captured: dict[str, object] = {}

    def run(**kwargs: object) -> object:
        captured.update(kwargs)
        return SimpleNamespace(
            status="successful",
            rc=0,
            events=events,
        )

    client = AnsibleRunnerClient(
        SimpleNamespace(run=run)
    )
    execution = client.execute(
        private_data_dir=_trusted_data_dir(),
        playbook="nika_pf3_deploy.yml",
        inventory="inventory/staging.ini",
        ident="nika-pf3-deploy-1",
        extravars={"nika_release_sha": _sha(1)},
    )
    assert execution.contract == {"applied": True}
    assert execution.evidence_ref.startswith(
        "ansible-runner:"
    )
    assert "must-not-escape" not in execution.evidence_ref
    assert captured["quiet"] is True


def test_ansible_runner_client_rejects_duplicate_result_contracts() -> None:
    event = {
        "event": "runner_on_ok",
        "event_data": {
            "task": "nika_pf3_result",
            "res": {"nika_pf3": {"applied": True}},
        },
    }

    def run(**kwargs: object) -> object:
        return SimpleNamespace(
            status="successful",
            rc=0,
            events=[event, event],
        )

    client = AnsibleRunnerClient(
        SimpleNamespace(run=run)
    )
    with pytest.raises(
        StagingAdapterError,
        match="multiple nika_pf3 result contracts",
    ):
        client.execute(
            private_data_dir=_trusted_data_dir(),
            playbook="nika_pf3_deploy.yml",
            inventory="inventory/staging.ini",
            ident="nika-pf3-deploy-1",
            extravars={"nika_release_sha": _sha(1)},
        )


def test_ansible_runner_client_evidence_is_deterministic_for_same_normalized_result() -> None:
    event = {
        "event": "runner_on_ok",
        "event_data": {
            "task": "nika_pf3_result",
            "res": {"nika_pf3": {"applied": True}},
        },
    }

    def module() -> object:
        return SimpleNamespace(
            run=lambda **kwargs: SimpleNamespace(
                status="successful",
                rc=0,
                events=[event],
            )
        )

    kwargs = {
        "private_data_dir": _trusted_data_dir(),
        "playbook": "nika_pf3_deploy.yml",
        "inventory": "inventory/staging.ini",
        "ident": "nika-pf3-deploy-1",
        "extravars": {"nika_release_sha": _sha(1)},
    }
    first = AnsibleRunnerClient(module()).execute(**kwargs)
    second = AnsibleRunnerClient(module()).execute(**kwargs)
    assert first == second


def test_default_runner_loader_rejects_native_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "nika_core.product_factory_ansible_staging.sys.platform",
        "win32",
    )
    client = AnsibleRunnerClient()
    with pytest.raises(
        StagingAdapterError,
        match="native Windows",
    ):
        client.execute(
            private_data_dir=_trusted_data_dir(),
            playbook="nika_pf3_deploy.yml",
            inventory="inventory/staging.ini",
            ident="nika-pf3-deploy-1",
            extravars={"nika_release_sha": _sha(1)},
        )
