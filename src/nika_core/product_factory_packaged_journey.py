from __future__ import annotations

import hashlib
import re
import unicodedata
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from nika_core.data.sqlite import SQLiteStore
from nika_core.packaged_intelligence_mode import is_packaged_intelligence_mode_command
from nika_core.product_command.command_center import ProductCommandCenter
from nika_core.product_command.contracts import (
    CommandRouteKind,
    ProductProjectDetail,
    ProductStatusKind,
    ProductUserDecision,
)
from nika_core.product_command.product_project_adapter import (
    ProductProjectCommandService,
    ProductProjectDecisionNotFoundError,
    ProductProjectPresentationConsistencyError,
)
from nika_core.product_command.routing import route_command
from nika_core.product_project import (
    ProductDecision,
    ProductDecisionState,
    ProductProjectSpec,
)
from nika_core.security import ApprovalAuthority
from nika_core.ui.bridge_models import UIResult

OrdinaryCommandHandler = Callable[[Mapping[str, Any]], UIResult]
AgentBuilderCommandHandler = Callable[[Mapping[str, Any]], UIResult]
TaskControlHandler = Callable[[Mapping[str, Any]], UIResult]
TaskStatusHandler = Callable[[str | None], UIResult]
ActivityReportHandler = Callable[[], UIResult]
TrainingStatusHandler = Callable[[str], UIResult]
IntelligenceModeCommandHandler = Callable[[str], UIResult]
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
_PRODUCT_STATUS_PREVIEW_LIMIT = 24
_CURRENT_DECISION_COMMANDS = frozenset(
    {
        "current product decision",
        "show current product decision",
        "поточне рішення productproject",
        "покажи поточне рішення productproject",
    }
)
_DECISION_APPROVE_PREFIXES = (
    "approve product decision",
    "схвали рішення productproject",
    "схвалити рішення productproject",
)
_DECISION_REJECT_PREFIXES = (
    "reject product decision",
    "відхили рішення productproject",
    "відхилити рішення productproject",
)
_DECISION_CONFIRM_PREFIXES = (
    "confirm product decision approval",
    "підтвердь схвалення рішення productproject",
    "підтвердити схвалення рішення productproject",
)
_DECISION_SHOW_PREFIXES = (
    "show product decision",
    "покажи рішення productproject",
)
_PENDING_DECISION_LIST_COMMANDS = (
    "list pending product decisions",
    "покажи рішення productproject, що очікують",
)
_PENDING_DECISION_PAGE_SIZE = 8
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
_TASK_PAUSE_COMMANDS = frozenset(
    {
        "pause task",
        "pause current task",
        "призупини завдання",
        "призупинити завдання",
        "призупини поточне завдання",
    }
)
_TASK_RESUME_COMMANDS = frozenset(
    {
        "resume task",
        "resume current task",
        "continue task",
        "віднови завдання",
        "відновити завдання",
        "продовж завдання",
        "продовжити завдання",
    }
)
_TASK_STOP_COMMANDS = frozenset(
    {
        "stop task",
        "cancel task",
        "stop current task",
        "зупини завдання",
        "зупинити завдання",
        "скасуй завдання",
        "скасувати завдання",
    }
)
_TASK_STATUS_COMMANDS = frozenset(
    {
        "current task",
        "show current task",
        "task status",
        "current task status",
        "поточне завдання",
        "покажи поточне завдання",
        "статус завдання",
        "статус поточного завдання",
    }
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


def packaged_current_product_decision_command(command: str) -> bool:
    """Recognize an exact read-only command for the unambiguous pending product decision."""
    if type(command) is not str:
        raise PackagedProductJourneyError("Команда має бути звичайним текстом.")
    normalized = " ".join(command.split()).casefold().strip(" :.!?")
    return normalized in _CURRENT_DECISION_COMMANDS


def _prefixed_owner_identifier(
    command: str,
    *,
    prefixes: tuple[str, ...],
    label: str,
    maximum: int,
) -> str | None:
    normalized = " ".join(command.split())
    lowered = normalized.casefold()
    for prefix in prefixes:
        if lowered == prefix:
            raise PackagedProductJourneyError(f"Вкажіть {label} після команди.")
        if not (
            lowered.startswith(prefix + " ")
            or lowered.startswith(prefix + ":")
        ):
            continue
        value = normalized[len(prefix) :].strip(" :")
        if (
            not value
            or len(value) > maximum
            or not _valid_selection_id(value)
        ):
            raise PackagedProductJourneyError(
                f"{label} має бути непорожнім безпечним ідентифікатором "
                f"довжиною не більше {maximum} символів."
            )
        return value
    return None


def packaged_product_decision_action(command: str) -> tuple[str, str] | None:
    """Recognize exact owner-decision commands without broad natural-language capture."""
    if type(command) is not str:
        raise PackagedProductJourneyError("Команда має бути звичайним текстом.")
    for action, prefixes, label, maximum in (
        ("approve", _DECISION_APPROVE_PREFIXES, "decision_id", 160),
        ("reject", _DECISION_REJECT_PREFIXES, "decision_id", 160),
        ("confirm", _DECISION_CONFIRM_PREFIXES, "approval request_id", 160),
    ):
        value = _prefixed_owner_identifier(
            command,
            prefixes=prefixes,
            label=label,
            maximum=maximum,
        )
        if value is None:
            continue
        if action == "confirm" and not value.startswith("approval-request-"):
            raise PackagedProductJourneyError(
                "approval request_id має починатися з «approval-request-»."
            )
        return action, value
    return None


def packaged_product_decision_query(command: str) -> tuple[str, str | int] | None:
    """Recognize bounded decision discovery/read commands for ambiguous pending sets."""
    if type(command) is not str:
        raise PackagedProductJourneyError("Команда має бути звичайним текстом.")
    normalized = " ".join(command.split())
    lowered = normalized.casefold()
    for base in _PENDING_DECISION_LIST_COMMANDS:
        if lowered == base:
            return "list", 1
        for marker in (" page ", " сторінка "):
            prefix = base + marker
            if not lowered.startswith(prefix):
                continue
            raw_page = normalized[len(prefix) :].strip()
            if not raw_page.isascii() or not raw_page.isdecimal():
                raise PackagedProductJourneyError(
                    "Номер сторінки рішень має бути додатним цілим числом."
                )
            page = int(raw_page)
            if page < 1 or page > 1_000_000:
                raise PackagedProductJourneyError(
                    "Номер сторінки рішень має бути в межах 1..1000000."
                )
            return "list", page

    decision_id = _prefixed_owner_identifier(
        command,
        prefixes=_DECISION_SHOW_PREFIXES,
        label="decision_id",
        maximum=160,
    )
    if decision_id is not None:
        return "show", decision_id
    return None


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


def packaged_task_direct_target(command: str) -> tuple[str, str | None] | None:
    """Recognize exact long-task controls and an optional canonical task UUID."""
    if type(command) is not str:
        raise PackagedProductJourneyError("Команда має бути звичайним текстом.")
    normalized = " ".join(command.split()).strip(" :.!?")
    lowered = normalized.casefold()
    for action, commands in (
        ("pause", _TASK_PAUSE_COMMANDS),
        ("resume", _TASK_RESUME_COMMANDS),
        ("stop", _TASK_STOP_COMMANDS),
        ("status", _TASK_STATUS_COMMANDS),
    ):
        if lowered in commands:
            return action, None
        prefixes = tuple(
            item for item in commands if lowered.startswith(item + " ")
        )
        if not prefixes:
            continue
        prefix = max(prefixes, key=len)
        task_id = normalized[len(prefix) :].strip(" :#")
        if len(task_id.split()) != 1:
            return None
        try:
            parsed = UUID(task_id)
        except (ValueError, AttributeError) as exc:
            raise PackagedProductJourneyError(
                "Вкажіть task_id у канонічному UUID-форматі після команди керування."
            ) from exc
        if str(parsed) != task_id:
            raise PackagedProductJourneyError(
                "Вкажіть task_id у канонічному UUID-форматі після команди керування."
            )
        return action, task_id
    return None


def packaged_task_direct_action(command: str) -> str | None:
    """Return the action part of an exact direct task command."""
    target = packaged_task_direct_target(command)
    return target[0] if target is not None else None


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


@dataclass(frozen=True, slots=True)
class _PendingPackagedDecisionApproval:
    project_id: str
    decision: ProductDecision
    expected_row_version: int
    idempotency_key: str


class PackagedProductCommandRouter:
    """Route packaged command input to durable ProductProject, read-only report, or task handling.

    Product intent creates/reopens a durable PF1 ProductProject through the public PF5 adapter.
    Explicit daily-report, training-status, intelligence-mode and long-task control intents
    delegate only to injected incumbent handlers. Explicit Agent Builder intent delegates only
    to an injected
    safe-draft handler. Toolsmith remains a separate fail-closed route. No high-impact external
    action is launched merely by command classification.
    """

    def __init__(
        self,
        *,
        products: ProductProjectCommandService,
        ordinary_handler: OrdinaryCommandHandler,
        agent_builder_handler: AgentBuilderCommandHandler | None = None,
        task_pause_handler: TaskControlHandler | None = None,
        task_resume_handler: TaskControlHandler | None = None,
        task_stop_handler: TaskControlHandler | None = None,
        task_status_handler: TaskStatusHandler | None = None,
        activity_report_handler: ActivityReportHandler | None = None,
        training_status_handler: TrainingStatusHandler | None = None,
        intelligence_mode_handler: IntelligenceModeCommandHandler | None = None,
        selection_store: PackagedProductSelectionStore | None = None,
        decision_approval_authority: ApprovalAuthority | None = None,
    ) -> None:
        self._products = products
        self._ordinary_handler = ordinary_handler
        self._agent_builder_handler = agent_builder_handler
        self._task_pause_handler = task_pause_handler
        self._task_resume_handler = task_resume_handler
        self._task_stop_handler = task_stop_handler
        self._task_status_handler = task_status_handler
        self._activity_report_handler = activity_report_handler
        self._training_status_handler = training_status_handler
        self._intelligence_mode_handler = intelligence_mode_handler
        self._selection_store = selection_store
        self._decision_approval_authority = decision_approval_authority
        self._pending_decision_approvals: dict[
            str, _PendingPackagedDecisionApproval
        ] = {}
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

    def _describe_current_decision(self) -> UIResult:
        project_id = self._active_project_id
        if project_id is None:
            raise PackagedProductJourneyError(
                "Поточний ProductProject не вибрано. Спочатку створіть або відкрийте його."
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
                "ProductProject changed while packaged state was read; "
                "retry the current-decision command."
            ) from exc

        decision = detail.summary.current_decision
        if decision is None:
            pending_count = sum(item.state == "pending" for item in detail.decisions)
            if pending_count > 1:
                raise PackagedProductJourneyError(
                    "Кілька рішень ProductProject очікують власника; "
                    "жодне не вибрано автоматично."
                )
            raise PackagedProductJourneyError(
                "Поточне рішення ProductProject відсутнє."
            )
        return UIResult(
            request_id="desktop-handler",
            status="completed",
            message=(
                f"Поточне рішення ProductProject: {decision.decision_id}; "
                f"{decision.title}; ризик R{decision.risk_level}; {decision.question}"
            ),
            focus_id="product-project-decision-heading",
        )

    def _decision_read_project_id(self) -> str:
        project_id = self._active_project_id
        if project_id is None:
            raise PackagedProductJourneyError(
                "Поточний ProductProject не вибрано. Спочатку створіть або відкрийте його."
            )
        return project_id

    def _describe_product_decision(self, decision_id: str) -> UIResult:
        project_id = self._decision_read_project_id()
        try:
            decision = self._products.inspect_decision(project_id, decision_id)
        except ProductProjectDecisionNotFoundError as exc:
            raise PackagedProductJourneyError(
                f"Рішення ProductProject не знайдено: {decision_id}."
            ) from exc
        except KeyError as exc:
            self.clear_stale_selection()
            raise PackagedProductJourneyError(
                "Збережений ProductProject більше не існує. "
                "Застарілий вибір очищено."
            ) from exc
        except ProductProjectPresentationConsistencyError as exc:
            raise PackagedProductJourneyError(
                "ProductProject changed while decision state was read; retry the command."
            ) from exc
        return UIResult(
            request_id="desktop-handler",
            status="completed",
            message=(
                f"Рішення ProductProject: {decision.decision_id}; "
                f"{decision.title}; стан {decision.state}; ризик R{decision.risk_level}; "
                f"{decision.question}"
            ),
            focus_id="product-project-heading",
        )

    def _list_pending_product_decisions(self, page: int) -> UIResult:
        project_id = self._decision_read_project_id()
        start = (page - 1) * _PENDING_DECISION_PAGE_SIZE
        try:
            selected, total = self._products.list_decisions_by_state(
                project_id,
                ProductDecisionState.PROPOSED,
                limit=_PENDING_DECISION_PAGE_SIZE,
                offset=start,
            )
        except KeyError as exc:
            self.clear_stale_selection()
            raise PackagedProductJourneyError(
                "Збережений ProductProject більше не існує. Застарілий вибір очищено."
            ) from exc
        except ProductProjectPresentationConsistencyError as exc:
            raise PackagedProductJourneyError(
                "ProductProject changed while decision state was read; retry the command."
            ) from exc
        if total == 0:
            return UIResult(
                request_id="desktop-handler",
                status="completed",
                message="Рішень ProductProject, що очікують власника, немає.",
                focus_id="product-project-heading",
            )
        if not selected:
            last_page = (total - 1) // _PENDING_DECISION_PAGE_SIZE + 1
            raise PackagedProductJourneyError(
                f"Сторінка {page} відсутня. Остання сторінка: {last_page}."
            )
        end = start + len(selected)
        summary = " | ".join(
            f"{item.decision_id}; {item.title}; R{item.risk_level}"
            for item in selected
        )
        next_hint = (
            ""
            if end >= total
            else (
                " Наступна сторінка: list pending product decisions page "
                f"{page + 1}."
            )
        )
        return UIResult(
            request_id="desktop-handler",
            status="completed",
            message=(
                f"Рішення ProductProject, що очікують власника: "
                f"{start + 1}-{end} із {total}. {summary}.{next_hint}"
            ),
            focus_id="product-project-heading",
        )

    def _prepare_owner_decision(
        self,
        decision_id: str,
        target_state: ProductDecisionState,
    ) -> tuple[str, ProductDecision, int, str]:
        project_id = self._active_project_id
        if project_id is None:
            raise PackagedProductJourneyError(
                "Поточний ProductProject не вибрано. Спочатку створіть або відкрийте його."
            )
        idempotency_key = (
            f"packaged-owner-decision:{project_id}:{decision_id}:{target_state.value}"
        )
        try:
            decision, row_version = self._products.prepare_owner_decision(
                project_id,
                decision_id,
                target_state,
            )
        except KeyError as exc:
            raise PackagedProductJourneyError(
                f"Рішення ProductProject не знайдено: {decision_id}."
            ) from exc
        except (ValueError, ProductProjectPresentationConsistencyError) as exc:
            raise PackagedProductJourneyError(
                "Рішення ProductProject змінилося, вже завершене іншим результатом "
                "або недоступне. Оновіть стан і повторіть точну команду."
            ) from exc
        return project_id, decision, row_version, idempotency_key

    def _prune_pending_decision_approvals(self) -> None:
        authority = self._decision_approval_authority
        if authority is None:
            self._pending_decision_approvals.clear()
            return
        valid = {item.request_id for item in authority.pending_views()}
        for request_id in tuple(self._pending_decision_approvals):
            if request_id not in valid:
                self._pending_decision_approvals.pop(request_id, None)

    def _request_product_decision_approval(self, decision_id: str) -> UIResult:
        authority = self._decision_approval_authority
        if authority is None:
            raise PackagedProductJourneyError(
                "Trusted owner approval недоступне у цьому запуску."
            )
        project_id, decision, row_version, idempotency_key = self._prepare_owner_decision(
            decision_id,
            ProductDecisionState.APPROVED,
        )

        # Exact durable replay is intentionally attempted before a new request.
        # The canonical repository returns an already-committed matching effect without
        # demanding a second ApprovalEvidence.
        try:
            self._products.record_decision(
                project_id,
                decision,
                expected_row_version=row_version,
                idempotency_key=idempotency_key,
            )
        except PermissionError as exc:
            if "trusted product-owner approval" not in str(exc):
                raise PackagedProductJourneyError(
                    "Trusted owner approval не пройшло перевірку."
                ) from exc
        except ValueError as exc:
            raise PackagedProductJourneyError(
                "Рішення ProductProject змінилося. Оновіть стан і повторіть команду."
            ) from exc
        else:
            return UIResult(
                request_id="desktop-handler",
                status="completed",
                message=f"Рішення ProductProject уже схвалено: {decision_id}.",
                focus_id="product-project-decision-heading",
            )

        self._prune_pending_decision_approvals()
        for request_id, pending in self._pending_decision_approvals.items():
            if (
                pending.project_id == project_id
                and pending.decision.decision_id == decision_id
                and pending.expected_row_version == row_version
                and pending.idempotency_key == idempotency_key
            ):
                return UIResult(
                    request_id="desktop-handler",
                    status="completed",
                    message=(
                        "Схвалення ще не виконано. Підтвердіть exact approval request: "
                        f"{request_id}. Команда: confirm product decision approval {request_id}"
                    ),
                    focus_id="command-input",
                )

        try:
            intent = self._products.decision_approval_intent(
                project_id,
                decision,
                expected_row_version=row_version,
                idempotency_key=idempotency_key,
            )
            request = authority.request(intent)
        except (PermissionError, ValueError) as exc:
            raise PackagedProductJourneyError(
                "Не вдалося створити точний trusted approval request. "
                "Оновіть ProductProject і повторіть команду."
            ) from exc
        self._pending_decision_approvals[request.request_id] = (
            _PendingPackagedDecisionApproval(
                project_id=project_id,
                decision=decision,
                expected_row_version=row_version,
                idempotency_key=idempotency_key,
            )
        )
        return UIResult(
            request_id="desktop-handler",
            status="completed",
            message=(
                "Рішення ще не схвалено. Створено одноразовий trusted approval request: "
                f"{request.request_id}. Щоб підтвердити саме цю дію, введіть: "
                f"confirm product decision approval {request.request_id}"
            ),
            focus_id="command-input",
        )

    def _confirm_product_decision_approval(self, request_id: str) -> UIResult:
        authority = self._decision_approval_authority
        if authority is None:
            raise PackagedProductJourneyError(
                "Trusted owner approval недоступне у цьому запуску."
            )
        self._prune_pending_decision_approvals()
        pending = self._pending_decision_approvals.get(request_id)
        if pending is None:
            raise PackagedProductJourneyError(
                "Approval request невідомий, прострочений або належить іншому запуску. "
                "Створіть новий exact request командою схвалення."
            )
        if self._active_project_id != pending.project_id:
            self._pending_decision_approvals.pop(request_id, None)
            try:
                authority.deny(request_id)
            except (KeyError, PermissionError):
                pass
            raise PackagedProductJourneyError(
                "Поточний ProductProject змінився. Approval request скасовано; "
                "відкрийте потрібний ProductProject і створіть новий."
            )
        try:
            approval = authority.approve(request_id)
        except (KeyError, PermissionError, ValueError) as exc:
            self._pending_decision_approvals.pop(request_id, None)
            raise PackagedProductJourneyError(
                "Approval request невідомий або прострочений. Створіть новий."
            ) from exc

        self._pending_decision_approvals.pop(request_id, None)
        try:
            self._products.record_decision(
                pending.project_id,
                pending.decision,
                expected_row_version=pending.expected_row_version,
                idempotency_key=pending.idempotency_key,
                approval=approval,
            )
        except (PermissionError, ValueError) as exc:
            raise PackagedProductJourneyError(
                "ProductProject, decision або evidence змінилися після запиту. "
                "Схвалення не записано; оновіть стан і створіть новий approval request."
            ) from exc
        return UIResult(
            request_id="desktop-handler",
            status="completed",
            message=(
                "Рішення ProductProject схвалено через trusted owner authority: "
                f"{pending.decision.decision_id}."
            ),
            focus_id="product-project-heading",
        )

    def _reject_product_decision(self, decision_id: str) -> UIResult:
        project_id, decision, row_version, idempotency_key = self._prepare_owner_decision(
            decision_id,
            ProductDecisionState.REJECTED,
        )
        try:
            self._products.record_decision(
                project_id,
                decision,
                expected_row_version=row_version,
                idempotency_key=idempotency_key,
            )
        except (PermissionError, ValueError) as exc:
            raise PackagedProductJourneyError(
                "Рішення ProductProject змінилося. Відхилення не записано; "
                "оновіть стан і повторіть точну команду."
            ) from exc
        return UIResult(
            request_id="desktop-handler",
            status="completed",
            message=f"Рішення ProductProject відхилено: {decision_id}.",
            focus_id="product-project-heading",
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

        if is_packaged_intelligence_mode_command(command):
            if self._intelligence_mode_handler is None:
                raise PackagedProductJourneyError(
                    "Керування режимом інтелекту недоступне у цьому запуску."
                )
            return self._intelligence_mode_handler(command)

        training_task_id = packaged_training_status_target(command)
        if training_task_id is not None:
            if self._training_status_handler is None:
                raise PackagedProductJourneyError(
                    "Статус навчання недоступний у цьому запуску."
                )
            return self._training_status_handler(training_task_id)

        task_direct = packaged_task_direct_target(command)
        if task_direct is not None:
            task_action, task_id = task_direct
            if task_action == "status":
                if self._task_status_handler is None:
                    raise PackagedProductJourneyError(
                        "Статус поточного завдання недоступний у цьому запуску."
                    )
                return self._task_status_handler(task_id)
            handler = {
                "pause": self._task_pause_handler,
                "resume": self._task_resume_handler,
                "stop": self._task_stop_handler,
            }[task_action]
            if handler is None:
                raise PackagedProductJourneyError(
                    f"Керування завданням «{task_action}» недоступне у цьому запуску."
                )
            # Direct command text and unrelated UI fields are classification input only.
            # Only the canonical target identity crosses into incumbent task-control authority.
            return handler({"task_id": task_id} if task_id is not None else {})
        decision_query = packaged_product_decision_query(command)
        if decision_query is not None:
            query, identity = decision_query
            if query == "show":
                if type(identity) is not str:
                    raise PackagedProductJourneyError("invalid ProductDecision query identity")
                return self._describe_product_decision(identity)
            if query == "list":
                if type(identity) is not int:
                    raise PackagedProductJourneyError("invalid ProductDecision page identity")
                return self._list_pending_product_decisions(identity)
            raise PackagedProductJourneyError("unsupported ProductDecision query")
        decision_action = packaged_product_decision_action(command)
        if decision_action is not None:
            action, identity = decision_action
            if action == "approve":
                return self._request_product_decision_approval(identity)
            if action == "reject":
                return self._reject_product_decision(identity)
            if action == "confirm":
                return self._confirm_product_decision_approval(identity)
            raise PackagedProductJourneyError("unsupported ProductDecision owner action")
        if packaged_current_product_decision_command(command):
            return self._describe_current_decision()
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
                "Команда одночасно відповідає кільком спеціалізованим маршрутам. "
                "Уточніть, чи потрібно створити ProductProject, агента через Agent Builder, "
                "чи нову можливість Toolsmith."
            )
        if decision.route is CommandRouteKind.AGENT_BUILDER:
            if self._agent_builder_handler is None:
                raise PackagedProductJourneyError(
                    "Команда визначена як запит Agent Builder. Поточна packaged-композиція "
                    "ще не підключила Agent Builder handler; звичайне завдання не створено."
                )
            return self._agent_builder_handler(payload)
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
    status_items = _safe_product_status_items(detail)
    return {
        "project_id": detail.summary.project_id,
        "spec_version": detail.summary.version,
        "title": detail.summary.title,
        "goal": detail.summary.goal,
        "state": detail.summary.state,
        "blocker_count": detail.summary.blocker_count,
        "status_count": len(detail.statuses),
        "status_counts": dict(sorted(status_counts.items())),
        "status_items": status_items,
        "status_items_truncated": len(status_items) < len(detail.statuses),
        "decision_count": len(detail.decisions),
        "decision_state_counts": dict(sorted(decision_counts.items())),
        "current_decision": _safe_product_decision(detail.summary.current_decision),
    }


def _safe_product_status_items(
    detail: ProductProjectDetail,
) -> list[dict[str, Any]]:
    blockers = [
        item for item in detail.statuses if item.kind is ProductStatusKind.BLOCKER
    ]
    others = [
        item for item in detail.statuses if item.kind is not ProductStatusKind.BLOCKER
    ]
    selected = (blockers + others)[:_PRODUCT_STATUS_PREVIEW_LIMIT]
    return [
        {
            "kind": item.kind.value,
            "item_id": item.item_id,
            "label": item.label,
            "state": item.state,
            "detail": item.detail,
        }
        for item in selected
    ]


def _safe_product_decision(
    decision: ProductUserDecision | None,
) -> dict[str, Any] | None:
    if decision is None:
        return None
    return {
        "decision_id": decision.decision_id,
        "title": decision.title,
        "question": decision.question,
        "risk_level": decision.risk_level,
        "state": decision.state,
    }


def _project_title(goal: str) -> str:
    normalized = " ".join(goal.split())
    return normalized if len(normalized) <= 160 else normalized[:157] + "..."
