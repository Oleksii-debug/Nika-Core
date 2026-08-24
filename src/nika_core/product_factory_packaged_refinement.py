from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from nika_core.product_command.product_project_adapter import (
    ProductProjectCommandService,
    ProductProjectPresentationConsistencyError,
)
from nika_core.product_factory_packaged_journey import (
    PackagedProductCommandRouter,
    PackagedProductJourneyError,
)
from nika_core.product_project import StaleProjectVersionError
from nika_core.ui.bridge_models import UIResult

_REFINE_GOAL_COMMANDS = frozenset(
    {
        "set current productproject goal",
        "update current productproject goal",
        "встанови ціль поточного productproject",
        "онови ціль поточного productproject",
    }
)
_MAX_PRODUCT_GOAL_CHARS = 4000


def packaged_product_goal_refinement(command: str) -> str | None:
    """Parse an explicit keyboard command for one versioned ProductProject goal update."""
    normalized = " ".join(command.split())
    head, separator, remainder = normalized.partition(":")
    if head.casefold() not in _REFINE_GOAL_COMMANDS:
        return None
    if not separator:
        raise PackagedProductJourneyError(
            "Після команди оновлення цілі поставте двокрапку і введіть нову ціль."
        )
    goal = " ".join(remainder.split())
    if not goal:
        raise PackagedProductJourneyError("Нова ціль ProductProject не може бути порожньою.")
    if len(goal) > _MAX_PRODUCT_GOAL_CHARS:
        raise PackagedProductJourneyError(
            f"Нова ціль ProductProject перевищує {_MAX_PRODUCT_GOAL_CHARS} символів."
        )
    return goal


class PackagedProductRefinementRouter:
    """Thin packaged adapter for versioned ProductProject refinement.

    The canonical ProductProjectCommandService remains the mutation authority. This adapter only
    recognizes an explicit keyboard grammar, binds it to the current durable presentation
    selection, normalizes concurrency errors, and delegates every other command to the already
    integrated packaged ProductProject router.
    """

    def __init__(
        self,
        *,
        products: ProductProjectCommandService,
        base_router: PackagedProductCommandRouter,
    ) -> None:
        self._products = products
        self._base_router = base_router

    def create(self, payload: Mapping[str, Any]) -> UIResult:
        command = str(payload.get("command", "")).strip()
        if not command:
            return self._base_router.create(payload)
        refined_goal = packaged_product_goal_refinement(command)
        if refined_goal is None:
            return self._base_router.create(payload)
        return self._refine_current_goal(refined_goal)

    def _refine_current_goal(self, goal: str) -> UIResult:
        project_id = self._base_router.active_project_id
        if project_id is None:
            raise PackagedProductJourneyError(
                "Поточний ProductProject не вибрано. Створіть продукт або відкрийте його за ID."
            )
        try:
            before = self._products.inspect_project(project_id)
        except KeyError as exc:
            self._base_router.clear_stale_selection()
            raise PackagedProductJourneyError(
                "Збережений ProductProject більше не існує. Застарілий вибір очищено."
            ) from exc
        except ProductProjectPresentationConsistencyError as exc:
            raise PackagedProductJourneyError(
                "ProductProject changed while its current goal was read; retry the update command."
            ) from exc

        if before.summary.goal == goal:
            return UIResult(
                request_id="desktop-handler",
                status="completed",
                message=(
                    f"Ціль ProductProject вже актуальна: {project_id}; "
                    f"spec version {before.summary.version}; state {before.summary.state}; "
                    f"goal: {before.summary.goal}."
                ),
                focus_id="tasks-heading",
            )

        try:
            after = self._products.update_project(
                project_id,
                expected_spec_version=before.summary.version,
                goal=goal,
            )
        except StaleProjectVersionError as exc:
            raise PackagedProductJourneyError(
                "ProductProject changed before the goal update committed; retry from fresh state."
            ) from exc
        except KeyError as exc:
            self._base_router.clear_stale_selection()
            raise PackagedProductJourneyError(
                "Збережений ProductProject більше не існує. Застарілий вибір очищено."
            ) from exc
        except ProductProjectPresentationConsistencyError as exc:
            raise PackagedProductJourneyError(
                "ProductProject changed while the updated goal was read; refresh and retry."
            ) from exc

        if (
            after.summary.project_id != project_id
            or after.summary.version != before.summary.version + 1
            or after.summary.goal != goal
            or after.summary.state != before.summary.state
        ):
            raise PackagedProductJourneyError(
                "ProductProject goal update returned inconsistent durable identity or state."
            )
        return UIResult(
            request_id="desktop-handler",
            status="completed",
            message=(
                f"ProductProject оновлено: {project_id}; "
                f"spec version {before.summary.version} -> {after.summary.version}; "
                f"state {after.summary.state}; goal: {after.summary.goal}."
            ),
            focus_id="tasks-heading",
        )
