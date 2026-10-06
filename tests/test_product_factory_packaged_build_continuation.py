from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from typing import Any, cast

import pytest

import nika_core.product_factory_packaged_build_continuation as continuation_module
from nika_core.data.sqlite import SQLiteStore
from nika_core.product_factory_build_execution import BuildExecutionState
from nika_core.product_factory_coordinator import WorkState
from nika_core.product_factory_deployment import (
    ExecutionNode,
    NodeCapabilities,
    NodeIdentity,
    Platform,
    ResourceEnvelope,
)
from nika_core.product_factory_local_coding import ContainedLocalCodingPolicy
from nika_core.product_factory_packaged_build_authority import (
    PackagedBuildAuthorityRuntime,
)
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
        "build_configured_packaged_reviewed_build_controller",
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
        "build_configured_packaged_reviewed_build_controller",
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
    calls = []

    class Controller:
        def advance_component(self, *, state, component_id):
            calls.append((state, component_id))
            return SimpleNamespace(
                state=BuildExecutionState.SUCCEEDED,
                spec=SimpleNamespace(
                    request=SimpleNamespace(
                        project_id=PROJECT_ID,
                        work_id=f"pf5-{component_id}",
                    )
                ),
            )

    def build_controller(
        store_value,
        *,
        host_task_id,
        project_id,
        startup,
        activation,
    ):
        assert store_value is store
        assert host_task_id == "host-task"
        assert project_id == PROJECT_ID
        assert startup is continuation.startup
        assert activation is continuation.activated
        return Controller()

    monkeypatch.setattr(
        continuation_module,
        "build_configured_packaged_reviewed_build_controller",
        build_controller,
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


def test_continuation_default_bound_allows_second_pf4_wave(
    tmp_path,
    monkeypatch,
):
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    component_count = 33
    configured = {
        (PROJECT_ID, f"repo-{index}", f"component-{index}")
        for index in range(component_count)
    }
    continuation = PackagedReviewedBuildContinuation(
        store,
        _startup(tmp_path),
        _activated(configured),
    )
    calls = []

    class Controller:
        def advance_component(self, *, state, component_id):
            calls.append(component_id)
            return SimpleNamespace(
                state=BuildExecutionState.SUCCEEDED,
                spec=SimpleNamespace(
                    request=SimpleNamespace(
                        project_id=PROJECT_ID,
                        work_id=f"pf5-{component_id}",
                    )
                ),
            )

    monkeypatch.setattr(
        continuation_module,
        "build_configured_packaged_reviewed_build_controller",
        lambda *_args, **_kwargs: Controller(),
    )
    records = [
        _record(
            f"component-{index}",
            f"repo-{index}",
            WorkState.ACCEPTED,
        )
        for index in range(component_count)
    ]

    continuation._advance(_prepared(records))

    assert calls == [f"component-{index}" for index in range(component_count)]


def test_continuation_rejects_accepted_batch_over_bound_before_pf5(
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
        max_components=1,
    )
    built = False

    def forbidden_controller(*_args, **_kwargs):
        nonlocal built
        built = True
        raise AssertionError("oversized accepted batch must not compose PF5")

    monkeypatch.setattr(
        continuation_module,
        "build_configured_packaged_reviewed_build_controller",
        forbidden_controller,
    )

    with pytest.raises(
        continuation_module.PackagedBuildContinuationError,
        match="component bound",
    ):
        continuation._advance(
            _prepared(
                [
                    _record("component-a", "repo-a", WorkState.ACCEPTED),
                    _record("component-b", "repo-b", WorkState.ACCEPTED),
                ]
            )
        )

    assert built is False


@pytest.mark.parametrize("value", [0, 129, True, 1.5])
def test_continuation_rejects_invalid_component_bound(tmp_path, value):
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()

    with pytest.raises(ValueError, match="max_components"):
        PackagedReviewedBuildContinuation(
            store,
            _startup(tmp_path),
            _activated({(PROJECT_ID, "repo-a", "component-a")}),
            max_components=value,
        )


def test_continuation_reconciles_uncertain_pf5_once_without_replay(
    tmp_path,
    monkeypatch,
):
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    continuation = PackagedReviewedBuildContinuation(
        store,
        _startup(tmp_path),
        _activated({(PROJECT_ID, "repo-a", "component-a")}),
    )
    calls = []

    class InnerController:
        def reconcile_work(self, work_id):
            calls.append(("reconcile", work_id))
            return SimpleNamespace(
                state=BuildExecutionState.SUCCEEDED,
                spec=SimpleNamespace(
                    request=SimpleNamespace(
                        project_id=PROJECT_ID,
                        work_id=work_id,
                    )
                ),
            )

    class Controller:
        controller = InnerController()

        def advance_component(self, *, state, component_id):
            calls.append(("advance", component_id))
            return SimpleNamespace(
                state=BuildExecutionState.RECONCILE_REQUIRED,
                spec=SimpleNamespace(
                    request=SimpleNamespace(
                        project_id=PROJECT_ID,
                        work_id="pf5-component-a",
                    )
                ),
            )

    monkeypatch.setattr(
        continuation_module,
        "build_configured_packaged_reviewed_build_controller",
        lambda *_args, **_kwargs: Controller(),
    )

    continuation._advance(
        _prepared(
            [_record("component-a", "repo-a", WorkState.ACCEPTED)]
        )
    )

    assert calls == [
        ("advance", "component-a"),
        ("reconcile", "pf5-component-a"),
    ]


def test_effect_admission_guard_blocks_next_component_after_settings_drift(
    tmp_path,
    monkeypatch,
):
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    allowed = True
    continuation = PackagedReviewedBuildContinuation(
        store,
        _startup(tmp_path),
        _activated(
            {
                (PROJECT_ID, "repo-a", "component-a"),
                (PROJECT_ID, "repo-b", "component-b"),
            }
        ),
        effect_admission_guard=lambda: allowed,
    )
    calls = []

    class Controller:
        def advance_component(self, *, state, component_id):
            nonlocal allowed
            calls.append(component_id)
            if component_id == "component-a":
                allowed = False
            return SimpleNamespace(
                state=BuildExecutionState.SUCCEEDED,
                spec=SimpleNamespace(
                    request=SimpleNamespace(
                        project_id=PROJECT_ID,
                        work_id=f"pf5-{component_id}",
                    )
                ),
            )

    monkeypatch.setattr(
        continuation_module,
        "build_configured_packaged_reviewed_build_controller",
        lambda *_args, **_kwargs: Controller(),
    )

    with pytest.raises(
        continuation_module.PackagedBuildContinuationError,
        match="effect authority changed",
    ):
        continuation._advance(
            _prepared(
                [
                    _record("component-a", "repo-a", WorkState.ACCEPTED),
                    _record("component-b", "repo-b", WorkState.ACCEPTED),
                ]
            )
        )

    assert calls == ["component-a"]


def test_continuation_settles_current_pf5_work_without_starting_next_after_cancel(
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
    prepared = _prepared(
        [
            _record("component-a", "repo-a", WorkState.ACCEPTED),
            _record("component-b", "repo-b", WorkState.ACCEPTED),
        ]
    )
    first_started = threading.Event()
    release_first = threading.Event()
    calls = []

    class Controller:
        def advance_component(self, *, state, component_id):
            assert state is prepared.state
            calls.append(component_id)
            if component_id == "component-a":
                first_started.set()
                if not release_first.wait(timeout=5):
                    raise AssertionError("first PF5 work was never released")
            return SimpleNamespace(
                state=BuildExecutionState.SUCCEEDED,
                spec=SimpleNamespace(
                    request=SimpleNamespace(
                        project_id=PROJECT_ID,
                        work_id=f"pf5-{component_id}",
                    )
                ),
            )

    monkeypatch.setattr(
        continuation_module,
        "build_configured_packaged_reviewed_build_controller",
        lambda *_args, **_kwargs: Controller(),
    )

    async def scenario():
        task = asyncio.create_task(continuation(prepared))
        assert await asyncio.to_thread(first_started.wait, 5)

        task.cancel()
        await asyncio.sleep(0)
        assert task.done() is False
        assert calls == ["component-a"]

        task.cancel()
        await asyncio.sleep(0)
        assert task.done() is False
        assert calls == ["component-a"]

        release_first.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    assert calls == ["component-a"]

