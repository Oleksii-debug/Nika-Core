from __future__ import annotations

import asyncio
from concurrent.futures import Future
from pathlib import Path
from typing import Any, cast

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_command.product_project_adapter import ProductProjectCommandService
from nika_core.product_factory_orchestration import (
    ProductComponent,
    ProductRepositoryGraph,
    RepositoryRef,
)
from nika_core.product_factory_packaged_execution import (
    PackagedProductFactoryExecutionController,
    PackagedProductFactoryExecutionError,
)
from nika_core.product_factory_packaged_journey import (
    PackagedProductCommandRouter,
    PackagedProductJourneyError,
    PackagedProductSelectionStore,
    packaged_run_current_product_factory_command,
    product_project_identity,
)
from nika_core.product_factory_packaged_preparation import (
    PackagedProductFactoryExecutionPlan,
    PreparedProductFactory,
)
from nika_core.product_project import ProductProjectRepository
from nika_core.ui.bridge_models import UIResult

_PROJECT_ID = "product-" + "a" * 64
_OTHER_PROJECT_ID = "product-" + "b" * 64


def _plan(project_id: str = _PROJECT_ID) -> PackagedProductFactoryExecutionPlan:
    graph = ProductRepositoryGraph(
        project_id=project_id,
        repositories=(
            RepositoryRef(
                repository_id="repo-core",
                provider="github",
                locator="Oleksii-debug/Nika-Core",
                default_branch="main",
            ),
        ),
        components=(
            ProductComponent(
                component_id="core",
                repository_id="repo-core",
                paths=("src/nika_core",),
                test_commands=(("python", "-m", "pytest", "tests"),),
            ),
        ),
    )
    return PackagedProductFactoryExecutionPlan(
        project_id=project_id,
        expected_spec_version=1,
        expected_row_version=0,
        graph=graph,
        graph_version=1,
        base_shas={"repo-core": "c" * 40},
        component_goals={"core": "Implement exact accepted ProductProject work"},
        permission_ceiling=frozenset({"read_source", "write_source", "run_tests"}),
    )


class _Preparation:
    def __init__(self) -> None:
        self.plans: list[PackagedProductFactoryExecutionPlan] = []
        self.prepared = PreparedProductFactory(
            host_task_id="host-task",
            state=cast(Any, object()),
        )

    def prepare(
        self,
        plan: PackagedProductFactoryExecutionPlan,
    ) -> PreparedProductFactory:
        self.plans.append(plan)
        return self.prepared


class _Host:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, object, int, int | None]] = []

    async def recover_running(
        self,
        *,
        host_task_id: str,
        state: object,
        max_parallel: int,
    ) -> tuple[()]:
        self.calls.append(("recover", host_task_id, state, max_parallel, None))
        return ()

    async def dispatch_ready(
        self,
        *,
        host_task_id: str,
        state: object,
        max_parallel: int,
        max_count: int,
    ) -> tuple[()]:
        self.calls.append(("dispatch", host_task_id, state, max_parallel, max_count))
        return ()


class _ImmediateSubmitter:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, coroutine):
        self.calls += 1
        future: Future[Any] = Future()
        try:
            future.set_result(asyncio.run(coroutine))
        except Exception as exc:  # pragma: no cover - future carries controller failure
            future.set_exception(exc)
        return future


class _HoldingSubmitter:
    def __init__(self) -> None:
        self.futures: list[Future[Any]] = []

    def __call__(self, coroutine):
        # The singleflight test exercises admission only. Close the unstarted
        # coroutine so the test never leaks an un-awaited object.
        coroutine.close()
        future: Future[Any] = Future()
        self.futures.append(future)
        return future


def test_controller_recovers_before_dispatch_through_packaged_submitter() -> None:
    plan = _plan()
    preparation = _Preparation()
    host = _Host()
    submitter = _ImmediateSubmitter()
    resolved: list[str] = []
    controller = PackagedProductFactoryExecutionController(
        preparation=cast(Any, preparation),
        host=cast(Any, host),
        resolve_plan=lambda project_id: resolved.append(project_id) or plan,
        submit=submitter,
        max_parallel=3,
        max_count=7,
    )

    result = controller.start(_PROJECT_ID)

    assert result.status == "completed"
    assert result.focus_id == "product-project-operator-heading"
    assert _PROJECT_ID in result.message
    assert resolved == [_PROJECT_ID]
    assert preparation.plans == [plan]
    assert submitter.calls == 1
    assert host.calls == [
        ("recover", "host-task", preparation.prepared.state, 3, None),
        ("dispatch", "host-task", preparation.prepared.state, 3, 7),
    ]


