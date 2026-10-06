from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

import nika_core.product_factory_packaged_staging_pass as staging_pass_module
from nika_core.data.sqlite import SQLiteStore
from nika_core.product_factory_ansible_staging import AuthorizedStagingTarget
from nika_core.product_factory_build_execution import BuildExecutionState
from nika_core.product_factory_deployment import (
    DeploymentIntent,
    DeploymentRecord,
    DeploymentState,
    ExecutionNode,
    NodeCapabilities,
    NodeIdentity,
    Platform,
    ReleaseRef,
    ResourceEnvelope,
)
from nika_core.product_factory_deployment_checkpoint import (
    DurableDeploymentFabric,
    ProductFactoryDeploymentCheckpointHost,
)
from nika_core.product_factory_local_coding import ContainedLocalCodingPolicy
from nika_core.product_factory_packaged_build_authority import (
    PackagedBuildAuthorityRuntime,
    PackagedBuildAuthorityStore,
)
from nika_core.product_factory_packaged_build_pass import (
    PackagedBuildPassResult,
    PackagedComponentBuildState,
    PackagedReviewedBuildPass,
)
from nika_core.product_factory_packaged_local_startup import (
    PackagedLocalProductFactoryStartup,
)
from nika_core.product_factory_packaged_preparation import PreparedProductFactory
from nika_core.product_factory_packaged_staging_authority import (
    PackagedStagingAuthorityStore,
)
from nika_core.product_factory_packaged_staging_pass import (
    PackagedReviewedStagingPass,
    PackagedStagingPassError,
)
from nika_core.toolsmith.contracts import ResourceBudget

PROJECT_ID = "project-packaged-staging-pass"
REPOSITORY_ID = "repo-main"
COMPONENT_ID = "desktop-release"
WORK_ID = "build:project-packaged-staging-pass:desktop-release:1"
SOURCE_SHA = "a" * 40
ARTIFACT_DIGEST = "b" * 64


class _NoEffectProvider:
    def deploy(self, intent):
        raise AssertionError("provider effect must be replaced by canonical handoff test double")

    def health(self, intent):
        raise AssertionError("provider health must not run")

    def rollback(self, intent, previous_release_sha):
        raise AssertionError("provider rollback must not run")

    def inspect(self, intent):
        raise AssertionError("provider inspection must not run")


def _platform() -> Platform:
    return Platform.WINDOWS if os.name == "nt" else Platform.LINUX


def _node() -> ExecutionNode:
    return ExecutionNode(
        NodeIdentity(
            "build-node",
            _platform(),
            "x86_64",
            "instance-staging-pass",
        ),
        NodeCapabilities(
            frozenset({"build"}),
            frozenset({"python"}),
            False,
        ),
        ResourceEnvelope(4, 8192, 32768),
    )


def _startup(tmp_path: Path) -> PackagedLocalProductFactoryStartup:
    executable = str(Path(sys.executable).resolve())
    return PackagedLocalProductFactoryStartup(
        workspace_parent=tmp_path.resolve(),
        policy=ContainedLocalCodingPolicy(
            allowed_executables=(executable,),
            resource_budget=ResourceBudget(
                timeout_seconds=30,
                max_output_bytes=100_000,
                max_changed_files=8,
            ),
            lease_seconds=120,
        ),
        git_executable=Path(sys.executable).resolve(),
    )


def _build_pass(
    store: SQLiteStore,
    tmp_path: Path,
) -> PackagedReviewedBuildPass:
    node = _node()
    startup = _startup(tmp_path)
    runtime = PackagedBuildAuthorityRuntime(
        PackagedBuildAuthorityStore(
            store,
            node=node,
            startup=startup,
        )
    )
    return PackagedReviewedBuildPass(
        store=store,
        node=node,
        startup=startup,
        authority=runtime,
    )


def _prepared() -> PreparedProductFactory:
    state = SimpleNamespace(
        authority=SimpleNamespace(project_id=PROJECT_ID),
    )
    return PreparedProductFactory(
        host_task_id="host-product-factory",
        state=cast(Any, state),
    )


def _build_result(state: BuildExecutionState) -> PackagedBuildPassResult:
    return PackagedBuildPassResult(
        PROJECT_ID,
        (
            PackagedComponentBuildState(
                component_id=COMPONENT_ID,
                work_id=WORK_ID,
                state=state,
                block_reason=(
                    None
                    if state is BuildExecutionState.SUCCEEDED
                    else "build is not ready"
                ),
            ),
        ),
    )


