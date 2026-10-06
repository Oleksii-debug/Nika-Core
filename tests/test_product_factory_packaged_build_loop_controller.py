from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from nika_core.product_factory_build_deployment_handoff import BuildDeploymentHandoff
from nika_core.product_factory_build_execution import (
    BuildExecutionError,
    BuildExecutionRecord,
    BuildExecutionScopeRequest,
    BuildExecutionSpec,
    BuildExecutionState,
    ExecutionGrant,
)
from nika_core.product_factory_build_execution_host import DurableBuildExecutionHost
from nika_core.product_factory_deployment import (
    ExecutionRequest,
    Platform,
    ResourceEnvelope,
)
from nika_core.product_factory_multi_repository import MultiRepositoryExecutionState
from nika_core.product_factory_packaged_build_authority import (
    PackagedBuildAuthorityRuntime,
)
from nika_core.product_factory_packaged_build_loop import (
    PackagedReviewedBuildLoopController,
    PackagedReviewedBuildLoopError,
)

PROJECT_ID = "product-packaged-loop"
REPOSITORY_ID = "repo-main"
COMPONENT_ID = "desktop"
WORK_ID = "pf5-build:" + "a" * 64
SOURCE_SHA = "b" * 40
NODE_ID = "build-node"


def _spec() -> BuildExecutionSpec:
    return BuildExecutionSpec(
        request=ExecutionRequest(
            project_id=PROJECT_ID,
            work_id=WORK_ID,
            platform=Platform.LINUX,
            required_features=frozenset({"build"}),
            required_toolchains=frozenset({"python"}),
            resources=ResourceEnvelope(1, 1024, 2048),
        ),
        source_sha=SOURCE_SHA,
        scope=BuildExecutionScopeRequest(
            repository_id=REPOSITORY_ID,
            workspace_relpath="products/build",
            requested_node_ids=(NODE_ID,),
            command_id="build",
        ),
        lease_seconds=120,
    )


def _grant() -> ExecutionGrant:
    return ExecutionGrant(
        project_id=PROJECT_ID,
        repository_id=REPOSITORY_ID,
        work_id=WORK_ID,
        workspace_relpath="products/build",
        allowed_node_ids=(NODE_ID,),
        network_scopes=(),
        credential_refs=(),
        command_id="build",
        argv=("python", "-m", "build"),
        authority_evidence_refs=("authority://packaged-build",),
    )


def _record(state: BuildExecutionState) -> BuildExecutionRecord:
    return BuildExecutionRecord(
        spec=_spec(),
        grant=_grant(),
        state=state,
        node_id=(
            NODE_ID
            if state
            not in {
                BuildExecutionState.PENDING,
                BuildExecutionState.WAITING_FOR_NODE,
                BuildExecutionState.WAITING_FOR_AUTHORITY,
            }
            else None
        ),
        lease_id="lease-1" if state is BuildExecutionState.PREPARED else None,
        attempt=1 if state is not BuildExecutionState.PENDING else 0,
    )


def _runtime() -> PackagedBuildAuthorityRuntime:
    return object.__new__(PackagedBuildAuthorityRuntime)


def _host() -> DurableBuildExecutionHost:
    return object.__new__(DurableBuildExecutionHost)


def _state() -> MultiRepositoryExecutionState:
    return MultiRepositoryExecutionState(
        authority=object(),
        binding=object(),
        coordinator=object(),
    )


def _patch_admission(monkeypatch: pytest.MonkeyPatch) -> None:
    def admit(
        _self,
        *,
        authority,
        coordinator,
        component_id: str,
    ) -> BuildExecutionSpec:
        assert authority is not None
        assert coordinator is not None
        assert component_id == COMPONENT_ID
        return _spec()

    monkeypatch.setattr(
        PackagedBuildAuthorityRuntime,
        "admit_reviewed_component",
        admit,
    )


