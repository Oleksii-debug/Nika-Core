from __future__ import annotations

import hashlib
import re
import unicodedata
from collections import Counter
from collections.abc import Callable, Mapping
from typing import Any
from uuid import UUID

from nika_core.data.sqlite import SQLiteStore
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
ActivityReportHandler = Callable[[], UIResult]
TrainingStatusHandler = Callable[[str], UIResult]
DesktopStateProvider = Callable[[], Mapping[str, Any]]
_PRODUCT_PROJECT_ID = re.compile(r"product-[0-9a-f]{64}", re.IGNORECASE)
_REOPEN_PREFIXES = (
    "open productproject",
    "reopen productproject",
    "відкрий productproject",
    "відкрити productproject",
    "перейди до productproject",
)
_CURRENT_PROJECT_COMMANDS = frozenset(
    {
        "current productproject",
        "show current productproject",
        "поточний productproject",
        "покажи поточний productproject",
    }
)
_DAILY_ACTIVITY_REPORT_COMMANDS = frozenset(
    {
        "daily activity report",
        "show daily activity report",
        "nika daily activity report",
        "щоденний звіт активності",
        "покажи щоденний звіт активності",
        "звіт діяльності nika",
        "покажи звіт діяльності nika",
    }
)
_TRAINING_STATUS_PREFIXES = (
    "show training status",
    "training status",
    "покажи статус навчання",
    "статус навчання",
)


class PackagedProductJourneyError(ValueError):
    """Raised when the packaged command cannot safely enter Product Factory routing."""


def product_project_identity(normalized_goal: str) -> str:
    if type(normalized_goal) is not str:
        raise PackagedProductJourneyError("Команда має бути звичайним текстом.")
    goal = " ".join(normalized_goal.split())
    if not goal:
        raise PackagedProductJourneyError("product goal must not be empty")
    digest = hashlib.sha256(goal.encode("utf-8")).hexdigest()
    return f"product-{digest}"


def packaged_product_reopen_target(command: str) -> str | None:
    """Return a strict ProductProject id for an explicit keyboard reopen command.

    Ordinary text containing a project id is deliberately not intercepted. This keeps the
    existing command classifier authoritative unless the user explicitly asks to open/reopen a
    ProductProject. The accepted id is canonicalized to lowercase before durable lookup.
    """
    if type(command) is not str:
        raise PackagedProductJourneyError("Команда має бути звичайним текстом.")
    normalized = " ".join(command.split())
    lowered = normalized.casefold()
    prefix = next(
        (
            item
            for item in _REOPEN_PREFIXES
            if lowered.startswith(item)
            and lowered[len(item) : len(item) + 1] in ("", " ", ":", "#")
        ),
        None,
    )
    if prefix is None:
        return None
    remainder = normalized[len(prefix) :].strip(" :#")
    if not remainder or _PRODUCT_PROJECT_ID.fullmatch(remainder) is None:
        raise PackagedProductJourneyError(
            "Вкажіть повний ProductProject ID у форматі product- і 64 hex-символи."
        )
    return remainder.lower()


def packaged_current_product_command(command: str) -> bool:
    """Recognize an exact keyboard command that reports the durable presentation selection."""
    if type(command) is not str:
        raise PackagedProductJourneyError("Команда має бути звичайним текстом.")
    normalized = " ".join(command.split()).casefold().strip(" :")
    return normalized in _CURRENT_PROJECT_COMMANDS


def packaged_daily_activity_report_command(command: str) -> bool:
    """Recognize explicit read-only daily report commands without broad keyword capture."""
    if type(command) is not str:
        raise PackagedProductJourneyError("Команда має бути звичайним текстом.")
    normalized = " ".join(command.split()).casefold().strip(" :.!?")
    return normalized in _DAILY_ACTIVITY_REPORT_COMMANDS


