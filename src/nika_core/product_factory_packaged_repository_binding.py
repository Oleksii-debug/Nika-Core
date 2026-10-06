from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from nika_core.product_factory_local_repository_binding import (
    ProductFactoryLocalRepositoryBindingError,
    ProductFactoryLocalRepositoryBindings,
)
from nika_core.product_factory_orchestration import RepositoryRef
from nika_core.product_factory_packaged_preparation import (
    PackagedProductFactoryExecutionPlan,
)
from nika_core.product_project import ProductProject, ProductProjectRepository
from nika_core.ui.bridge_models import UIResult

_LOGGER = logging.getLogger(__name__)
_SCHEMA_VERSION = 1
_MAX_ROOT_CHARS = 32_767
PlanResolver = Callable[[str], PackagedProductFactoryExecutionPlan]


class PackagedProductFactoryRepositoryBindingError(ValueError):
    """The packaged repository-binding request is not safe to admit."""


class _PlanUnavailable(PackagedProductFactoryRepositoryBindingError):
    pass


class _PlanStale(PackagedProductFactoryRepositoryBindingError):
    pass


class PackagedProductFactoryRepositoryBindingController:
    """Bridge admitted repository identity to durable local-root authority."""

    def __init__(
        self,
        *,
        bindings: ProductFactoryLocalRepositoryBindings,
        projects: ProductProjectRepository,
        resolve_plan: PlanResolver,
    ) -> None:
        if not isinstance(bindings, ProductFactoryLocalRepositoryBindings):
            raise TypeError("bindings must be ProductFactoryLocalRepositoryBindings")
        if not isinstance(projects, ProductProjectRepository):
            raise TypeError("projects must be ProductProjectRepository")
        if not callable(resolve_plan):
            raise TypeError("resolve_plan must be callable")
        self._bindings = bindings
        self._projects = projects
        self._resolve_plan = resolve_plan

    def snapshot(self, project_id: str | None) -> dict[str, object]:
        if project_id is None:
            return _snapshot(
                status="project_required",
                project_id=None,
                repositories=[],
                message=(
                    "Виберіть ProductProject перед налаштуванням локальних "
                    "репозиторіїв Product Factory."
                ),
            )
        try:
            plan, _project = self._require_current_plan(project_id)
        except _PlanUnavailable:
            return _snapshot(
                status="plan_required",
                project_id=project_id,
                repositories=[],
                message=(
                    "Завантажте JSON-план виконання для поточного ProductProject, "
                    "щоб вибрати репозиторій."
                ),
            )
        except _PlanStale:
            return _snapshot(
                status="stale_plan",
                project_id=project_id,
                repositories=[],
                message=(
                    "Завантажений JSON-план не відповідає поточній версії "
                    "ProductProject. Завантажте актуальний план."
                ),
            )

        repositories = [
            self._repository_snapshot(plan.project_id, repository)
            for repository in plan.graph.repositories
        ]
        invalid_count = sum(
            item["binding_status"] == "invalid" for item in repositories
        )
        bound_count = sum(
            item["binding_status"] == "bound" for item in repositories
        )
        message = (
            f"Локальні репозиторії Product Factory: прив’язано {bound_count} "
            f"з {len(repositories)}."
        )
        if invalid_count:
            message += (
                f" Пошкоджених або застарілих прив’язок: {invalid_count}; "
                "вкажіть новий корінь і збережіть із поточною версією."
            )
        return _snapshot(
            status="ready",
            project_id=plan.project_id,
            repositories=repositories,
            message=message,
        )

    def bind(
        self,
        project_id: str | None,
        payload: Mapping[str, Any],
    ) -> UIResult:
        if project_id is None:
            return _result(
                "rejected",
                "Поточний ProductProject не вибрано.",
                "product-project-heading",
            )
        try:
            repository_id, root, expected_version = _binding_payload(payload)
        except PackagedProductFactoryRepositoryBindingError:
            return _result(
                "rejected",
                (
                    "Вкажіть репозиторій з поточного плану та повний шлях до "
                    "локального Git-репозиторію."
                ),
                "product-factory-repository-root",
            )

        try:
            plan, _project = self._require_current_plan(project_id)
        except _PlanUnavailable:
            return _result(
                "rejected",
                "Спочатку завантажте JSON-план для поточного ProductProject.",
                "product-factory-execution-plan-path",
            )
        except _PlanStale:
            return _result(
                "rejected",
                (
                    "JSON-план застарів. Завантажте актуальний план перед "
                    "прив’язкою репозиторію."
                ),
                "product-factory-execution-plan-path",
            )

        repository = next(
            (
                item
                for item in plan.graph.repositories
                if item.repository_id == repository_id
            ),
            None,
        )
        if repository is None:
            return _result(
                "rejected",
                "Вибраний репозиторій відсутній у поточному JSON-плані.",
                "product-factory-repository-id",
            )

        try:
            current_version = self._bindings.current_binding_version(
                plan.project_id,
                repository.repository_id,
            )
        except ProductFactoryLocalRepositoryBindingError as exc:
            _log_failure("binding version read", exc)
            return _result(
                "failed",
                (
                    "Не вдалося безпечно прочитати поточну версію "
                    "прив’язки репозиторію."
                ),
                "product-factory-repository-root",
            )
        if current_version != expected_version:
            return _result(
                "rejected",
                (
                    "Прив’язка репозиторію змінилася. Перечитайте стан "
                    "і повторіть дію."
                ),
                "product-factory-repository-root",
            )

        try:
            bound = self._bindings.bind(
                project_id=plan.project_id,
                repository=repository,
                root=root,
                expected_binding_version=expected_version,
                expected_project_spec_version=plan.expected_spec_version,
                expected_project_row_version=plan.expected_row_version,
            )
        except (ProductFactoryLocalRepositoryBindingError, OSError, ValueError) as exc:
            _log_failure("binding write", exc)
            return _result(
                "rejected",
                (
                    "Локальний Git-репозиторій не пройшов перевірку або стан "
                    "змінився. Перечитайте стан і перевірте шлях."
                ),
                "product-factory-repository-root",
            )

        return _result(
            "completed",
            (
                f"Локальний репозиторій {bound.repository_id} прив’язано; "
                f"версія прив’язки {bound.binding_version}."
            ),
            "product-factory-repository-root",
        )

    def _require_current_plan(
        self,
        project_id: str,
    ) -> tuple[PackagedProductFactoryExecutionPlan, ProductProject]:
        if type(project_id) is not str or not project_id:
            raise _PlanUnavailable("project identity is unavailable")
        try:
            plan = self._resolve_plan(project_id)
        except (KeyError, TypeError, ValueError) as exc:
            _log_failure("plan resolve", exc)
            raise _PlanUnavailable("execution plan is unavailable") from exc
        if type(plan) is not PackagedProductFactoryExecutionPlan:
            raise _PlanUnavailable("execution plan carrier is invalid")
        try:
            project = self._projects.get(project_id)
        except KeyError as exc:
            raise _PlanStale("ProductProject no longer exists") from exc
        if (
            project.status != "active"
            or project.spec_version != plan.expected_spec_version
            or project.row_version != plan.expected_row_version
            or any(
                repository.locator not in project.spec.repository_refs
                for repository in plan.graph.repositories
            )
        ):
            raise _PlanStale("execution plan is stale")
        return plan, project

    def _repository_snapshot(
        self,
        project_id: str,
        repository: RepositoryRef,
    ) -> dict[str, object]:
        try:
            version = self._bindings.current_binding_version(
                project_id,
                repository.repository_id,
            )
        except ProductFactoryLocalRepositoryBindingError:
            version = None
        try:
            binding = self._bindings.require(project_id, repository.repository_id)
        except KeyError:
            binding_status = "unbound"
            root: str | None = None
        except ProductFactoryLocalRepositoryBindingError:
            binding_status = "invalid"
            root = None
        else:
            if (
                binding.provider != repository.provider
                or binding.locator != repository.locator
            ):
                binding_status = "invalid"
                root = None
            else:
                binding_status = "bound"
                root = str(binding.root)
                version = binding.binding_version
        return {
            "repository_id": repository.repository_id,
            "provider": repository.provider,
            "locator": repository.locator,
            "default_branch": repository.default_branch,
            "binding_status": binding_status,
            "binding_version": version,
            "root": root,
        }