def test_advance_component_uses_canonical_pf5_transition_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime()
    host = _host()
    calls: list[str] = []
    _patch_admission(monkeypatch)

    def submit(_self, spec: BuildExecutionSpec) -> BuildExecutionRecord:
        assert spec == _spec()
        calls.append("submit")
        return _record(BuildExecutionState.PENDING)

    def prepare(_self, work_id: str) -> BuildExecutionRecord:
        assert work_id == WORK_ID
        calls.append("prepare")
        return _record(BuildExecutionState.PREPARED)

    def begin_dispatch(_self, work_id: str):
        assert work_id == WORK_ID
        calls.append("begin_dispatch")
        return object()

    def execute(_self, work_id: str) -> BuildExecutionRecord:
        assert work_id == WORK_ID
        calls.append("execute")
        return _record(BuildExecutionState.SUCCEEDED)

    monkeypatch.setattr(DurableBuildExecutionHost, "submit", submit)
    monkeypatch.setattr(DurableBuildExecutionHost, "prepare", prepare)
    monkeypatch.setattr(DurableBuildExecutionHost, "begin_dispatch", begin_dispatch)
    monkeypatch.setattr(DurableBuildExecutionHost, "execute", execute)

    controller = PackagedReviewedBuildLoopController(runtime, host)
    result = controller.advance_component(
        state=_state(),
        component_id=COMPONENT_ID,
    )

    assert result.state is BuildExecutionState.SUCCEEDED
    assert result.spec.request.work_id == WORK_ID
    assert calls == ["submit", "prepare", "begin_dispatch", "execute"]


