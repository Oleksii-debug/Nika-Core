from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest

import nika_core.product_factory_packaged_build_continuation as continuation_module
from nika_core.data.sqlite import SQLiteStore
from nika_core.product_factory_coordinator import WorkState
from nika_core.product_factory_deployment import (
    ExecutionNode,
    NodeCapabilities,
    NodeIdentity,
    Platform,
    ResourceEnvelope,
)
from nika_core.product_factory_packaged_build_authority import PackagedBuildAuthorityRuntime
from nika_core.product_factory_packaged_build_continuation import (
    PackagedReviewedBuildContinuation,
)
from nika_core.product_factory_packaged_build_settings import (
    ActivatedPackagedBuildRuntime,
    PackagedBuildRuntimeSettingsError,
)
from nika_core.product_factory_packaged_local_startup import (
    PackagedLocalProductFactoryStartup,
)
from nika_core.product_factory_packaged_preparation import PreparedProductFactory
from nika_core.product_factory_local_coding import ContainedLocalCodingPolicy
from nika_core.toolsmith.contracts import ResourceBudget

PROJECT_ID = "product-" + "a" * 64


def _startup(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executable = tmp_path / "python"
    executable.write_text("stub", encoding="utf-8")
    git = tmp_path / "git"
    git.write_text("stub", encoding="utf-8")
    return PackagedLocalProductFactoryStartup(
        workspace_parent=workspace,
        policy=ContainedLocalCodingPolicy(
            allowed_executables=(str(executable),),
            resource_budget=ResourceBudget(60, 1024 * 1024, 32),
            lease_seconds=120,
        ),
        git_executable=git,
    )


def _activated(configured_components):
    node = ExecutionNode(
        NodeIdentity("build-node", Platform.LINUX, "test", "instance"),
        NodeCapabilities(frozenset({"build"}), frozenset({"python"}), False),
        ResourceEnvelope(2, 2048, 4096),
    )
    runtime = object.__new__(PackagedBuildAuthorityRuntime)
    return ActivatedPackagedBuildRuntime(
        node=node,
        runtime=runtime,
        configured_components=frozenset(configured_components),
    )


def _record(component_id: str, repository_id: str, state: WorkState):
    request = SimpleNamespace(
        component_id=component_id,
        repository_id=repository_id,
    )
    return SimpleNamespace(request=request, state=state)


def _prepared(records):
    coordinator = SimpleNamespace(snapshot=lambda: SimpleNamespace(records=tuple(records)))
    state = SimpleNamespace(
        authority=SimpleNamespace(project_id=PROJECT_ID),
        coordinator=coordinator,
    )
    return PreparedProductFactory(
        host_task_id="host-task",
        state=cast(Any, state),
    )


def test_continuation_does_nothing_without_accepted_work(tmp_path, monkeypatch):
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    continuation = PackagedReviewedBuildContinuation(
        store,
        _startup(tmp_path),
        _activated({(PROJECT_ID, "repo", "component")}),
    )

    monkeypatch.setattr(
        continuation_module,
        "build_packaged_local_durable_build_host",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("PF5 host must not be built without accepted work")
        ),
    )

    asyncio.run(
        continuation(
            _prepared([_record("component", "repo", WorkState.REVIEW_REQUIRED)])
        )
    )


def test_continuation_validates_all_accepted_membership_before_pf5(tmp_path, monkeypatch):
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    continuation = PackagedReviewedBuildContinuation(
        store,
        _startup(tmp_path),
        _activated({(PROJECT_ID, "repo-a", "component-a")}),
    )
    built = False

    def forbidden_host(*_args, **_kwargs):
        nonlocal built
        built = True
        raise AssertionError("partial PF5 batch must not start")

    monkeypatch.setattr(
        continuation_module,
        "build_packaged_local_durable_build_host",
        forbidden_host,
    )

    with pytest.raises(PackagedBuildRuntimeSettingsError, match="не має активної"):
        continuation._advance(
            _prepared(
                [
                    _record("component-a", "repo-a", WorkState.ACCEPTED),
                    _record("component-b", "repo-b", WorkState.ACCEPTED),
                ]
            )
        )

    assert built is False


def test_continuation_advances_each_accepted_component_in_snapshot_order(
    tmp_path,
    monkeypatch,
):
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    continuation = PackagedReviewedBuildContinuation(
        store,
        _startup(tmp_path),
        _activated(
            {
                (PROJECT_ID, "repo-a", "component-a"),
                (PROJECT_ID, "repo-b", "component-b"),
            }
        ),
    )
    host = object()
    calls = []

    monkeypatch.setattr(
        continuation_module,
        "build_packaged_local_durable_build_host",
        lambda *_args, **_kwargs: host,
    )

    class Controller:
        def __init__(self, runtime, received_host):
            assert runtime is continuation.activated.runtime
            assert received_host is host

        def advance_component(self, *, state, component_id):
            calls.append((state, component_id))
            return SimpleNamespace(
                spec=SimpleNamespace(request=SimpleNamespace(project_id=PROJECT_ID))
            )

    monkeypatch.setattr(
        continuation_module,
        "PackagedReviewedBuildLoopController",
        Controller,
    )
    prepared = _prepared(
        [
            _record("component-a", "repo-a", WorkState.ACCEPTED),
            _record("waiting", "repo-w", WorkState.REVIEW_REQUIRED),
            _record("component-b", "repo-b", WorkState.ACCEPTED),
        ]
    )

    continuation._advance(prepared)

    assert [component_id for _state, component_id in calls] == [
        "component-a",
        "component-b",
    ]
    assert all(state is prepared.state for state, _component_id in calls)