def _target() -> AuthorizedStagingTarget:
    return AuthorizedStagingTarget(
        project_id=PROJECT_ID,
        environment_id="staging-release",
        provider_ref="ansible-staging",
        inventory="inventory/staging.ini",
        authorization_ref="credential-ref:staging-release",
    )


def _staging_authority(
    store: SQLiteStore,
    *,
    authorize: bool,
) -> PackagedStagingAuthorityStore:
    authority = PackagedStagingAuthorityStore(store, target=_target())
    if authorize:
        authority.authorize(
            project_id=PROJECT_ID,
            repository_id=REPOSITORY_ID,
            work_id=WORK_ID,
            release_version="2.0.0",
        )
    return authority


def _deployment(store: SQLiteStore) -> DurableDeploymentFabric:
    return DurableDeploymentFabric(
        _NoEffectProvider(),
        checkpoint_host=ProductFactoryDeploymentCheckpointHost(store),
        host_task_id="host-product-factory",
        project_id=PROJECT_ID,
    )


def _durable_record(state: BuildExecutionState):
    return SimpleNamespace(
        state=state,
        spec=SimpleNamespace(
            request=SimpleNamespace(
                project_id=PROJECT_ID,
                work_id=WORK_ID,
            ),
            scope=SimpleNamespace(repository_id=REPOSITORY_ID),
        ),
    )


def _fake_host(state: BuildExecutionState):
    durable = _durable_record(state)

    class _Coordinator:
        def get(self, work_id: str):
            assert work_id == WORK_ID
            return durable

    return SimpleNamespace(coordinator=_Coordinator())


def _deployment_record(authority: PackagedStagingAuthorityStore) -> DeploymentRecord:
    resolved = authority.resolve(
        project_id=PROJECT_ID,
        repository_id=REPOSITORY_ID,
        work_id=WORK_ID,
    )
    intent = DeploymentIntent(
        "intent-packaged-staging-pass",
        PROJECT_ID,
        resolved.staging_environment,
        ReleaseRef(
            PROJECT_ID,
            resolved.release_version,
            SOURCE_SHA,
            ARTIFACT_DIGEST,
        ),
    )
    return DeploymentRecord(intent, DeploymentState.HEALTHY)


def _patch_build_result(
    monkeypatch: pytest.MonkeyPatch,
    result: PackagedBuildPassResult,
) -> None:
    def advance(self, prepared):
        assert prepared.project_id == PROJECT_ID
        return result

    monkeypatch.setattr(PackagedReviewedBuildPass, "advance", advance)


def test_non_succeeded_build_never_reconstructs_host_or_reaches_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SQLiteStore(tmp_path / "waiting.db")
    store.initialize()
    build_pass = _build_pass(store, tmp_path)
    _patch_build_result(
        monkeypatch,
        _build_result(BuildExecutionState.WAITING_FOR_NODE),
    )

    def forbidden_builder(*args, **kwargs):
        raise AssertionError("non-succeeded PF5 work must not reconstruct staging host")

    monkeypatch.setattr(
        staging_pass_module,
        "build_packaged_local_durable_build_host",
        forbidden_builder,
    )
    result = PackagedReviewedStagingPass(
        build_pass,
        _deployment(store),
        _staging_authority(store, authorize=False),
    ).advance_component(
        _prepared(),
        component_id=COMPONENT_ID,
    )

    assert result.build.state is BuildExecutionState.WAITING_FOR_NODE
    assert result.deployment is None


def test_succeeded_build_without_staging_authority_fails_before_handoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SQLiteStore(tmp_path / "missing-authority.db")
    store.initialize()
    build_pass = _build_pass(store, tmp_path)
    _patch_build_result(
        monkeypatch,
        _build_result(BuildExecutionState.SUCCEEDED),
    )
    monkeypatch.setattr(
        staging_pass_module,
        "build_packaged_local_durable_build_host",
        lambda *args, **kwargs: _fake_host(BuildExecutionState.SUCCEEDED),
    )

    class _ForbiddenHandoff:
        def __init__(self, *args, **kwargs):
            raise AssertionError("missing staging authority must block before handoff")

    monkeypatch.setattr(
        staging_pass_module,
        "BuildDeploymentHandoff",
        _ForbiddenHandoff,
    )

    with pytest.raises(
        PackagedStagingPassError,
        match="lacks current packaged staging authorization",
    ):
        PackagedReviewedStagingPass(
            build_pass,
            _deployment(store),
            _staging_authority(store, authorize=False),
        ).advance_component(
            _prepared(),
            component_id=COMPONENT_ID,
        )


