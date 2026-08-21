from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Callable, Mapping
from typing import Any

from nika_core.product_command.command_center import ProductCommandCenter
from nika_core.product_command.contracts import CommandRouteKind, ProductProjectDetail
from nika_core.product_command.product_project_adapter import (
    ProductProjectCommandService,
    ProductProjectPresentationConsistencyError,
)
from nika_core.product_command.routing import route_command
from nika_core.product_project import ProductProjectSpec
from nika_core.ui.bridge_models import UIResult

OrdinaryCommandHandler = Callable[[Mapping[str, Any]], UIResult]
DesktopStateProvider = Callable[[], Mapping[str, Any]]


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
        self._active_project_id: str | None = None

    @property
    def active_project_id(self) -> str | None:
        """Return process-local presentation selection, never a durable authority record."""
        return self._active_project_id

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
        self._active_project_id = project_id
        return UIResult(
            request_id="desktop-handler",
            status="completed",
            message=(
                f"ProductProject створено або відкрито: {project_id}; "
                f"spec version {detail.summary.version}."
            ),
            focus_id="tasks-heading",
        )


class PackagedProductStateProvider:
    """Compose Desktop state with a bounded PF5 ProductCommandCenter projection.

    The active project pointer is intentionally process-local. Durable ProductProject identity,
    lifecycle and decisions remain owned by PF1/PF5 repositories. Replaying the same product
    command after restart re-selects the same durable project rather than introducing a second
    persisted selection mechanism.

    Only bounded presentation fields are returned. Evidence references, credential references,
    authorization material, provider sessions and protected-store handles are not serialized by
    this adapter.
    """

    def __init__(
        self,
        *,
        base_state: DesktopStateProvider,
        router: PackagedProductCommandRouter,
        command_center: ProductCommandCenter,
    ) -> None:
        self._base_state = base_state
        self._router = router
        self._command_center = command_center

    def __call__(self) -> dict[str, Any]:
        state = dict(self._base_state())
        project_id = self._router.active_project_id
        state["product_project"] = None
        if project_id is None:
            return state
        try:
            detail = self._command_center.inspect_project(project_id)
        except ProductProjectPresentationConsistencyError as exc:
            raise PackagedProductJourneyError(
                "ProductProject changed while packaged state was composed; refresh required."
            ) from exc
        state["product_project"] = _safe_product_project_state(detail)
        return state


def _safe_product_project_state(detail: ProductProjectDetail) -> dict[str, Any]:
    status_counts = Counter(item.kind.value for item in detail.statuses)
    decision_counts = Counter(item.state for item in detail.decisions)
    return {
        "project_id": detail.summary.project_id,
        "spec_version": detail.summary.version,
        "title": detail.summary.title,
        "goal": detail.summary.goal,
        "state": detail.summary.state,
        "blocker_count": detail.summary.blocker_count,
        "status_count": len(detail.statuses),
        "status_counts": dict(sorted(status_counts.items())),
        "decision_count": len(detail.decisions),
        "decision_state_counts": dict(sorted(decision_counts.items())),
    }


def _project_title(goal: str) -> str:
    normalized = " ".join(goal.split())
    return normalized if len(normalized) <= 160 else normalized[:157] + "..."
