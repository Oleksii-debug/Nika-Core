from __future__ import annotations

import logging
import pathlib
from collections.abc import Callable, Mapping
from typing import Any

from nika_core.product_factory_local_repository_binding import (
    ProductFactoryLocalRepositoryBindingError,
    ProductFactoryLocalRepositoryBindings,
)
from nika_core.product_factory_orchestration import RepositoryRef
from nika_core.product_factory_packaged_preparation import (
    PackagedProductFactoryExecutionPlan,
)
from nika_core.ui.bridge_models import UIResult

_LOGGER = logging.getLogger(__name__)
_MAX_PATH_CHARS = 32_767

ProductFactoryExecutionPlanResolver = Callable[
    [str],
    PackagedProductFactoryExecutionPlan,
]


class PackagedLocalRepositoryOperator:
    """Accessible packaged authority for explicit ProductProject local-root binding.

    Repository identity is selected exclusively from the already-admitted execution plan.
    The UI supplies only the repository id, an explicit local root, and the optimistic
    binding version. Filesystem identity validation and durable mutation remain owned by
    ProductFactoryLocalRepositoryBindings.
    """

    def __init__(
        self,
        *,
        bindings: ProductFactoryLocalRepositoryBindings,
        resolve_plan: ProductFactoryExecutionPlanResolver,
    ) -> None:
        if not isinstance(bindings, ProductFactoryLocalRepositoryBindings):
            raise TypeError("bindings must be ProductFactoryLocalRepositoryBindings")
        if not callable(resolve_plan):
            raise TypeError("resolve_plan must be callable")
        self._bindings = bindings
        self._resolve_plan = resolve_plan

    def bind(self, payload: Mapping[str, Any]) -> UIResult:
        try:
            project_id, repository_id, expected_version = _mutation_identity(
                payload,
                require_root=True,
            )
            plan = self._plan(project_id)
            repository = self._repository_from_plan(plan, repository_id)
            root = _root_path(payload["root_path"])
            binding = self._bindings.bind(
                project_id=project_id,
                repository=repository,
                root=root,
                expected_binding_version=expected_version,
                expected_project_spec_version=plan.expected_spec_version,
                expected_project_row_version=plan.expected_row_version,
            )
        except (KeyError, TypeError, ValueError, OSError) as exc:
            _log_failure("bind", exc)
            return _result(
                "rejected",
                (
                    "Не вдалося прив’язати локальний репозиторій. "
                    "Перевірте вибраний репозиторій, повний шлях і актуальну версію прив’язки."
                ),
                "product-factory-local-repository-root",
            )
        return _result(
            "completed",
            (
                f"Локальний репозиторій {binding.repository_id} прив’язано "
                f"(версія {binding.binding_version})."
            ),
            "product-factory-local-repository-select",
        )

    def unbind(self, payload: Mapping[str, Any]) -> UIResult:
        try:
            project_id, repository_id, expected_version = _mutation_identity(
                payload,
                require_root=False,
            )
            if expected_version is None:
                raise ValueError("unbind requires an exact binding version")
            plan = self._plan(project_id)
            repository = self._repository_from_plan(plan, repository_id)
            self._bindings.unbind(
                project_id=project_id,
                repository_id=repository_id,
                expected_binding_version=expected_version,
                expected_repository=repository,
                expected_project_spec_version=plan.expected_spec_version,
                expected_project_row_version=plan.expected_row_version,
            )
        except (KeyError, TypeError, ValueError, OSError) as exc:
            _log_failure("unbind", exc)
            return _result(
                "rejected",
                (
                    "Не вдалося скасувати локальну прив’язку. "
                    "Перечитайте стан і повторіть лише для актуальної версії."
                ),
                "product-factory-local-repository-select",
            )
        return _result(
            "completed",
            f"Локальну прив’язку {repository_id} скасовано.",
            "product-factory-local-repository-select",
        )

    def snapshot(self, project_id: str | None) -> dict[str, object]:
        if project_id is None:
            return {
                "status": "missing_plan",
                "project_id": None,
                "repositories": [],
                "message": (
                    "Спочатку завантажте JSON-план виконання Product Factory, "
                    "щоб вибрати локальний репозиторій."
                ),
            }
        try:
            plan = self._plan(project_id)
            repositories: list[dict[str, object]] = []
            invalid_count = 0
            for repository in plan.graph.repositories:
                version = self._bindings.current_binding_version(
                    plan.project_id,
                    repository.repository_id,
                )
                if version is None:
                    repositories.append(
                        _repository_state(
                            repository,
                            binding_status="unbound",
                            bound=False,
                            binding_version=None,
                        )
                    )
                    continue
                try:
                    binding = self._bindings.require(
                        plan.project_id,
                        repository.repository_id,
                    )
                except ProductFactoryLocalRepositoryBindingError as exc:
                    _log_failure("snapshot binding validation", exc)
                    invalid_count += 1
                    repositories.append(
                        _repository_state(
                            repository,
                            binding_status="invalid",
                            bound=False,
                            binding_version=version,
                        )
                    )
                    continue
                if (
                    binding.provider != repository.provider
                    or binding.locator != repository.locator
                ):
                    invalid_count += 1
                    repositories.append(
                        _repository_state(
                            repository,
                            binding_status="invalid",
                            bound=False,
                            binding_version=version,
                        )
                    )
                    continue
                repositories.append(
                    _repository_state(
                        repository,
                        binding_status="bound",
                        bound=True,
                        binding_version=binding.binding_version,
                    )
                )
            message = (
                "Виберіть репозиторій з поточного плану та явно вкажіть "
                "його локальний Git-корінь."
            )
            if invalid_count:
                message += (
                    " Недійсні прив’язки можна безпечно замінити, вказавши "
                    "новий шлях; поточна CAS-версія збережена без розкриття старого шляху."
                )
            self._bindings.validate_plan(plan)
            return {
                "status": "ready",
                "project_id": plan.project_id,
                "repositories": repositories,
                "message": message,
            }
        except (KeyError, TypeError, ValueError, OSError) as exc:
            _log_failure("snapshot plan resolution", exc)
            return {
                "status": "invalid",
                "project_id": None,
                "repositories": [],
                "message": "Стан локальних прив’язок Product Factory недоступний.",
            }

    def _repository_for(
        self,
        project_id: str,
        repository_id: str,
    ) -> RepositoryRef:
        return self._repository_from_plan(self._plan(project_id), repository_id)

    @staticmethod
    def _repository_from_plan(
        plan: PackagedProductFactoryExecutionPlan,
        repository_id: str,
    ) -> RepositoryRef:
        for repository in plan.graph.repositories:
            if repository.repository_id == repository_id:
                return repository
        raise KeyError(repository_id)

    def _plan(self, project_id: str) -> PackagedProductFactoryExecutionPlan:
        project_id = _text(project_id, "project_id")
        plan = self._resolve_plan(project_id)
        if type(plan) is not PackagedProductFactoryExecutionPlan:
            raise TypeError("execution-plan resolver returned an invalid carrier")
        if plan.project_id != project_id:
            raise ValueError("execution plan belongs to another ProductProject")
        self._bindings.validate_plan(plan)
        return plan