def test_revoked_staging_authority_fails_before_handoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SQLiteStore(tmp_path / "revoked-authority.db")
    store.initialize()
    build_pass = _build_pass(store, tmp_path)
    _patch_build_result(
        monkeypatch,
        _build_result(BuildExecutionState.SUCCEEDED),
    )
    authority = _staging_authority(store, authorize=True)
    snapshot = authority.snapshot(
        project_id=PROJECT_ID,
        repository_id=REPOSITORY_ID,
        work_id=WORK_ID,
    )
    authority.revoke(
        project_id=PROJECT_ID,
        repository_id=REPOSITORY_ID,
        work_id=WORK_ID,
        expected_authority_digest=snapshot.authority_digest,
    )
    monkeypatch.setattr(
        staging_pass_module,
        "build_packaged_local_durable_build_host",
        lambda *args, **kwargs: _fake_host(BuildExecutionState.SUCCEEDED),
    )

    class _ForbiddenHandoff:
        def __init__(self, *args, **kwargs):
            raise AssertionError("revoked staging authority must block before handoff")

    monkeypatch.setattr(
        staging_pass_module,
        "BuildDeploymentHandoff",
        _ForbiddenHandoff,
    )

    with pytest.raises(
        PackagedStagingPassError,
        match="lacks current packaged staging authorization",
    ):
        PackagedReviewedStagingPass(
            build_pass,
            _deployment(store),
            authority,
        ).advance_component(
            _prepared(),
            component_id=COMPONENT_ID,
        )


def test_durable_pf5_state_drift_blocks_staging_before_authority_or_handoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SQLiteStore(tmp_path / "state-drift.db")
    store.initialize()
    build_pass = _build_pass(store, tmp_path)
    _patch_build_result(
        monkeypatch,
        _build_result(BuildExecutionState.SUCCEEDED),
    )
    authority = _staging_authority(store, authorize=True)
    monkeypatch.setattr(
        staging_pass_module,
        "build_packaged_local_durable_build_host",
        lambda *args, **kwargs: _fake_host(BuildExecutionState.FAILED),
    )

    with pytest.raises(
        PackagedStagingPassError,
        match="durable build state changed",
    ):
        PackagedReviewedStagingPass(
            build_pass,
            _deployment(store),
            authority,
        ).advance_component(
            _prepared(),
            component_id=COMPONENT_ID,
        )


def test_exact_authorized_succeeded_build_reaches_canonical_handoff_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SQLiteStore(tmp_path / "success.db")
    store.initialize()
    build_pass = _build_pass(store, tmp_path)
    _patch_build_result(
        monkeypatch,
        _build_result(BuildExecutionState.SUCCEEDED),
    )
    authority = _staging_authority(store, authorize=True)
    expected = _deployment_record(authority)
    fake_host = _fake_host(BuildExecutionState.SUCCEEDED)
    monkeypatch.setattr(
        staging_pass_module,
        "build_packaged_local_durable_build_host",
        lambda *args, **kwargs: fake_host,
    )
    calls: list[str] = []

    class _Handoff:
        def __init__(self, *, build_host, deployment, authority):
            assert build_host is fake_host
            assert type(deployment) is DurableDeploymentFabric
            assert authority is staging_authority
            self.authority = authority

        def deploy_staging(self, work_id: str):
            calls.append(work_id)
            self.authority.resolve(
                project_id=PROJECT_ID,
                repository_id=REPOSITORY_ID,
                work_id=work_id,
            )
            return expected

    staging_authority = authority
    monkeypatch.setattr(
        staging_pass_module,
        "BuildDeploymentHandoff",
        _Handoff,
    )

    result = PackagedReviewedStagingPass(
        build_pass,
        _deployment(store),
        authority,
    ).advance_component(
        _prepared(),
        component_id=COMPONENT_ID,
    )

    assert result.build.state is BuildExecutionState.SUCCEEDED
    assert result.deployment == expected
    assert calls == [WORK_ID]


def test_unaccepted_component_identity_is_rejected_without_staging_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SQLiteStore(tmp_path / "wrong-component.db")
    store.initialize()
    build_pass = _build_pass(store, tmp_path)
    _patch_build_result(
        monkeypatch,
        _build_result(BuildExecutionState.SUCCEEDED),
    )

    with pytest.raises(
        PackagedStagingPassError,
        match="not one exact accepted packaged build",
    ):
        PackagedReviewedStagingPass(
            build_pass,
            _deployment(store),
            _staging_authority(store, authorize=True),
        ).advance_component(
            _prepared(),
            component_id="other-component",
        )
