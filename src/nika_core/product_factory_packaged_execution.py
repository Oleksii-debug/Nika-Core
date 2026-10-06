from __future__ import annotations

import logging
import re
from collections.abc import Callable, Coroutine
from concurrent.futures import Future
from threading import Lock
from typing import Any

from nika_core.product_factory_multi_repository import MultiRepositoryProductFactoryHost
from nika_core.product_factory_packaged_preparation import (
    PackagedProductFactoryExecutionPlan,
    PackagedProductFactoryPreparationService,
    PreparedProductFactory,
)
from nika_core.ui.bridge_models import UIResult

_LOGGER = logging.getLogger(__name__)
_PRODUCT_PROJECT_ID = re.compile(r"product-[0-9a-f]{64}")

ProductFactoryExecutionPlanResolver = Callable[
    [str],
    PackagedProductFactoryExecutionPlan,
]
PackagedCoroutineSubmitter = Callable[
    [Coroutine[Any, Any, Any]],
    Future[Any],
]
ProductFactoryExecutionContextFactory = Callable[
    [PackagedProductFactoryExecutionPlan],
    tuple[PackagedProductFactoryPreparationService, MultiRepositoryProductFactoryHost],
]


class PackagedProductFactoryExecutionError(RuntimeError):
    """Raised when packaged execution composition cannot prove its authority."""


