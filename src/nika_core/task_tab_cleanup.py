"""Lifecycle cleanup policy for canonical task-owned browser tabs.

This module does not own browser pages or external-effect state. It decides when the existing
``TaskBrowserTabs`` owner may be asked to close records and emits JSON-safe checkpoint metadata for
restart/reconciliation. Canonical effect truth remains in the runtime/effect ledger.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .task_browser_tabs import TaskBrowserTabs, TaskOwnedTab

_CHECKPOINT_SCHEMA = 1
_MAX_EFFECT_REF_LENGTH = 512


class TaskTabCleanupError(RuntimeError):
    """Base error for invalid or unsafe task-tab cleanup decisions."""


class TaskTabCleanupBlockedError(TaskTabCleanupError):
    """Destructive cleanup was requested while reconciliation evidence must be preserved."""


class TaskTabCleanupEvent(StrEnum):
    """Lifecycle reasons that may influence task-tab cleanup."""

    CONFIRMED_SUCCESS = "confirmed_success"
    TERMINAL_DETERMINISTIC_FAILURE = "terminal_deterministic_failure"
    TASK_COMPLETE = "task_complete"
    CANCEL = "cancel"
    PAUSE = "pause"
    RETRY = "retry"
    UNCERTAIN_EXTERNAL_EFFECT = "uncertain_external_effect"
    APPLICATION_CLOSE = "application_close"
    APPLICATION_RESTART = "application_restart"


class TaskTabCleanupAction(StrEnum):
    """Effect of a cleanup decision on canonical tab ownership."""

    CLOSE_TAB = "close_tab"
    CLEANUP_TASK = "cleanup_task"
    PRESERVE = "preserve"
    PRESERVE_FOR_RECONCILIATION = "preserve_for_reconciliation"
    CHECKPOINT_ONLY = "checkpoint_only"


@dataclass(frozen=True, slots=True)
class TaskTabReconciliationEvidence:
    """Opaque durable external-effect identity bound to one logical task-owned tab."""

    task_id: str
    tab_id: str
    effect_ref: str

    def __post_init__(self) -> None:
        _validate_nonempty("task_id", self.task_id, 256)
        _validate_nonempty("tab_id", self.tab_id, 256)
        _validate_nonempty("effect_ref", self.effect_ref, _MAX_EFFECT_REF_LENGTH)

    def to_dict(self) -> dict[str, str]:
        return {
            "task_id": self.task_id,
            "tab_id": self.tab_id,
            "effect_ref": self.effect_ref,
        }


@dataclass(frozen=True, slots=True)
class TaskTabCleanupResult:
    """JSON-safe policy result suitable for an existing task/checkpoint persistence boundary."""

    event: TaskTabCleanupEvent
    action: TaskTabCleanupAction
    task_id: str
    tab_id: str | None
    affected_tab_ids: tuple[str, ...]
    task_tabs_snapshot: dict[str, object]
    reconciliation: tuple[TaskTabReconciliationEvidence, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": _CHECKPOINT_SCHEMA,
            "event": self.event.value,
            "action": self.action.value,
            "task_id": self.task_id,
            "tab_id": self.tab_id,
            "affected_tab_ids": list(self.affected_tab_ids),
            "task_tabs_snapshot": self.task_tabs_snapshot,
            "reconciliation": [item.to_dict() for item in self.reconciliation],
        }


def apply_task_tab_cleanup(
    tabs: TaskBrowserTabs,
    *,
    event: TaskTabCleanupEvent,
    task_id: str,
    tab_id: str | None = None,
    unresolved_external_effect: bool | None = None,
    reconciliation: tuple[TaskTabReconciliationEvidence, ...] = (),
) -> TaskTabCleanupResult:
    """Apply the minimum safe cleanup allowed by the supplied lifecycle event.

    The caller must derive ``unresolved_external_effect`` and ``reconciliation`` from canonical
    durable effect authority. This policy never infers or mutates effect status itself.
    """

    if not isinstance(event, TaskTabCleanupEvent):
        raise TypeError("event must be a TaskTabCleanupEvent")
    if unresolved_external_effect is not None and not isinstance(unresolved_external_effect, bool):
        raise TypeError("unresolved_external_effect must be a bool or None")
    if not isinstance(reconciliation, tuple):
        raise TypeError("reconciliation must be a tuple")

    owned = tabs.owned_tabs(task_id)
    owned_by_id = {item.tab_id: item for item in owned}
    _validate_scope(event=event, tab_id=tab_id)
    if tab_id is not None and tab_id not in owned_by_id:
        raise TaskTabCleanupError("cleanup target is not owned by the supplied task")

    uncertainty = (
        unresolved_external_effect is True
        or event is TaskTabCleanupEvent.UNCERTAIN_EXTERNAL_EFFECT
    )
    _validate_reconciliation(
        task_id=task_id,
        tab_id=tab_id,
        owned_by_id=owned_by_id,
        uncertainty=uncertainty,
        reconciliation=reconciliation,
    )

    if event in {
        TaskTabCleanupEvent.CONFIRMED_SUCCESS,
        TaskTabCleanupEvent.TERMINAL_DETERMINISTIC_FAILURE,
    }:
        if uncertainty:
            raise TaskTabCleanupBlockedError(
                "terminal tab cleanup conflicts with unresolved external-effect evidence"
            )
        assert tab_id is not None
        tabs.close_tab(task_id=task_id, tab_id=tab_id)
        return _result(
            tabs=tabs,
            event=event,
            action=TaskTabCleanupAction.CLOSE_TAB,
            task_id=task_id,
            tab_id=tab_id,
            affected_tab_ids=(tab_id,),
        )

    if event in {TaskTabCleanupEvent.TASK_COMPLETE, TaskTabCleanupEvent.CANCEL}:
        if unresolved_external_effect is None:
            raise TaskTabCleanupBlockedError(
                "task-terminal cleanup requires explicit external-effect clearance"
            )
        if uncertainty:
            return _result(
                tabs=tabs,
                event=event,
                action=TaskTabCleanupAction.PRESERVE_FOR_RECONCILIATION,
                task_id=task_id,
                tab_id=None,
                reconciliation=reconciliation,
            )
        removed = tabs.cleanup_task(task_id)
        return _result(
            tabs=tabs,
            event=event,
            action=TaskTabCleanupAction.CLEANUP_TASK,
            task_id=task_id,
            tab_id=None,
            affected_tab_ids=tuple(item.tab_id for item in removed),
        )

    if event is TaskTabCleanupEvent.UNCERTAIN_EXTERNAL_EFFECT:
        return _result(
            tabs=tabs,
            event=event,
            action=TaskTabCleanupAction.PRESERVE_FOR_RECONCILIATION,
            task_id=task_id,
            tab_id=tab_id,
            reconciliation=reconciliation,
        )

    if event is TaskTabCleanupEvent.APPLICATION_CLOSE:
        return _result(
            tabs=tabs,
            event=event,
            action=TaskTabCleanupAction.CHECKPOINT_ONLY,
            task_id=task_id,
            tab_id=None,
            reconciliation=reconciliation,
        )

    action = (
        TaskTabCleanupAction.PRESERVE_FOR_RECONCILIATION
        if uncertainty
        else TaskTabCleanupAction.PRESERVE
    )
    return _result(
        tabs=tabs,
        event=event,
        action=action,
        task_id=task_id,
        tab_id=tab_id,
        reconciliation=reconciliation,
    )


def _validate_scope(*, event: TaskTabCleanupEvent, tab_id: str | None) -> None:
    tab_events = {
        TaskTabCleanupEvent.CONFIRMED_SUCCESS,
        TaskTabCleanupEvent.TERMINAL_DETERMINISTIC_FAILURE,
        TaskTabCleanupEvent.RETRY,
        TaskTabCleanupEvent.UNCERTAIN_EXTERNAL_EFFECT,
    }
    task_events = {
        TaskTabCleanupEvent.TASK_COMPLETE,
        TaskTabCleanupEvent.CANCEL,
        TaskTabCleanupEvent.PAUSE,
        TaskTabCleanupEvent.APPLICATION_CLOSE,
        TaskTabCleanupEvent.APPLICATION_RESTART,
    }
    if event in tab_events and tab_id is None:
        raise TaskTabCleanupError("tab-scoped cleanup event requires tab_id")
    if event in task_events and tab_id is not None:
        raise TaskTabCleanupError("task-scoped cleanup event must not include tab_id")


def _validate_reconciliation(
    *,
    task_id: str,
    tab_id: str | None,
    owned_by_id: dict[str, TaskOwnedTab],
    uncertainty: bool,
    reconciliation: tuple[TaskTabReconciliationEvidence, ...],
) -> None:
    if uncertainty and not reconciliation:
        raise TaskTabCleanupBlockedError(
            "unresolved external effect requires durable reconciliation evidence"
        )
    if not uncertainty and reconciliation:
        raise TaskTabCleanupError("reconciliation evidence requires unresolved external effect")

    seen: set[str] = set()
    for item in reconciliation:
        if not isinstance(item, TaskTabReconciliationEvidence):
            raise TypeError("reconciliation entries must be TaskTabReconciliationEvidence")
        if item.task_id != task_id:
            raise TaskTabCleanupError("reconciliation evidence is bound to another task")
        if item.tab_id not in owned_by_id:
            raise TaskTabCleanupError("reconciliation evidence is bound to an unowned tab")
        if item.tab_id in seen:
            raise TaskTabCleanupError("duplicate reconciliation evidence for one task tab")
        seen.add(item.tab_id)

    if tab_id is not None and reconciliation and set(seen) != {tab_id}:
        raise TaskTabCleanupError("tab-scoped uncertainty must bind evidence to that exact tab")


def _result(
    *,
    tabs: TaskBrowserTabs,
    event: TaskTabCleanupEvent,
    action: TaskTabCleanupAction,
    task_id: str,
    tab_id: str | None,
    affected_tab_ids: tuple[str, ...] = (),
    reconciliation: tuple[TaskTabReconciliationEvidence, ...] = (),
) -> TaskTabCleanupResult:
    return TaskTabCleanupResult(
        event=event,
        action=action,
        task_id=task_id,
        tab_id=tab_id,
        affected_tab_ids=affected_tab_ids,
        task_tabs_snapshot=tabs.snapshot(),
        reconciliation=reconciliation,
    )


def _validate_nonempty(name: str, value: str, max_length: int) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if not value or value != value.strip() or len(value) > max_length:
        raise ValueError(f"{name} must be a non-empty bounded string without outer whitespace")
    if any(ord(character) < 32 for character in value):
        raise ValueError(f"{name} must not contain control characters")