def packaged_training_status_target(command: str) -> str | None:
    """Return the canonical task UUID for an explicit read-only training-status command."""
    if type(command) is not str:
        raise PackagedProductJourneyError("Команда має бути звичайним текстом.")
    normalized = " ".join(command.split()).strip(" :.!?")
    lowered = normalized.casefold()
    prefix = next(
        (
            item
            for item in _TRAINING_STATUS_PREFIXES
            if lowered == item or lowered.startswith(item + " ")
        ),
        None,
    )
    if prefix is None:
        return None
    task_id = normalized[len(prefix) :].strip(" :#")
    try:
        parsed = UUID(task_id)
    except (ValueError, AttributeError) as exc:
        raise PackagedProductJourneyError(
            "Вкажіть task_id після команди статусу навчання у канонічному UUID-форматі."
        ) from exc
    if str(parsed) != task_id:
        raise PackagedProductJourneyError(
            "Вкажіть task_id після команди статусу навчання у канонічному UUID-форматі."
        )
    return task_id


def _valid_selection_id(value: object) -> bool:
    if type(value) is not str or not value or value != value.strip():
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return not any(
        unicodedata.category(character) in {"Cc", "Cf", "Zl", "Zp"}
        for character in value
    )


class PackagedProductSelectionStore:
    """Durable presentation-only selection for the packaged ProductCommandCenter.

    This record is not ProductProject authority. It stores only the opaque project identity needed
    to restore the last visible ProductProject after an application/process restart.
    """

    def __init__(self, store: SQLiteStore) -> None:
        self._store = store
        with self._store.connection() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS packaged_product_selection ("
                "slot INTEGER PRIMARY KEY CHECK(slot = 1), "
                "project_id TEXT NOT NULL CHECK(length(trim(project_id)) > 0))"
            )

    def load(self) -> str | None:
        with self._store.connection() as conn:
            row = conn.execute(
                "SELECT typeof(project_id) AS id_type, "
                "CAST(project_id AS BLOB) AS raw_id "
                "FROM packaged_product_selection WHERE slot = 1"
            ).fetchone()
        if row is None or row["id_type"] != "text":
            return None
        try:
            project_id = row["raw_id"].decode("utf-8")
        except UnicodeDecodeError:
            return None
        return project_id if _valid_selection_id(project_id) else None

    def select(self, project_id: str) -> None:
        if type(project_id) is not str:
            raise PackagedProductJourneyError("selected ProductProject id must be text")
        normalized = project_id.strip()
        if not _valid_selection_id(normalized):
            raise PackagedProductJourneyError("selected ProductProject id contains invalid text")
        with self._store.connection() as conn:
            conn.execute(
                "INSERT INTO packaged_product_selection(slot, project_id) VALUES (1, ?) "
                "ON CONFLICT(slot) DO UPDATE SET project_id = excluded.project_id",
                (normalized,),
            )

    def clear(self) -> None:
        with self._store.connection() as conn:
            conn.execute("DELETE FROM packaged_product_selection WHERE slot = 1")


