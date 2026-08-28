from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from nika_core.interaction.domain import StaleSnapshotError
from nika_core.task_browser_tabs import StaleTaskTabError, TaskBrowserTabs, TaskTabReopenPolicy
from nika_core.task_tab_cleanup import (
    TaskTabCleanupAction,
    TaskTabCleanupBlockedError,
    TaskTabCleanupError,
    TaskTabCleanupEvent,
    TaskTabReconciliationEvidence,
    apply_task_tab_cleanup,
)


@dataclass
class _Page:
    page_id: str
    closed: bool = False

    def goto(self, _url: str, *, wait_until: str, timeout: float) -> None:
        assert wait_until == "domcontentloaded"
        assert timeout == 250

    def bring_to_front(self) -> None:
        if self.closed:
            raise RuntimeError("closed")

    def close(self) -> None:
        self.closed = True

    def is_closed(self) -> bool:
        return self.closed


@dataclass
class _PageRecord:
    page: _Page


class _Registry:
    def __init__(self) -> None:
        self.pages: dict[str, _PageRecord] = {}

    def get(self, page_id: str) -> _PageRecord:
        record = self.pages.get(page_id)
        if record is None or record.page.closed:
            raise StaleSnapshotError("stale")
        return record


class _Session:
    timeout_ms = 250

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.registry = _Registry()
        self._counter = 0

    def new_page(self) -> str:
        self._counter += 1
        page_id = f"page-{self._counter}"
        self.registry.pages[page_id] = _PageRecord(_Page(page_id))
        return page_id


def _fixture() -> tuple[_Session, TaskBrowserTabs, str]:
    session = _Session("session-a")
    foreign = session.new_page()
    tabs = TaskBrowserTabs(session=session)  # type: ignore[arg-type]
    for task_id, tab_id in (("task-a", "a1"), ("task-a", "a2"), ("task-b", "b1")):
        tabs.open_tab(
            task_id=task_id,
            tab_id=tab_id,
            target_url=f"http://127.0.0.1/{tab_id}",
            reopen_policy=TaskTabReopenPolicy.SAME_TARGET,
        )
    return session, tabs, foreign


def _ids(tabs: TaskBrowserTabs, task_id: str) -> tuple[str, ...]:
    return tuple(tab.tab_id for tab in tabs.owned_tabs(task_id))


def _reconciliation(tab_id: str = "a1") -> tuple[TaskTabReconciliationEvidence, ...]:
    return (
        TaskTabReconciliationEvidence(
            task_id="task-a",
            tab_id=tab_id,
            effect_ref=f"tool:effect-{tab_id}",
        ),
    )


@pytest.mark.parametrize(
    "event",
    [
        TaskTabCleanupEvent.CONFIRMED_SUCCESS,
        TaskTabCleanupEvent.TERMINAL_DETERMINISTIC_FAILURE,
    ],
)
def test_terminal_tab_outcomes_close_only_exact_owned_tab(event: TaskTabCleanupEvent) -> None:
    session, tabs, foreign = _fixture()

    result = apply_task_tab_cleanup(tabs, event=event, task_id="task-a", tab_id="a1")

    assert result.action is TaskTabCleanupAction.CLOSE_TAB
    assert _ids(tabs, "task-a") == ("a2",)
    assert _ids(tabs, "task-b") == ("b1",)
    assert session.registry.pages[foreign].page.closed is False
    assert session.registry.pages["page-4"].page.closed is False


@pytest.mark.parametrize("event", [TaskTabCleanupEvent.TASK_COMPLETE, TaskTabCleanupEvent.CANCEL])
def test_safe_task_terminal_cleanup_is_cross_task_isolated(event: TaskTabCleanupEvent) -> None:
    session, tabs, foreign = _fixture()

    result = apply_task_tab_cleanup(
        tabs,
        event=event,
        task_id="task-a",
        unresolved_external_effect=False,
    )

    assert result.action is TaskTabCleanupAction.CLEANUP_TASK
    assert result.affected_tab_ids == ("a1", "a2")
    assert _ids(tabs, "task-a") == ()
    assert _ids(tabs, "task-b") == ("b1",)
    assert session.registry.pages[foreign].page.closed is False
    assert session.registry.pages["page-4"].page.closed is False


def test_task_terminal_cleanup_fails_closed_without_explicit_effect_clearance() -> None:
    _session, tabs, _foreign = _fixture()

    with pytest.raises(TaskTabCleanupBlockedError):
        apply_task_tab_cleanup(tabs, event=TaskTabCleanupEvent.CANCEL, task_id="task-a")

    assert _ids(tabs, "task-a") == ("a1", "a2")
    assert _ids(tabs, "task-b") == ("b1",)