@pytest.mark.parametrize(
    "uncertain_state",
    (
        BuildExecutionState.EFFECT_IN_FLIGHT,
        BuildExecutionState.RECONCILE_REQUIRED,
    ),
)
def test_advance_component_never_blind_replays_uncertain_effect(
    monkeypatch: pytest.MonkeyPatch,
    uncertain_state: BuildExecutionState,
) -> None:
    runtime = _runtime()
    host = _host()
    _patch_admission(monkeypatch)

    monkeypatch.setattr(
        DurableBuildExecutionHost,
        "submit",
        lambda _self, _spec_value: _record(uncertain_state),
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("uncertain PF5 work must not be replayed")

    monkeypatch.setattr(DurableBuildExecutionHost, "prepare", forbidden)
    monkeypatch.setattr(DurableBuildExecutionHost, "begin_dispatch", forbidden)
    monkeypatch.setattr(DurableBuildExecutionHost, "execute", forbidden)

    result = PackagedReviewedBuildLoopController(
        runtime,
        host,
    ).advance_component(
        state=_state(),
        component_id=COMPONENT_ID,
    )

    assert result.state is uncertain_state


def test_reconcile_is_explicit_and_uses_canonical_inspection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime()
    host = _host()
    uncertain = _record(BuildExecutionState.RECONCILE_REQUIRED)
    succeeded = replace(uncertain, state=BuildExecutionState.SUCCEEDED)
    calls: list[str] = []

    monkeypatch.setattr(
        DurableBuildExecutionHost,
        "snapshot",
        lambda _self: SimpleNamespace(
            coordinator=SimpleNamespace(records=(uncertain,))
        ),
    )

    def reconcile(_self, work_id: str) -> BuildExecutionRecord:
        assert work_id == WORK_ID
        calls.append("reconcile")
        return succeeded

    monkeypatch.setattr(DurableBuildExecutionHost, "reconcile", reconcile)

    result = PackagedReviewedBuildLoopController(
        runtime,
        host,
    ).reconcile_work(WORK_ID)

    assert result.state is BuildExecutionState.SUCCEEDED
    assert calls == ["reconcile"]


def test_reconcile_does_not_inspect_non_uncertain_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime()
    host = _host()
    succeeded = _record(BuildExecutionState.SUCCEEDED)

    monkeypatch.setattr(
        DurableBuildExecutionHost,
        "snapshot",
        lambda _self: SimpleNamespace(
            coordinator=SimpleNamespace(records=(succeeded,))
        ),
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("terminal PF5 work does not need reconciliation")

    monkeypatch.setattr(DurableBuildExecutionHost, "reconcile", forbidden)

    result = PackagedReviewedBuildLoopController(
        runtime,
        host,
    ).reconcile_work(WORK_ID)

    assert result is succeeded


def test_staging_requires_explicit_canonical_handoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime()
    host = _host()
    controller = PackagedReviewedBuildLoopController(runtime, host)

    with pytest.raises(
        PackagedReviewedBuildLoopError,
        match="staging handoff is not configured",
    ):
        controller.deploy_staging(WORK_ID)

    handoff = object.__new__(BuildDeploymentHandoff)
    object.__setattr__(handoff, "build_host", host)
    sentinel = object()
    monkeypatch.setattr(
        BuildDeploymentHandoff,
        "deploy_staging",
        lambda _self, work_id: sentinel if work_id == WORK_ID else None,
    )

    configured = PackagedReviewedBuildLoopController(
        runtime,
        host,
        handoff,
    )
    assert configured.deploy_staging(WORK_ID) is sentinel


def test_handoff_cannot_point_at_a_different_pf5_host() -> None:
    runtime = _runtime()
    host = _host()
    different_host = _host()
    handoff = object.__new__(BuildDeploymentHandoff)
    object.__setattr__(handoff, "build_host", different_host)

    with pytest.raises(
        PackagedReviewedBuildLoopError,
        match="share the exact packaged PF5 durable host",
    ):
        PackagedReviewedBuildLoopController(runtime, host, handoff)


@pytest.mark.parametrize("value", ("", " desktop", "desktop\nother"))
def test_component_identity_is_strict(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    _patch_admission(monkeypatch)
    controller = PackagedReviewedBuildLoopController(_runtime(), _host())

    with pytest.raises(PackagedReviewedBuildLoopError):
        controller.advance_component(state=_state(), component_id=value)


def test_pre_dispatch_availability_loss_is_settled_through_durable_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime()
    host = _host()
    _patch_admission(monkeypatch)
    waiting = _record(BuildExecutionState.WAITING_FOR_NODE)
    retry_calls: list[str] = []

    monkeypatch.setattr(
        DurableBuildExecutionHost,
        "submit",
        lambda _self, _spec_value: _record(BuildExecutionState.PENDING),
    )
    monkeypatch.setattr(
        DurableBuildExecutionHost,
        "prepare",
        lambda _self, _work_id: _record(BuildExecutionState.PREPARED),
    )

    def lose_node(_self, _work_id: str):
        raise BuildExecutionError("selected node became unavailable")

    monkeypatch.setattr(
        DurableBuildExecutionHost,
        "begin_dispatch",
        lose_node,
    )
    object.__setattr__(
        host,
        "coordinator",
        SimpleNamespace(get=lambda work_id: waiting if work_id == WORK_ID else None),
    )

    def retry(_self, work_id: str) -> BuildExecutionRecord:
        retry_calls.append(work_id)
        return waiting

    monkeypatch.setattr(DurableBuildExecutionHost, "retry", retry)

    result = PackagedReviewedBuildLoopController(
        runtime,
        host,
    ).advance_component(
        state=_state(),
        component_id=COMPONENT_ID,
    )

    assert result.state is BuildExecutionState.WAITING_FOR_NODE
    assert retry_calls == [WORK_ID]


def test_pre_dispatch_unclassified_failure_remains_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime()
    host = _host()
    _patch_admission(monkeypatch)
    prepared = _record(BuildExecutionState.PREPARED)

    monkeypatch.setattr(
        DurableBuildExecutionHost,
        "submit",
        lambda _self, _spec_value: _record(BuildExecutionState.PENDING),
    )
    monkeypatch.setattr(
        DurableBuildExecutionHost,
        "prepare",
        lambda _self, _work_id: prepared,
    )

    def explode(_self, _work_id: str):
        raise BuildExecutionError("lease identity mismatch")

    monkeypatch.setattr(
        DurableBuildExecutionHost,
        "begin_dispatch",
        explode,
    )
    object.__setattr__(
        host,
        "coordinator",
        SimpleNamespace(get=lambda work_id: prepared if work_id == WORK_ID else None),
    )

    def forbidden_retry(*_args, **_kwargs):
        raise AssertionError("unclassified dispatch failure must not be normalized")

    monkeypatch.setattr(DurableBuildExecutionHost, "retry", forbidden_retry)

    with pytest.raises(BuildExecutionError, match="lease identity mismatch"):
        PackagedReviewedBuildLoopController(
            runtime,
            host,
        ).advance_component(
            state=_state(),
            component_id=COMPONENT_ID,
        )
