from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_factory_ansible_staging import (
    AnsibleRunnerConfig,
    AuthorizedAnsibleStagingAdapter,
    AuthorizedStagingTarget,
    RunnerExecution,
)
from nika_core.product_factory_deployment import (
    DeploymentIntent,
    EnvironmentTier,
    ReleaseRef,
)
from nika_core.product_factory_packaged_staging_authority import (
    PackagedStagingAuthorityError,
    PackagedStagingAuthorityStore,
)

PROJECT_ID = "project-packaged-staging"
REPOSITORY_ID = "repo-main"
WORK_ID = "work-build-001"
SOURCE_SHA = "a" * 40
ARTIFACT_DIGEST = "b" * 64


def _target(
    *,
    project_id: str = PROJECT_ID,
    environment_id: str = "staging-eu-1",
    provider_ref: str = "ansible-staging",
    inventory: str = "inventory/staging.ini",
    authorization_ref: str = "credential-ref:staging-deploy",
) -> AuthorizedStagingTarget:
    return AuthorizedStagingTarget(
        project_id=project_id,
        environment_id=environment_id,
        provider_ref=provider_ref,
        inventory=inventory,
        authorization_ref=authorization_ref,
    )


def _store(
    tmp_path: Path,
    *,
    target: AuthorizedStagingTarget | None = None,
) -> tuple[SQLiteStore, PackagedStagingAuthorityStore]:
    sqlite = SQLiteStore(tmp_path / "nika.db")
    sqlite.initialize()
    return (
        sqlite,
        PackagedStagingAuthorityStore(
            sqlite,
            target=target or _target(),
        ),
    )


def _authorize(authorities: PackagedStagingAuthorityStore):
    return authorities.authorize(
        project_id=PROJECT_ID,
        repository_id=REPOSITORY_ID,
        work_id=WORK_ID,
        release_version="1.4.0",
        migration_refs=("migration://schema/14",),
    )


@dataclass
class SuccessfulRunner:
    calls: int = 0

    def execute(
        self,
        *,
        private_data_dir,
        playbook: str,
        inventory: str,
        ident: str,
        extravars,
    ) -> RunnerExecution:
        self.calls += 1
        assert Path(private_data_dir).is_absolute()
        assert playbook == "nika_pf3_deploy.yml"
        assert inventory == "inventory/staging.ini"
        assert ident
        assert extravars["nika_project_id"] == PROJECT_ID
        assert extravars["nika_environment_id"] == "staging-eu-1"
        assert extravars["nika_provider_ref"] == "ansible-staging"
        assert extravars["nika_release_version"] == "1.4.0"
        assert extravars["nika_release_sha"] == SOURCE_SHA
        assert extravars["nika_artifact_digest"] == ARTIFACT_DIGEST
        return RunnerExecution(
            "successful",
            0,
            {"applied": True},
            "runner://deploy/exact",
        )


def test_authorized_work_resolves_exact_staging_authority_after_restart(
    tmp_path: Path,
) -> None:
    sqlite, authorities = _store(tmp_path)
    authorized = _authorize(authorities)

    resolved = authorities.resolve(
        project_id=PROJECT_ID,
        repository_id=REPOSITORY_ID,
        work_id=WORK_ID,
    )
    assert resolved == authorized.authority
    assert resolved.release_version == "1.4.0"
    assert resolved.migration_refs == ("migration://schema/14",)
    assert resolved.staging_environment.environment_id == "staging-eu-1"
    assert resolved.staging_environment.tier is EnvironmentTier.STAGING
    assert resolved.staging_environment.provider_ref == "ansible-staging"

    reopened = SQLiteStore(sqlite.path)
    reopened.initialize()
    restarted = PackagedStagingAuthorityStore(
        reopened,
        target=_target(),
    )
    assert restarted.resolve(
        project_id=PROJECT_ID,
        repository_id=REPOSITORY_ID,
        work_id=WORK_ID,
    ) == resolved


def test_same_work_authority_is_immutable_and_exact_repeat_is_idempotent(
    tmp_path: Path,
) -> None:
    _sqlite, authorities = _store(tmp_path)
    first = _authorize(authorities)
    repeated = _authorize(authorities)

    assert repeated == first

    with pytest.raises(
        PackagedStagingAuthorityError,
        match="already bound to different staging authority",
    ):
        authorities.authorize(
            project_id=PROJECT_ID,
            repository_id=REPOSITORY_ID,
            work_id=WORK_ID,
            release_version="1.4.1",
            migration_refs=("migration://schema/14",),
        )

    assert authorities.resolve(
        project_id=PROJECT_ID,
        repository_id=REPOSITORY_ID,
        work_id=WORK_ID,
    ) == first.authority


def test_revocation_is_digest_guarded_durable_and_fail_closed(
    tmp_path: Path,
) -> None:
    sqlite, authorities = _store(tmp_path)
    authorized = _authorize(authorities)

    with pytest.raises(
        PackagedStagingAuthorityError,
        match="reload before revocation",
    ):
        authorities.revoke(
            project_id=PROJECT_ID,
            repository_id=REPOSITORY_ID,
            work_id=WORK_ID,
            expected_authority_digest="0" * 64,
        )

    revoked = authorities.revoke(
        project_id=PROJECT_ID,
        repository_id=REPOSITORY_ID,
        work_id=WORK_ID,
        expected_authority_digest=authorized.authority_digest,
    )
    assert revoked.revoked is True

    repeated = authorities.revoke(
        project_id=PROJECT_ID,
        repository_id=REPOSITORY_ID,
        work_id=WORK_ID,
        expected_authority_digest=authorized.authority_digest,
    )
    assert repeated == revoked

    with pytest.raises(
        PackagedStagingAuthorityError,
        match="cannot be reauthorized",
    ):
        _authorize(authorities)

    reopened = SQLiteStore(sqlite.path)
    reopened.initialize()
    restarted = PackagedStagingAuthorityStore(
        reopened,
        target=_target(),
    )
    with pytest.raises(
        PackagedStagingAuthorityError,
        match="has been revoked",
    ):
        restarted.resolve(
            project_id=PROJECT_ID,
            repository_id=REPOSITORY_ID,
            work_id=WORK_ID,
        )