@pytest.mark.parametrize("event", [TaskTabCleanupEvent.TASK_COMPLETE, TaskTabCleanupEvent.CANCEL])
def test_unresolved_task_terminal_state_preserves_tabs_for_reconciliation(
    event: TaskTabCleanupEvent,
) -> None:
    _session, tabs, _foreign = _fixture()

    result = apply_task_tab_cleanup(
        tabs,
        event=event,
        task_id="task-a",
        unresolved_external_effect=True,
        reconciliation=_reconciliation(),
    )

    assert result.action is TaskTabCleanupAction.PRESERVE_FOR_RECONCILIATION
    assert _ids(tabs, "task-a") == ("a1", "a2")
    assert _ids(tabs, "task-b") == ("b1",)


def test_unresolved_cleanup_without_durable_reference_is_blocked() -> None:
    _session, tabs, _foreign = _fixture()
    with pytest.raises(TaskTabCleanupBlockedError):
        apply_task_tab_cleanup(
            tabs,
            event=TaskTabCleanupEvent.TASK_COMPLETE,
            task_id="task-a",
            unresolved_external_effect=True,
        )


@pytest.mark.parametrize(
    ("event", "tab_id"),
    [(TaskTabCleanupEvent.PAUSE, None), (TaskTabCleanupEvent.RETRY, "a1")],
)
def test_pause_and_retry_preserve_task_owned_tabs(
    event: TaskTabCleanupEvent,
    tab_id: str | None,
) -> None:
    session, tabs, foreign = _fixture()

    result = apply_task_tab_cleanup(tabs, event=event, task_id="task-a", tab_id=tab_id)

    assert result.action is TaskTabCleanupAction.PRESERVE
    assert _ids(tabs, "task-a") == ("a1", "a2")
    assert _ids(tabs, "task-b") == ("b1",)
    assert all(not record.page.closed for record in session.registry.pages.values())
    assert session.registry.pages[foreign].page.closed is False


def test_uncertain_effect_preserves_bound_logical_evidence_without_runtime_handles() -> None:
    _session, tabs, _foreign = _fixture()

    result = apply_task_tab_cleanup(
        tabs,
        event=TaskTabCleanupEvent.UNCERTAIN_EXTERNAL_EFFECT,
        task_id="task-a",
        tab_id="a1",
        reconciliation=_reconciliation(),
    )
    encoded = json.dumps(result.to_dict(), sort_keys=True)

    assert result.action is TaskTabCleanupAction.PRESERVE_FOR_RECONCILIATION
    assert "tool:effect-a1" in encoded
    assert "session-a" not in encoded
    assert "page-2" not in encoded
    assert _ids(tabs, "task-a") == ("a1", "a2")


def test_reconciliation_evidence_cannot_cross_task_or_tab_identity() -> None:
    _session, tabs, _foreign = _fixture()
    wrong_task = (
        TaskTabReconciliationEvidence(task_id="task-b", tab_id="b1", effect_ref="tool:x"),
    )
    with pytest.raises(TaskTabCleanupError):
        apply_task_tab_cleanup(
            tabs,
            event=TaskTabCleanupEvent.UNCERTAIN_EXTERNAL_EFFECT,
            task_id="task-a",
            tab_id="a1",
            reconciliation=wrong_task,
        )
    with pytest.raises(TaskTabCleanupError):
        apply_task_tab_cleanup(
            tabs,
            event=TaskTabCleanupEvent.UNCERTAIN_EXTERNAL_EFFECT,
            task_id="task-a",
            tab_id="a1",
            reconciliation=_reconciliation("a2"),
        )


def test_application_close_restart_keeps_identity_but_not_runtime_page_handles() -> None:
    _session, tabs, _foreign = _fixture()
    evidence = _reconciliation()

    closing = apply_task_tab_cleanup(
        tabs,
        event=TaskTabCleanupEvent.APPLICATION_CLOSE,
        task_id="task-a",
        unresolved_external_effect=True,
        reconciliation=evidence,
    )
    encoded = json.dumps(closing.to_dict(), sort_keys=True)
    assert closing.action is TaskTabCleanupAction.CHECKPOINT_ONLY
    assert "session-a" not in encoded
    assert "page-2" not in encoded

    session_b = _Session("session-b")
    restored = TaskBrowserTabs.from_snapshot(  # type: ignore[arg-type]
        session=session_b,
        payload=closing.task_tabs_snapshot,
    )
    restarted = apply_task_tab_cleanup(
        restored,
        event=TaskTabCleanupEvent.APPLICATION_RESTART,
        task_id="task-a",
        unresolved_external_effect=True,
        reconciliation=evidence,
    )

    assert restarted.action is TaskTabCleanupAction.PRESERVE_FOR_RECONCILIATION
    assert _ids(restored, "task-a") == ("a1", "a2")
    assert _ids(restored, "task-b") == ("b1",)
    assert session_b.registry.pages == {}
    with pytest.raises(StaleTaskTabError):
        restored.switch_to(task_id="task-a", tab_id="a1")
