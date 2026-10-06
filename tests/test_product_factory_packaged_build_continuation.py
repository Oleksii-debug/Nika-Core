from __future__ import annotations

import asyncio
import os
import pathlib
import sys
from types import SimpleNamespace
from typing import Any, cast

import pytest

import nika_core.product_factory_packaged_build_continuation as continuation_module
from nika_core.data.sqlite import SQLiteStore
from nika_core.product_factory_build_execution import (
    BuildExecutionRecord,
    BuildExecutionState,
)
from nika_core.product_factory_coordinator import (
    ComponentWorkRequest,
    CoordinatorSnapshot,
    WorkRecord,
    WorkState,
)
from nika_core.product_factory_deployment import (
    ExecutionNode,
    NodeCapabilities,
    NodeIdentity,
    Platform,
    ResourceEnvelope,
)
from nika_core.product_factory_local_coding import ContainedLocalCodingPolicy
from nika_core.product_factory_multi_repository import MultiRepositoryExecutionState
from nika_core.product_factory_orchestration import (
    ProductComponent,
    ProductRepositoryGraph,
    RepositoryRef,
)
from nika_core.product_factory_packaged_build_authority import (
    PackagedBuildAuthorityRuntime,
)
from nika_core.product_factory_packaged_build_continuation import (
    PackagedAcceptedBuildContinuation,
    PackagedAcceptedBuildContinuationError,
)
from nika_core.product_factory_packaged_build_settings import (
    ActivatedPackagedBuildRuntime,
    ConfiguredPackagedReviewedBuildController,
    PackagedBuildRuntimeSettingsError,
)
from nika_core.product_factory_packaged_local_startup import (
    PackagedLocalProductFactoryStartup,
)
from nika_core.product_factory_packaged_preparation import PreparedProductFactory
from nika_core.toolsmith.contracts import ResourceBudget

PROJECT_ID = "product-" + "a" * 64


class _Coordinator:
    def __init__(self, records: tuple[WorkRecord, ...]) -> None:
        self._records = records

    def snapshot(self) -> CoordinatorSnapshot:
        return CoordinatorSnapshot(
            project_id=PROJECT_ID,
            revision=1,
            records=self._records,
        )


def _request(
    component_id: str,
    repository_id: str,
    *,
    work_suffix: str,
) -> ComponentWorkRequest:
    return ComponentWorkRequest(
        work_id=f"work-{work_suffix}",
        project_id=PROJECT_ID,
        component_id=component_id,
        repository_id=repository_id,
        goal=f"Build {component_id}",
        base_sha="b" * 40,
        allowed_paths=(component_id,),
        permission_ceiling=frozenset({"build_release"}),
        acceptance_commands=(("python", "-m", "pytest"),),
    )


def _record(
    component_id: str,
    repository_id: str,
    *,
    work_suffix: str,
    state: WorkState = WorkState.ACCEPTED,
) -> WorkRecord:
    return WorkRecord(
        request=_request(
            component_id,
            repository_id,
            work_suffix=work_suffix,
        ),
        state=state,
    )


def _graph() -> ProductRepositoryGraph:
    return ProductRepositoryGraph(
        project_id=PROJECT_ID,
        repositories=(
            RepositoryRef("repo-a", "local", "repo-a", "main"),
            RepositoryRef("repo-b", "local", "repo-b", "main"),
        ),
        components=(
            ProductComponent("core", "repo-a", ("core",)),
            ProductComponent(
                "desktop",
                "repo-b",
                ("desktop",),
                dependencies=("core",),
            ),
        ),
    )


def _prepared(records: tuple[WorkRecord, ...]) -> PreparedProductFactory:
    state = MultiRepositoryExecutionState(
        authority=cast(
            Any,
            SimpleNamespace(
                project_id=PROJECT_ID,
                graph=_graph(),
            ),
        ),
        binding=cast(Any, object()),
        coordinator=cast(Any, _Coordinator(records)),
    )
    return PreparedProductFactory(
        host_task_id="host-task-1",
        state=state,
    )