class PackagedProductFactoryExecutionController:
    """Start one bounded Product Factory execution pass from trusted host authority.

    The command surface supplies only a selected ProductProject id. Repository graph,
    base SHAs, component goals and permissions come exclusively from the injected
    execution-plan resolver. The incumbent preparation service binds that plan to the
    exact durable ProductProject version and canonical host task before any worker
    effect may be admitted.

    Async work always runs through the packaged desktop submitter. This controller does
    not create a second event loop, worker, planner, checkpoint store or recovery
    authority.
    """

    def __init__(
        self,
        *,
        preparation: PackagedProductFactoryPreparationService | None = None,
        host: MultiRepositoryProductFactoryHost | None = None,
        execution_context_factory: ProductFactoryExecutionContextFactory | None = None,
        resolve_plan: ProductFactoryExecutionPlanResolver,
        submit: PackagedCoroutineSubmitter,
        max_parallel: int = 4,
        max_count: int = 32,
    ) -> None:
        has_static_context = preparation is not None or host is not None
        if execution_context_factory is None:
            if preparation is None or host is None:
                raise TypeError(
                    "Product Factory execution requires preparation and host authorities"
                )
            _require_execution_context(preparation, host)
        else:
            if has_static_context:
                raise ValueError(
                    "static Product Factory execution authority conflicts with context factory"
                )
            if not callable(execution_context_factory):
                raise TypeError("Product Factory execution-context factory must be callable")
        if not callable(resolve_plan):
            raise TypeError("Product Factory execution-plan resolver must be callable")
        if not callable(submit):
            raise TypeError("packaged coroutine submitter must be callable")
        if type(max_parallel) is not int or not 1 <= max_parallel <= 32:
            raise ValueError("Product Factory max_parallel must be 1..32")
        if type(max_count) is not int or not 1 <= max_count <= 256:
            raise ValueError("Product Factory max_count must be 1..256")

        self._preparation = preparation
        self._host = host
        self._execution_context_factory = execution_context_factory
        self._resolve_plan = resolve_plan
        self._submit = submit
        self._max_parallel = max_parallel
        self._max_count = max_count
        self._lock = Lock()
        self._starting: set[str] = set()
        self._active: dict[str, Future[Any]] = {}

    def start(self, project_id: str) -> UIResult:
        """Prepare and schedule one recovery/dispatch pass for the selected project."""

        project_id = _canonical_project_id(project_id)
        with self._lock:
            active = self._active.get(project_id)
            if project_id in self._starting or (
                active is not None and not active.done()
            ):
                return _result(
                    "rejected",
                    "Product Factory для поточного ProductProject уже виконується.",
                )
            self._starting.add(project_id)

        try:
            try:
                plan = self._resolve_plan(project_id)
            except Exception as exc:  # noqa: BLE001 - redact trusted-composition failure
                _log_failure("execution-plan resolution", exc)
                return _result(
                    "failed",
                    "Не вдалося отримати авторитетний план виконання Product Factory.",
                )
            if type(plan) is not PackagedProductFactoryExecutionPlan:
                return _result(
                    "rejected",
                    "Авторитетний план виконання Product Factory має некоректний формат.",
                )
            if plan.project_id != project_id:
                return _result(
                    "rejected",
                    "План виконання Product Factory належить іншому ProductProject.",
                )

            try:
                preparation, host = self._execution_context(plan)
            except Exception as exc:  # noqa: BLE001 - redact trusted-composition failure
                _log_failure("execution-context resolution", exc)
                return _result(
                    "failed",
                    "Не вдалося безпечно підготувати середовище виконання Product Factory.",
                )

            try:
                prepared = preparation.prepare(plan)
            except Exception as exc:  # noqa: BLE001 - packaged boundary must redact details
                _log_failure("durable preparation", exc)
                return _result(
                    "failed",
                    "Не вдалося безпечно підготувати поточний ProductProject до виконання.",
                )
            if type(prepared) is not PreparedProductFactory:
                return _result(
                    "failed",
                    "Product Factory не підтвердив канонічний підготовлений стан.",
                )

            coroutine = self._run_once(prepared, host)
            try:
                future = self._submit(coroutine)
            except Exception as exc:  # noqa: BLE001 - packaged boundary must redact details
                coroutine.close()
                _log_failure("background submission", exc)
                return _result(
                    "failed",
                    "Фоновий запуск Product Factory недоступний.",
                )
            if not isinstance(future, Future):
                coroutine.close()
                return _result(
                    "failed",
                    "Фоновий запуск Product Factory повернув некоректний стан.",
                )

            with self._lock:
                self._active[project_id] = future
            future.add_done_callback(
                lambda completed, identity=project_id: self._done(identity, completed)
            )
            return _result(
                "completed",
                (
                    f"Product Factory запущено для {project_id}. "
                    "Перевіряйте виконання командою «Show current Product Factory status»."
                ),
            )
        finally:
            with self._lock:
                self._starting.discard(project_id)

    def _execution_context(
        self,
        plan: PackagedProductFactoryExecutionPlan,
    ) -> tuple[PackagedProductFactoryPreparationService, MultiRepositoryProductFactoryHost]:
        if self._execution_context_factory is None:
            assert self._preparation is not None
            assert self._host is not None
            return self._preparation, self._host
        context = self._execution_context_factory(plan)
        if type(context) is not tuple or len(context) != 2:
            raise TypeError("Product Factory execution-context factory returned invalid context")
        preparation, host = context
        _require_execution_context(preparation, host)
        return preparation, host

    async def _run_once(
        self,
        prepared: PreparedProductFactory,
        host: MultiRepositoryProductFactoryHost,
    ) -> None:
        await host.recover_running(
            host_task_id=prepared.host_task_id,
            state=prepared.state,
            max_parallel=self._max_parallel,
        )
        await host.dispatch_ready(
            host_task_id=prepared.host_task_id,
            state=prepared.state,
            max_parallel=self._max_parallel,
            max_count=self._max_count,
        )

    def _done(self, project_id: str, future: Future[Any]) -> None:
        try:
            future.result()
        except Exception as exc:  # noqa: BLE001 - future diagnostics stay private
            _log_failure("background execution", exc)
        finally:
            with self._lock:
                if self._active.get(project_id) is future:
                    self._active.pop(project_id, None)


def _require_execution_context(
    preparation: object,
    host: object,
) -> None:
    if not callable(getattr(preparation, "prepare", None)):
        raise TypeError("Product Factory preparation authority is invalid")
    if not callable(getattr(host, "recover_running", None)) or not callable(
        getattr(host, "dispatch_ready", None)
    ):
        raise TypeError("Product Factory execution host is invalid")


def _canonical_project_id(value: object) -> str:
    if type(value) is not str or _PRODUCT_PROJECT_ID.fullmatch(value) is None:
        raise PackagedProductFactoryExecutionError(
            "Product Factory execution requires a canonical ProductProject id"
        )
    return value


def _result(status: str, message: str) -> UIResult:
    return UIResult(
        request_id="desktop-handler",
        status=status,
        message=message,
        focus_id="product-project-operator-heading",
    )


def _log_failure(stage: str, exc: Exception) -> None:
    _LOGGER.error(
        "Packaged Product Factory %s failed: exception_type=%s",
        stage,
        type(exc).__name__,
    )
