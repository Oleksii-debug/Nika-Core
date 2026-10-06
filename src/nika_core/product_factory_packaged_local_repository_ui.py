from __future__ import annotations

import hashlib
import json
import pathlib
import re
import unicodedata
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

_MAX_PATH_CHARS = 32_767
_REPOSITORY_TOKEN = re.compile(r"[0-9a-f]{64}")

ProductFactoryExecutionPlanResolver = Callable[
    [str],
    PackagedProductFactoryExecutionPlan,
]
ActiveProductProjectResolver = Callable[[], str | None]


class PackagedLocalRepositoryBindingCommands:
    """Keyboard/UI adapter over the durable local-repository binding authority.

    Browser payloads never provide provider/locator authority. The projected repository
    token is only an optimistic stale-plan fence; the current loaded execution plan and
    ProductProject remain authoritative at bind time.
    """

    def __init__(
        self,
        bindings: ProductFactoryLocalRepositoryBindings,
        *,
        resolve_plan: ProductFactoryExecutionPlanResolver,
        active_project_id: ActiveProductProjectResolver,
    ) -> None:
        if not isinstance(bindings, ProductFactoryLocalRepositoryBindings):
            raise TypeError("bindings must be ProductFactoryLocalRepositoryBindings")
        if not callable(resolve_plan):
            raise TypeError("resolve_plan must be callable")
        if not callable(active_project_id):
            raise TypeError("active_project_id must be callable")
        self._bindings = bindings
        self._resolve_plan = resolve_plan
        self._active_project_id = active_project_id

    def snapshot(self) -> dict[str, object]:
        project_id = self._active_project_id()
        if project_id is None:
            return {
                "status": "project_required",
                "project_id": None,
                "repositories": [],
                "message": (
                    "Спочатку створіть або відкрийте ProductProject, а потім "
                    "завантажте його JSON-план виконання."
                ),
            }

        try:
            plan = self._resolve_plan(project_id)
        except Exception:
            return {
                "status": "plan_required",
                "project_id": project_id,
                "repositories": [],
                "message": (
                    "Завантажте JSON-план виконання для поточного ProductProject."
                ),
            }
        if type(plan) is not PackagedProductFactoryExecutionPlan:
            return {
                "status": "invalid",
                "project_id": project_id,
                "repositories": [],
                "message": "Поточний JSON-план Product Factory має некоректний формат.",
            }

        repositories: list[dict[str, object]] = []
        for repository in plan.graph.repositories:
            token = _repository_token(repository)
            try:
                version = self._bindings.binding_version(
                    project_id,
                    repository.repository_id,
                )
            except Exception:
                version = None
                binding_status = "invalid"
            else:
                if version is None:
                    binding_status = "unbound"
                else:
                    try:
                        binding = self._bindings.require(
                            project_id,
                            repository.repository_id,
                        )
                    except Exception:
                        binding_status = "invalid"
                    else:
                        binding_status = (
                            "bound"
                            if (
                                binding.provider == repository.provider
                                and binding.locator == repository.locator
                            )
                            else "invalid"
                        )
            repositories.append(
                {
                    "repository_id": repository.repository_id,
                    "provider": repository.provider,
                    "repository_token": token,
                    "binding_status": binding_status,
                    "binding_version": version,
                }
            )

        return {
            "status": "ready",
            "project_id": project_id,
            "repositories": repositories,
            "message": (
                "Локальні репозиторії Product Factory готові до явної прив'язки."
            ),
        }

    def bind(self, payload: Mapping[str, Any]) -> UIResult:
        try:
            request = _bind_request(payload)
        except (TypeError, ValueError):
            return _result(
                "rejected",
                "Перевірте ProductProject, репозиторій і повний локальний шлях.",
            )

        (
            project_id,
            repository_id,
            repository_token,
            root,
            expected_binding_version,
        ) = request
        if self._active_project_id() != project_id:
            return _result(
                "rejected",
                "Поточний ProductProject змінився. Оновіть стан і повторіть прив'язку.",
            )

        try:
            plan = self._resolve_plan(project_id)
        except Exception:
            return _result(
                "rejected",
                "Завантажте актуальний JSON-план виконання для поточного ProductProject.",
            )
        if type(plan) is not PackagedProductFactoryExecutionPlan:
            return _result(
                "failed",
                "Не вдалося підтвердити поточний JSON-план Product Factory.",
            )

        repository = _repository_for_id(plan, repository_id)
        if repository is None or _repository_token(repository) != repository_token:
            return _result(
                "rejected",
                "Репозиторій у JSON-плані змінився. Оновіть стан і повторіть прив'язку.",
            )

        try:
            current_version = self._bindings.binding_version(
                project_id,
                repository_id,
            )
        except Exception:
            return _result(
                "failed",
                "Не вдалося безпечно прочитати версію локальної прив'язки.",
            )
        if current_version != expected_binding_version:
            return _result(
                "rejected",
                "Локальна прив'язка вже змінилася. Оновіть стан перед повторною спробою.",
            )

        try:
            binding = self._bindings.bind_for_plan(
                plan=plan,
                repository_id=repository_id,
                root=root,
                expected_binding_version=expected_binding_version,
            )
        except (ProductFactoryLocalRepositoryBindingError, KeyError, OSError, ValueError):
            return _result(
                "rejected",
                (
                    "Не вдалося безпечно прив'язати локальний Git-репозиторій. "
                    "Перевірте актуальність ProductProject і JSON-плану."
                ),
            )
        except Exception:
            return _result(
                "failed",
                "Не вдалося зберегти локальну прив'язку Product Factory.",
            )

        return _result(
            "completed",
            (
                f"Локальний репозиторій {repository.repository_id} прив'язано; "
                f"версія {binding.binding_version}."
            ),
        )