def _binding_payload(
    payload: Mapping[str, Any],
) -> tuple[str, Path, int | None]:
    if type(payload) is not dict or set(payload) != {
        "repository_id",
        "root",
        "expected_binding_version",
    }:
        raise PackagedProductFactoryRepositoryBindingError(
            "repository binding payload fields are invalid"
        )
    repository_id = _canonical_text(payload["repository_id"], "repository_id")
    root_text = _canonical_root_text(payload["root"])
    version = payload["expected_binding_version"]
    if version is not None and (type(version) is not int or version < 1):
        raise PackagedProductFactoryRepositoryBindingError(
            "expected_binding_version must be null or a positive integer"
        )
    root = Path(root_text)
    if not root.is_absolute():
        raise PackagedProductFactoryRepositoryBindingError(
            "repository root must be absolute"
        )
    return repository_id, root, version


def _canonical_text(value: object, label: str) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise PackagedProductFactoryRepositoryBindingError(
            f"{label} must be canonical text"
        )
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise PackagedProductFactoryRepositoryBindingError(
            f"{label} must be valid UTF-8"
        ) from exc
    return value


def _canonical_root_text(value: object) -> str:
    text = _canonical_text(value, "root")
    if len(text) > _MAX_ROOT_CHARS:
        raise PackagedProductFactoryRepositoryBindingError(
            "repository root exceeds the path limit"
        )
    return text


def _snapshot(
    *,
    status: str,
    project_id: str | None,
    repositories: list[dict[str, object]],
    message: str,
) -> dict[str, object]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "status": status,
        "project_id": project_id,
        "repositories": repositories,
        "message": message,
    }


def _result(status: str, message: str, focus_id: str) -> UIResult:
    return UIResult(
        request_id="desktop-handler",
        status=status,
        message=message,
        focus_id=focus_id,
    )


def _log_failure(stage: str, exc: Exception) -> None:
    _LOGGER.warning(
        "Packaged Product Factory repository binding %s failed: exception_type=%s",
        stage,
        type(exc).__name__,
    )