def _mutation_identity(
    payload: Mapping[str, Any],
    *,
    require_root: bool,
) -> tuple[str, str, int | None]:
    if not isinstance(payload, Mapping):
        raise TypeError("payload must be a mapping")
    expected = {
        "project_id",
        "repository_id",
        "expected_binding_version",
    }
    if require_root:
        expected.add("root_path")
    if set(payload) != expected:
        raise ValueError("local repository mutation payload has an invalid schema")
    project_id = _text(payload["project_id"], "project_id")
    repository_id = _text(payload["repository_id"], "repository_id")
    version = payload["expected_binding_version"]
    if version is not None and (
        type(version) is not int or version < 1
    ):
        raise ValueError("expected_binding_version must be null or a positive integer")
    return project_id, repository_id, version


def _root_path(value: object) -> pathlib.Path:
    text = _text(value, "root_path")
    if len(text) > _MAX_PATH_CHARS:
        raise ValueError("root_path exceeds the path length limit")
    root = pathlib.Path(text)
    if not root.is_absolute():
        raise ValueError("root_path must be absolute")
    return root


def _text(value: object, label: str) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"{label} must be canonical non-empty text")
    if len(value.encode("utf-8")) > 2048:
        raise ValueError(f"{label} exceeds the UTF-8 byte limit")
    return value


def _repository_state(
    repository: RepositoryRef,
    *,
    binding_status: str,
    bound: bool,
    binding_version: int | None,
) -> dict[str, object]:
    return {
        "repository_id": repository.repository_id,
        "provider": repository.provider,
        "locator": repository.locator,
        "binding_status": binding_status,
        "bound": bound,
        "binding_version": binding_version,
    }


def _result(status: str, message: str, focus_id: str) -> UIResult:
    return UIResult(
        request_id="product-factory-local-repository",
        status=status,
        message=message,
        focus_id=focus_id,
    )


def _log_failure(stage: str, exc: BaseException) -> None:
    _LOGGER.warning(
        "packaged local repository operator %s failed: %s",
        stage,
        type(exc).__name__,
    )
