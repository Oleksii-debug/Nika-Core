from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from typing import Any

from nika_core.product_command.contracts import CommandRouteKind
from nika_core.product_command.product_project_adapter import ProductProjectCommandService
from nika_core.product_command.routing import route_command
from nika_core.product_project import ProductProjectSpec
from nika_core.ui.bridge_models import UIResult

OrdinaryCommandHandler = Callable[[Mapping[str, Any]], UIResult]


class PackagedProductJourneyError(ValueError):
    """Raised when the packaged command cannot safely enter Product Factory routing."""


def product_project_identity(normalized_goal: str) -> str:
    goal = " ".join(normalized_goal.split())
    if not goal:
        raise PackagedProductJourneyError("product goal must not be empty")
    digest = hashlib.sha256(goal.encode("utf-8")).hexdigest()
    return f"product-{digest}"


class PackagedProductCommandRouter:
    """Route packaged command input to durable ProductProject or ordinary task handling.

    Product intent creates/reopens a durable PF1 ProductProject through the public PF5 adapter.
    This boundary deliberately does not dispatch workers, deploy providers, Toolsmith, or any
    high-impact external action. Those remain downstream explicit factory/security boundaries.
    """

    def __init__(
        self,
        *,
        products: ProductProjectCommandService,
        ordinary_handler: OrdinaryCommandHandler,
    ) -> None:
        self._products = products
        self._ordinary_handler = ordinary_handler

    def create(self, payload: Mapping[str, Any]) -> UIResult:
        command = str(payload.get("command", "")).strip()
        if not command:
            raise PackagedProductJourneyError(
                "Введіть команду перед створенням завдання."
            )

        decision = route_command(command)
        if decision.route is CommandRouteKind.AGENT_TASK:
            return self._ordinary_handler(payload)
        if decision.route is CommandRouteKind.AMBIGUOUS:
            raise PackagedProductJourneyError(
                "Команда одночасно схожа на ProductProject і Toolsmith. "
                "Уточніть, чи це довготривалий продукт, "
                "чи створення інструмента."
            )
        if decision.route is CommandRouteKind.TOOLSMITH:
            raise PackagedProductJourneyError(
                "Команда визначена як запит на нову "
                "можливість Toolsmith. "
                "Packaged ProductProject route не запускає capability-builder "
                "без окремого контексту."
            )
        if decision.route is not CommandRouteKind.PRODUCT_PROJECT:
            raise PackagedProductJourneyError("unsupported packaged command route")

        goal = decision.normalized_goal or command
        project_id = product_project_identity(goal)
        detail = self._products.create_project(
            project_id=project_id,
            name=_project_title(goal),
            spec=ProductProjectSpec(goal=goal, desired_outcome=goal),
            idempotency_key=f"packaged-product-route:{project_id}",
        )
        return UIResult(
            request_id="desktop-handler",
            status="completed",
            message=(
                f"ProductProject створено або відкрито: {project_id}; "
                f"spec version {detail.summary.version}."
            ),
            focus_id="tasks-heading",
        )


def _project_title(goal: str) -> str:
    normalized = " ".join(goal.split())
    return normalized if len(normalized) <= 160 else normalized[:157] + "..."