class PackagedProductCommandRouter:
    """Route packaged command input to durable ProductProject, read-only report, or task handling.

    Product intent creates/reopens a durable PF1 ProductProject through the public PF5 adapter.
    Explicit daily-report and training-status intents may call injected read-only handlers.
    This boundary deliberately does not dispatch workers, deploy providers, Toolsmith, or any
    high-impact external action. Those remain downstream explicit factory/security boundaries.
    """

    def __init__(
        self,
        *,
        products: ProductProjectCommandService,
        ordinary_handler: OrdinaryCommandHandler,
        activity_report_handler: ActivityReportHandler | None = None,
        training_status_handler: TrainingStatusHandler | None = None,
        selection_store: PackagedProductSelectionStore | None = None,
    ) -> None:
        self._products = products
        self._ordinary_handler = ordinary_handler
        self._activity_report_handler = activity_report_handler
        self._training_status_handler = training_status_handler
        self._selection_store = selection_store
        self._active_project_id = selection_store.load() if selection_store is not None else None

    @property
    def active_project_id(self) -> str | None:
        """Return presentation selection; durable authority remains in ProductProject state."""
        return self._active_project_id

    def clear_stale_selection(self) -> None:
        self._active_project_id = None
        if self._selection_store is not None:
            self._selection_store.clear()

    def _select_existing_project(self, project_id: str) -> UIResult:
        try:
            detail = self._products.inspect_project(project_id)
        except KeyError as exc:
            raise PackagedProductJourneyError(
                f"ProductProject не знайдено: {project_id}. Поточний вибір не змінено."
            ) from exc
        except ProductProjectPresentationConsistencyError as exc:
            raise PackagedProductJourneyError(
                "ProductProject changed while packaged state was read; retry the reopen command."
            ) from exc
        if self._selection_store is not None:
            self._selection_store.select(project_id)
        self._active_project_id = project_id
        return UIResult(
            request_id="desktop-handler",
            status="completed",
            message=(
                f"ProductProject відкрито: {project_id}; "
                f"spec version {detail.summary.version}; state {detail.summary.state}."
            ),
            focus_id="tasks-heading",
        )

    def _describe_current_project(self) -> UIResult:
        project_id = self._active_project_id
        if project_id is None:
            raise PackagedProductJourneyError(
                "Поточний ProductProject не вибрано. Створіть продукт або відкрийте його за ID."
            )
        try:
            detail = self._products.inspect_project(project_id)
        except KeyError as exc:
            self.clear_stale_selection()
            raise PackagedProductJourneyError(
                "Збережений ProductProject більше не існує. Застарілий вибір очищено."
            ) from exc
        except ProductProjectPresentationConsistencyError as exc:
            raise PackagedProductJourneyError(
                "ProductProject changed while packaged state was read; retry the current command."
            ) from exc
        return UIResult(
            request_id="desktop-handler",
            status="completed",
            message=(
                f"Поточний ProductProject: {project_id}; "
                f"spec version {detail.summary.version}; state {detail.summary.state}; "
                f"goal: {detail.summary.goal}."
            ),
            focus_id="tasks-heading",
        )

    def create(self, payload: Mapping[str, Any]) -> UIResult:
        raw_command = payload.get("command", "")
        if type(raw_command) is not str:
            raise PackagedProductJourneyError("Команда повинна бути текстом.")
        command = raw_command.strip()
        if not command:
            raise PackagedProductJourneyError(
                "Введіть команду перед створенням завдання."
            )
        if "\x00" in command:
            raise PackagedProductJourneyError("Команда містить недопустимий NUL-символ.")
        try:
            command.encode("utf-8")
        except UnicodeEncodeError:
            raise PackagedProductJourneyError(
                "Команда містить некоректний текст Unicode."
            ) from None

        if packaged_daily_activity_report_command(command):
            if self._activity_report_handler is None:
                raise PackagedProductJourneyError(
                    "Щоденний звіт активності недоступний у цьому запуску."
                )
            return self._activity_report_handler()

        training_task_id = packaged_training_status_target(command)
        if training_task_id is not None:
            if self._training_status_handler is None:
                raise PackagedProductJourneyError(
                    "Статус навчання недоступний у цьому запуску."
                )
            return self._training_status_handler(training_task_id)

        if packaged_current_product_command(command):
            return self._describe_current_project()

        reopen_target = packaged_product_reopen_target(command)
        if reopen_target is not None:
            return self._select_existing_project(reopen_target)

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
        if self._selection_store is not None:
            self._selection_store.select(project_id)
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

    Durable ProductProject identity, lifecycle and decisions remain owned by PF1/PF5 repositories.
    The separate packaged selection record contains only the opaque project id needed to restore
    the last visible project after restart; it is never accepted as project authority.

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
        except KeyError:
            self._router.clear_stale_selection()
            return state
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