def _activation(
    configured: frozenset[tuple[str, str, str]],
) -> ActivatedPackagedBuildRuntime:
    platform = Platform.WINDOWS if os.name == "nt" else Platform.LINUX
    node = ExecutionNode(
        NodeIdentity("local-build", platform, "x86_64", "desktop"),
        NodeCapabilities(
            frozenset({"build"}),
            frozenset({"python"}),
            False,
        ),
        ResourceEnvelope(2, 2048, 4096),
        enabled=True,
    )
    runtime = object.__new__(PackagedBuildAuthorityRuntime)
    return ActivatedPackagedBuildRuntime(
        node=node,
        runtime=runtime,
        configured_components=configured,
    )


def _startup(tmp_path: pathlib.Path) -> PackagedLocalProductFactoryStartup:
    executable = str(pathlib.Path(sys.executable).resolve())
    workspace = (tmp_path / "pf5 workspaces").resolve()
    workspace.mkdir()
    return PackagedLocalProductFactoryStartup(
        workspace_parent=workspace,
        policy=ContainedLocalCodingPolicy(
            allowed_executables=(executable,),
            resource_budget=ResourceBudget(30, 1024 * 1024, 100),
            lease_seconds=300,
        ),
        git_executable=pathlib.Path(executable),
    )


def _continuation(
    tmp_path: pathlib.Path,
    *,
    configured: frozenset[tuple[str, str, str]],
    max_components: int = 32,
) -> PackagedAcceptedBuildContinuation:
    return PackagedAcceptedBuildContinuation(
        store=SQLiteStore((tmp_path / "nika.db").resolve()),
        startup=_startup(tmp_path),
        activation=_activation(configured),
        max_components=max_components,
    )


def _build_record(
    work_id: str,
    state: BuildExecutionState,
) -> BuildExecutionRecord:
    record = object.__new__(BuildExecutionRecord)
    object.__setattr__(
        record,
        "spec",
        SimpleNamespace(request=SimpleNamespace(work_id=work_id)),
    )
    object.__setattr__(record, "state", state)
    return record


def _configured_controller(
    reconcile,
) -> ConfiguredPackagedReviewedBuildController:
    configured = object.__new__(ConfiguredPackagedReviewedBuildController)
    object.__setattr__(
        configured,
        "controller",
        SimpleNamespace(reconcile_work=reconcile),
    )
    return configured