def test_controller_runs_async_post_dispatch_after_pf4_dispatch() -> None:
    plan = _plan()
    preparation = _Preparation()
    host = _Host()
    events: list[tuple[str, object]] = []

    class _OrderedHost(_Host):
        async def recover_running(self, **kwargs):
            events.append(("recover", kwargs["state"]))
            return await super().recover_running(**kwargs)

        async def dispatch_ready(self, **kwargs):
            events.append(("dispatch", kwargs["state"]))
            return await super().dispatch_ready(**kwargs)

    ordered_host = _OrderedHost()

    async def post_dispatch(prepared: PreparedProductFactory) -> None:
        events.append(("post_dispatch", prepared))

    controller = PackagedProductFactoryExecutionController(
        preparation=cast(Any, preparation),
        host=cast(Any, ordered_host),
        resolve_plan=lambda _project_id: plan,
        submit=_ImmediateSubmitter(),
        post_dispatch=post_dispatch,
    )

    result = controller.start(_PROJECT_ID)

    assert result.status == "completed"
    assert events == [
        ("recover", preparation.prepared.state),
        ("dispatch", preparation.prepared.state),
        ("post_dispatch", preparation.prepared),
    ]


def test_controller_rejects_noncallable_post_dispatch_hook() -> None:
    with pytest.raises(TypeError, match="post-dispatch"):
        PackagedProductFactoryExecutionController(
            preparation=cast(Any, _Preparation()),
            host=cast(Any, _Host()),
            resolve_plan=lambda _project_id: _plan(),
            submit=_ImmediateSubmitter(),
            post_dispatch=cast(Any, object()),
        )


def test_controller_propagates_post_dispatch_failure_into_background_future() -> None:
    preparation = _Preparation()
    submitter = _ImmediateSubmitter()

    async def post_dispatch(_prepared: PreparedProductFactory) -> None:
        raise RuntimeError("private continuation diagnostic")

    controller = PackagedProductFactoryExecutionController(
        preparation=cast(Any, preparation),
        host=cast(Any, _Host()),
        resolve_plan=lambda _project_id: _plan(),
        submit=submitter,
        post_dispatch=post_dispatch,
    )

    result = controller.start(_PROJECT_ID)

    assert result.status == "completed"
    assert submitter.calls == 1


def test_controller_rejects_concurrent_start_for_same_project() -> None:
    preparation = _Preparation()
    host = _Host()
    submitter = _HoldingSubmitter()
    controller = PackagedProductFactoryExecutionController(
        preparation=cast(Any, preparation),
        host=cast(Any, host),
        resolve_plan=lambda _project_id: _plan(),
        submit=submitter,
    )

    first = controller.start(_PROJECT_ID)
    duplicate = controller.start(_PROJECT_ID)

    assert first.status == "completed"
    assert duplicate.status == "rejected"
    assert "вже виконується" in duplicate.message
    assert len(preparation.plans) == 1
    assert len(submitter.futures) == 1

    submitter.futures[0].set_result(None)
    after_completion = controller.start(_PROJECT_ID)

    assert after_completion.status == "completed"
    assert len(preparation.plans) == 2
    assert len(submitter.futures) == 2
    submitter.futures[1].set_result(None)


def test_controller_rejects_plan_for_another_product_before_preparation() -> None:
    preparation = _Preparation()
    submitter = _ImmediateSubmitter()
    controller = PackagedProductFactoryExecutionController(
        preparation=cast(Any, preparation),
        host=cast(Any, _Host()),
        resolve_plan=lambda _project_id: _plan(_OTHER_PROJECT_ID),
        submit=submitter,
    )

    result = controller.start(_PROJECT_ID)

    assert result.status == "rejected"
    assert "іншому ProductProject" in result.message
    assert preparation.plans == []
    assert submitter.calls == 0