def _bind_request(
    payload: Mapping[str, Any],
) -> tuple[str, str, str, pathlib.Path, int | None]:
    if type(payload) is not dict or set(payload) != {
        "project_id",
        "repository_id",
        "repository_token",
        "root_path",
        "expected_binding_version",
    }:
        raise ValueError("local repository binding payload shape is invalid")

    project_id = _exact_text(payload["project_id"], "project_id")
    repository_id = _exact_text(payload["repository_id"], "repository_id")
    repository_token = _exact_text(payload["repository_token"], "repository_token")
    if _REPOSITORY_TOKEN.fullmatch(repository_token) is None:
        raise ValueError("repository_token must be canonical SHA-256 text")

    raw_path = _exact_text(payload["root_path"], "root_path")
    if len(raw_path) > _MAX_PATH_CHARS or any(
        unicodedata.category(character) in {"Cc", "Cf", "Zl", "Zp"}
        for character in raw_path
    ):
        raise ValueError("root_path contains unsupported text")
    root = pathlib.Path(raw_path)
    if not root.is_absolute():
        raise ValueError("root_path must be absolute")

    expected = payload["expected_binding_version"]
    if expected is not None and (
        type(expected) is not int
        or expected < 1
        or expected > (1 << 63) - 1
    ):
        raise ValueError("expected_binding_version is invalid")
    return project_id, repository_id, repository_token, root, expected


def _repository_for_id(
    plan: PackagedProductFactoryExecutionPlan,
    repository_id: str,
) -> RepositoryRef | None:
    matches = tuple(
        repository
        for repository in plan.graph.repositories
        if repository.repository_id == repository_id
    )
    return matches[0] if len(matches) == 1 else None


def _repository_token(repository: RepositoryRef) -> str:
    payload = json.dumps(
        {
            "locator": repository.locator,
            "provider": repository.provider,
            "repository_id": repository.repository_id,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _exact_text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise TypeError(f"{field} must be exact non-empty text")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{field} must be valid UTF-8 text") from exc
    return value


def _result(status: str, message: str) -> UIResult:
    return UIResult(
        request_id="desktop-handler",
        status=status,
        message=message,
        focus_id="product-factory-local-repository-path",
    )