def test_membership_is_preflighted_for_all_accepted_work_before_pf5_effect(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = _prepared(
        (
            _record("core", "repo-a", work_suffix="core"),
            _record("desktop", "repo-b", work_suffix="desktop"),
        )
    )
    continuation = _continuation(
        tmp_path,
        configured=frozenset({(PROJECT_ID, "repo-a", "core")}),
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("partial PF5 composition must not start")

    monkeypatch.setattr(
        continuation_module,
        "build_configured_packaged_reviewed_build_controller",
        forbidden,
    )

    with pytest.raises(PackagedBuildRuntimeSettingsError, match="активної"):
        continuation.advance(prepared)


def test_accepted_work_advances_in_dependency_order_and_reconciles_once(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = _prepared(
        (
            _record("desktop", "repo-b", work_suffix="desktop"),
            _record("core", "repo-a", work_suffix="core"),
        )
    )
    continuation = _continuation(
        tmp_path,
        configured=frozenset(
            {
                (PROJECT_ID, "repo-a", "core"),
                (PROJECT_ID, "repo-b", "desktop"),
            }
        ),
    )
    events: list[tuple[str, str]] = []

    def reconcile(work_id: str) -> BuildExecutionRecord:
        events.append(("reconcile", work_id))
        return _build_record(work_id, BuildExecutionState.SUCCEEDED)

    configured = _configured_controller(reconcile)

    def build_controller(
        store,
        *,
        host_task_id,
        project_id,
        startup,
        activation,
    ):
        assert store is continuation.store
        assert host_task_id == prepared.host_task_id
        assert project_id == PROJECT_ID
        assert startup is continuation.startup
        assert activation is continuation.activation
        return configured

    def advance_component(_self, *, state, component_id):
        assert state is prepared.state
        events.append(("advance", component_id))
        if component_id == "core":
            return _build_record("pf5-core", BuildExecutionState.SUCCEEDED)
        return _build_record(
            "pf5-desktop",
            BuildExecutionState.RECONCILE_REQUIRED,
        )

    monkeypatch.setattr(
        continuation_module,
        "build_configured_packaged_reviewed_build_controller",
        build_controller,
    )
    monkeypatch.setattr(
        ConfiguredPackagedReviewedBuildController,
        "advance_component",
        advance_component,
    )

    result = continuation.advance(prepared)

    assert events == [
        ("advance", "core"),
        ("advance", "desktop"),
        ("reconcile", "pf5-desktop"),
    ]
    assert result.project_id == PROJECT_ID
    assert tuple(record.state for record in result.records) == (
        BuildExecutionState.SUCCEEDED,
        BuildExecutionState.SUCCEEDED,
    )


def test_continuation_bounds_accepted_set_before_controller_composition(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = _prepared(
        (
            _record("core", "repo-a", work_suffix="core"),
            _record("desktop", "repo-b", work_suffix="desktop"),
        )
    )
    continuation = _continuation(
        tmp_path,
        configured=frozenset(
            {
                (PROJECT_ID, "repo-a", "core"),
                (PROJECT_ID, "repo-b", "desktop"),
            }
        ),
        max_components=1,
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("oversized PF5 continuation must not compose a host")

    monkeypatch.setattr(
        continuation_module,
        "build_configured_packaged_reviewed_build_controller",
        forbidden,
    )

    with pytest.raises(
        PackagedAcceptedBuildContinuationError,
        match="bounded continuation",
    ):
        continuation.advance(prepared)


def test_duplicate_accepted_component_fails_before_controller_composition(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = _prepared(
        (
            _record("core", "repo-a", work_suffix="core-1"),
            _record("core", "repo-a", work_suffix="core-2"),
        )
    )
    continuation = _continuation(
        tmp_path,
        configured=frozenset({(PROJECT_ID, "repo-a", "core")}),
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("ambiguous PF5 continuation must not compose a host")

    monkeypatch.setattr(
        continuation_module,
        "build_configured_packaged_reviewed_build_controller",
        forbidden,
    )

    with pytest.raises(
        PackagedAcceptedBuildContinuationError,
        match="duplicate accepted component",
    ):
        continuation.advance(prepared)


def test_nonaccepted_work_does_not_compose_pf5_host(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = _prepared(
        (
            _record(
                "core",
                "repo-a",
                work_suffix="core",
                state=WorkState.REVIEW_REQUIRED,
            ),
        )
    )
    continuation = _continuation(tmp_path, configured=frozenset())

    def forbidden(*_args, **_kwargs):
        raise AssertionError("empty PF5 continuation must not compose a host")

    monkeypatch.setattr(
        continuation_module,
        "build_configured_packaged_reviewed_build_controller",
        forbidden,
    )

    result = continuation.advance(prepared)

    assert result.project_id == PROJECT_ID
    assert result.records == ()


def test_async_post_dispatch_hook_delegates_to_bounded_continuation(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = _prepared(())
    continuation = _continuation(tmp_path, configured=frozenset())
    seen: list[PreparedProductFactory] = []

    def advance(_self, value: PreparedProductFactory):
        seen.append(value)
        return SimpleNamespace(project_id=PROJECT_ID, records=())

    monkeypatch.setattr(
        PackagedAcceptedBuildContinuation,
        "advance",
        advance,
    )

    asyncio.run(continuation.run_after_dispatch(prepared))

    assert seen == [prepared]