def test_controller_redacts_execution_plan_resolver_failure() -> None:
    preparation = _Preparation()
    submitter = _ImmediateSubmitter()

    def explode(_project_id: str) -> PackagedProductFactoryExecutionPlan:
        raise RuntimeError("secret repository diagnostics must not reach UI")

    controller = PackagedProductFactoryExecutionController(
        preparation=cast(Any, preparation),
        host=cast(Any, _Host()),
        resolve_plan=explode,
        submit=submitter,
    )

    result = controller.start(_PROJECT_ID)

    assert result.status == "failed"
    assert "secret" not in result.message
    assert "diagnostics" not in result.message
    assert preparation.plans == []
    assert submitter.calls == 0


def test_controller_requires_canonical_product_project_identity() -> None:
    controller = PackagedProductFactoryExecutionController(
        preparation=cast(Any, _Preparation()),
        host=cast(Any, _Host()),
        resolve_plan=lambda _project_id: _plan(),
        submit=_ImmediateSubmitter(),
    )

    with pytest.raises(PackagedProductFactoryExecutionError, match="canonical"):
        controller.start("product-not-canonical")


@pytest.mark.parametrize(
    "command",
    [
        "Run current Product Factory",
        "Start current Product Factory",
        "Запусти поточний Product Factory",
        "Запустити поточний Product Factory",
        "Виконай поточний Product Factory",
        "Виконати поточний Product Factory",
        "  run   current   product factory!  ",
    ],
)
def test_packaged_execution_command_aliases_are_exact(command: str) -> None:
    assert packaged_run_current_product_factory_command(command) is True


@pytest.mark.parametrize(
    "command",
    [
        "run product factory",
        "run current product factory now",
        "current product factory status",
        "plan current product factory",
    ],
)
def test_packaged_execution_command_does_not_capture_nearby_intents(command: str) -> None:
    assert packaged_run_current_product_factory_command(command) is False


def _router(
    path: Path,
    *,
    execution_handler=None,
) -> tuple[PackagedProductCommandRouter, list[str]]:
    store = SQLiteStore(path)
    store.initialize()
    repository = ProductProjectRepository(store)
    calls: list[str] = []

    def ordinary_handler(_payload) -> UIResult:
        return UIResult(
            request_id="desktop-handler",
            status="completed",
            message="ordinary",
            focus_id="tasks-heading",
        )

    def handler(project_id: str) -> UIResult:
        calls.append(project_id)
        return UIResult(
            request_id="desktop-handler",
            status="completed",
            message=f"execution:{project_id}",
            focus_id="product-project-operator-heading",
        )

    router = PackagedProductCommandRouter(
        products=ProductProjectCommandService(repository),
        ordinary_handler=ordinary_handler,
        selection_store=PackagedProductSelectionStore(store),
        product_factory_execution_handler=(
            handler if execution_handler is None else execution_handler
        ),
    )
    return router, calls


def test_router_dispatches_exact_execution_command_for_selected_product(tmp_path: Path) -> None:
    router, calls = _router(tmp_path / "execution route.db")
    goal = "Create product application for accessible invoice review"
    project_id = product_project_identity(goal)
    router.create({"command": goal})

    result = router.create({"command": "Run current Product Factory"})

    assert result.status == "completed"
    assert result.message == f"execution:{project_id}"
    assert calls == [project_id]


def test_router_execution_without_selected_product_fails_closed(tmp_path: Path) -> None:
    router, calls = _router(tmp_path / "execution no selection.db")

    with pytest.raises(PackagedProductJourneyError, match="не вибрано"):
        router.create({"command": "Run current Product Factory"})

    assert calls == []


def test_router_execution_without_composed_handler_fails_closed(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "execution unavailable.db")
    store.initialize()
    repository = ProductProjectRepository(store)
    router = PackagedProductCommandRouter(
        products=ProductProjectCommandService(repository),
        ordinary_handler=lambda _payload: UIResult(
            request_id="desktop-handler",
            status="completed",
            message="ordinary",
            focus_id="tasks-heading",
        ),
        selection_store=PackagedProductSelectionStore(store),
    )
    goal = "Create product application for accessible invoice review"
    router.create({"command": goal})

    with pytest.raises(PackagedProductJourneyError, match="недоступне"):
        router.create({"command": "Run current Product Factory"})