@pytest.mark.parametrize(
    "replacement",
    (
        _target(provider_ref="ansible-staging-v2"),
        _target(environment_id="staging-eu-2"),
        _target(inventory="inventory/staging-v2.ini"),
        _target(authorization_ref="credential-ref:staging-deploy-v2"),
    ),
)
def test_any_provider_target_drift_invalidates_prior_work_authority(
    tmp_path: Path,
    replacement: AuthorizedStagingTarget,
) -> None:
    sqlite, authorities = _store(tmp_path)
    _authorize(authorities)

    reopened = SQLiteStore(sqlite.path)
    reopened.initialize()
    changed = PackagedStagingAuthorityStore(
        reopened,
        target=replacement,
    )
    with pytest.raises(
        PackagedStagingAuthorityError,
        match="provider target changed",
    ):
        changed.resolve(
            project_id=PROJECT_ID,
            repository_id=REPOSITORY_ID,
            work_id=WORK_ID,
        )


def test_authority_table_does_not_duplicate_inventory_or_authorization_ref(
    tmp_path: Path,
) -> None:
    sqlite, authorities = _store(tmp_path)
    _authorize(authorities)

    with sqlite.connection() as conn:
        row = conn.execute(
            "SELECT * FROM product_factory_staging_work_authority "
            "WHERE project_id = ? AND repository_id = ? AND work_id = ?",
            (PROJECT_ID, REPOSITORY_ID, WORK_ID),
        ).fetchone()

    assert row is not None
    persisted = "\n".join(
        str(row[key])
        for key in row.keys()
        if row[key] is not None
    )
    assert "inventory/staging.ini" not in persisted
    assert "credential-ref:staging-deploy" not in persisted
    assert len(row["target_digest"]) == 64


def test_store_readmits_forged_target_before_it_becomes_authority(
    tmp_path: Path,
) -> None:
    forged = object.__new__(AuthorizedStagingTarget)
    object.__setattr__(forged, "project_id", PROJECT_ID)
    object.__setattr__(forged, "environment_id", "staging-eu-1")
    object.__setattr__(forged, "provider_ref", "ansible-staging")
    object.__setattr__(forged, "inventory", "inventory/staging.ini")
    object.__setattr__(forged, "authorization_ref", "ghp_plaintextsecret")

    sqlite = SQLiteStore(tmp_path / "nika.db")
    sqlite.initialize()
    with pytest.raises(
        PackagedStagingAuthorityError,
        match="failed canonical readmission",
    ):
        PackagedStagingAuthorityStore(
            sqlite,
            target=forged,
        )


def test_target_project_mismatch_cannot_authorize_work(tmp_path: Path) -> None:
    _sqlite, authorities = _store(
        tmp_path,
        target=_target(project_id="other-project"),
    )

    with pytest.raises(
        PackagedStagingAuthorityError,
        match="target project does not match",
    ):
        authorities.authorize(
            project_id=PROJECT_ID,
            repository_id=REPOSITORY_ID,
            work_id=WORK_ID,
            release_version="1.4.0",
        )


def test_resolved_authority_drives_the_same_canonical_staging_target(
    tmp_path: Path,
) -> None:
    _sqlite, authorities = _store(tmp_path)
    _authorize(authorities)
    resolved = authorities.resolve(
        project_id=PROJECT_ID,
        repository_id=REPOSITORY_ID,
        work_id=WORK_ID,
    )
    runner = SuccessfulRunner()
    provider = AuthorizedAnsibleStagingAdapter(
        authorities.target,
        AnsibleRunnerConfig(tmp_path),
        runner,
    )
    intent = DeploymentIntent(
        intent_id="intent-pf5-pf6-001",
        project_id=PROJECT_ID,
        environment=resolved.staging_environment,
        release=ReleaseRef(
            PROJECT_ID,
            resolved.release_version,
            SOURCE_SHA,
            ARTIFACT_DIGEST,
        ),
        migration_refs=resolved.migration_refs,
    )

    result = provider.deploy(intent)

    assert result.applied is True
    assert result.uncertain is False
    assert result.evidence_refs == ("runner://deploy/exact",)
    assert runner.calls == 1


def test_missing_work_or_wrong_repository_never_falls_back(
    tmp_path: Path,
) -> None:
    _sqlite, authorities = _store(tmp_path)
    _authorize(authorities)

    with pytest.raises(
        PackagedStagingAuthorityError,
        match="has no packaged staging authority",
    ):
        authorities.resolve(
            project_id=PROJECT_ID,
            repository_id="repo-other",
            work_id=WORK_ID,
        )
    with pytest.raises(
        PackagedStagingAuthorityError,
        match="has no packaged staging authority",
    ):
        authorities.resolve(
            project_id=PROJECT_ID,
            repository_id=REPOSITORY_ID,
            work_id="work-other",
        )
