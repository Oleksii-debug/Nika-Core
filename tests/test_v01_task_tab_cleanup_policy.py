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
class _FakePage:
    page_id: str
    url: str | None = None
    closed: bool = False

    def goto(self, url: str, *, wait_until: str, timeout: float) -> None:
        assert wait_until == "domcontentloaded"
        assert timeout == 250
        self.url = url

    def bring_to_front(self) -> None:
        if self.closed:
            raise RuntimeError("closed")

    def close(self) -> None:
        self.closed = True

    def is_closed(self) -> bool:
        return self.closed


@dataclass
class _FakePageRecord:
    page: _FakePage


class _FakeRegistry:
    def __init__(self) -> None:
        self.pages: dict[str, _FakePageRecord] = {}

    def get(self, page_id: str) -> _FakePageRecord:
        record = self.pages.get(page_id)
        if record is None or record.page.closed:
            raise StaleSnapshotError("stale")
        return record


class _FakeSession:
    timeout_ms = 250

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.registry = _FakeRegistry()
        self._counter = 0

    def new_page(self) -> str:
        self._counter += 1
        page_id = f"page-{self._counter}"
        self.registry.pages[page_id] = _FakePageRecord(_FakePage(page_id))
        return page_id


def _page(session: _FakeSession, page_id: str) -> _FakePage:
    return session.registry.pages[page_id].page


def _manager_with_isolation_fixture() -> tuple[_FakeSession, TaskBrowserTabs, str]:
    session = _FakeSession("session-a")
    foreign_page_id = session.new_page()
    manager = TaskBrowserTabs(session=session)  # type: ignore[arg-type]
    manager.open_tab(
        task_id="task-a",
        tab_id="tab-a1",
        target_url="http://127.0.0.1/local/a1",
        reopen_policy=TaskTabReopenPolicy.SAME_TARGET,
    )
    manager.open_tab(
        task_id="task-a",
        tab_id="tab-a2",
        target_url="http://127.0.0.1/local/a2",
        reopen_policy=TaskTabReopenPolicy.SAME_TARGET,
    )
    manager.open_tab(
        task_id="task-b",
        tab_id="tab-b1",
        target_url="http://127.0.0.1/local/b1",
        reopen_policy=TaskTabReopenPolicy.SAME_TARGET,
    )
    return session, manager, foreign_page_id


@pytest.mark.parametrize(
    "event",
    [
        TaskTabCleanupEvent.CONFIRMED_SUCCESS,
        TaskTabCleanupEvent.TERMINAL_DETERMINISTIC_FAILURE,
    ],
)
def test_terminal_tab_outcome_closes_only_exact_tab(event: TaskTabCleanupEvent) -> None:
    session, manager, foreign_page_id = _manager_with_isolation_fixture()

    result = apply_task_tab_cleanup(
        manager,
        event=event,
        task_id="task-a",
        tab_id="tab-a1",
    )

    assert result.action is TaskTabCleanupAction.CLOSE_TAB
    assert result.affected_tab_ids == ("tab-a1",)
    assert _page(session, "page-2").closed is True
    assert _page(session, "page-3").closed is False
    assert _page(session, "page-4").closed is False
    assert _page(session, foreign_page_id).closed is False
    assert tuple(tab.tab_id for tab in manager.owned_tabs("task-a")) == ("tab-a2",)
    assert tuple(tab.tab_id for tab in manager.owned_tabs("task-b")) == ("tab-b1",)


@pytest.mark.parametrize(
    "event",
    [TaskTabCleanupEvent.TASK_COMPLETE, TaskTabCleanupEvent.CANCEL],
)
def test_safe_task_terminal_cleanup_never_crosses_task_boundary(event: TaskTabCleanupEvent) -> None:
    session, manager, foreign_page_id = _manager_with_isolation_fixture()

    result = apply_task_tab_cleanup(manager, event=event, task_id="task-a")

    assert result.action is TaskTabCleanupAction.CLEANUP_TASK
    assert result.affected_tab_ids == ("tab-a1", "tab-a2")
    assert _page(session, "page-2").closed is True
    assert _page(session, "page-3").closed is True
    assert _page(session, "page-4").closed is False
    assert _page(session, foreign_page_id).closed is False
    assert manager.owned_tabs("task-a") == ()
    assert tuple(tab.tab_id for tab in manager.owned_tabs("task-b")) == ("tab-b1",)


def test_cancel_with_unresolved_effect_preserves_task_for_reconciliation() -> None:
    session, manager, foreign_page_id = _manager_with_isolation_fixture()
    evidence = (
        TaskTabReconciliationEvidence(
            task_id="task-a",
            tab_id="tab-a1",
            effect_ref="tool:effect-a1",
        ),
    )

    result = apply_task_tab_cleanup(
        manager,
        event=TaskTabCleanupEvent.CANCEL,
        task_id="task-a",
        unresolved_external_effect=True,
        reconciliation=evidence,
    )

    assert result.action is TaskTabCleanupAction.PRESERVE_FOR_RECONCILIATION
    assert result.affected_tab_ids == ()
    assert tuple(tab.tab_id for tab in manager.owned_tabs("task-a")) == ("tab-a1", "tab-a2")
    assert _page(session, "page-2").closed is False
    assert _page(session, "page-3").closed is False
    assert _page(session, "page-4").closed is False
    assert _page(session, foreign_page_id).closed is False


def test_task_complete_cannot_destroy_unresolved_effect_without_durable_reference() -> None:
    _session, manager, _foreign_page_id = _manager_with_isolation_fixture()

    with pytest.raises(TaskTabCleanupBlockedError):
        apply_task_tab_cleanup(
            manager,
            event=TaskTabCleanupEvent.TASK_COMPLETE,
            task_id="task-a",
            unresolved_external_effect=True,
        )

    assert tuple(tab.tab_id for tab in manager.owned_tabs("task-a")) == ("tab-a1", "tab-a2")
    assert tuple(tab.tab_id for tab in manager.owned_tabs("task-b")) == ("tab-b1",)


@pytest.mark.parametrize(
    ("event", "tab_id"),
    [
        (TaskTabCleanupEvent.PAUSE, None),
        (TaskTabCleanupEvent.RETRY, "tab-a1"),
    ],
)
def test_pause_and_retry_preserve_logical_tabs(
    event: TaskTabCleanupEvent,
    tab_id: str | None,
) -> None:
    session, manager, foreign_page_id = _manager_with_isolation_fixture()

    result = apply_task_tab_cleanup(
        manager,
        event=event,
        task_id="task-a",
        tab_id=tab_id,
    )

    assert result.action is TaskTabCleanupAction.PRESERVE
    assert tuple(tab.tab_id for tab in manager.owned_tabs("task-a")) == ("tab-a1", "tab-a2")
    assert tuple(tab.tab_id for tab in manager.owned_tabs("task-b")) == ("tab-b1",)
    assert all(not record.page.closed for record in session.registry.pages.values())
    assert _page(session, foreign_page_id).closed is False


def test_uncertain_external_effect_keeps_tab_and_serializes_only_safe_logical_metadata() -> None:
    session, manager, foreign_page_id = _manager_with_isolation_fixture()
    evidence = (
        TaskTabReconciliationEvidence(
            task_id="task-a",
            tab_id="tab-a1",
            effect_ref="tool:9f42d1",
        ),
    )

    result = apply_task_tab_cleanup(
        manager,
        event=TaskTabCleanupEvent.UNCERTAIN_EXTERNAL_EFFECT,
        task_id="task-a",
        tab_id="tab-a1",
        reconciliation=evidence,
    )
    encoded = json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True)

    assert result.action is TaskTabCleanupAction.PRESERVE_FOR_RECONCILIATION
    assert "tool:9f42d1" in encoded
    assert "tab-a1" in encoded
    assert "session-a" not in encoded
    assert "page-2" not in encoded
    assert "page-3" not in encoded
    assert "page-4" not in encoded
    assert _page(session, "page-2").closed is False
    assert _page(session, "page-4").closed is False
    assert _page(session, foreign_page_id).closed is False


def test_application_close_and_restart_preserve_logical_identity_without_runtime_handles() -> None:
    original_session, original, foreign_page_id = _manager_with_isolation_fixture()
    evidence = (
        TaskTabReconciliationEvidence(
            task_id="task-a",
            tab_id="tab-a1",
            effect_ref="tool:effect-a1",
        ),
    )

    closing = apply_task_tab_cleanup(
        original,
        event=TaskTabCleanupEvent.APPLICATION_CLOSE,
        task_id="task-a",
        unresolved_external_effect=True,
        reconciliation=evidence,
    )
    encoded = json.dumps(closing.to_dict(), ensure_ascii=False, sort_keys=True)

    assert closing.action is TaskTabCleanupAction.CHECKPOINT_ONLY
    assert "session-a" not in encoded
    assert "page-2" not in encoded
    assert _page(original_session, foreign_page_id).closed is False

    restarted_session = _FakeSession("session-b")
    restarted = TaskBrowserTabs.from_snapshot(  # type: ignore[arg-type]
        session=restarted_session,
        payload=closing.task_tabs_snapshot,
    )
    restarted_result = apply_task_tab_cleanup(
        restarted,
        event=TaskTabCleanupEvent.APPLICATION_RESTART,
        task_id="task-a",
        unresolved_external_effect=True,
        reconciliation=evidence,
    )

    assert restarted_result.action is TaskTabCleanupAction.PRESERVE_FOR_RECONCILIATION
    assert tuple(tab.tab_id for tab in restarted.owned_tabs("task-a")) == ("tab-a1", "tab-a2")
    assert tuple(tab.tab_id for tab in restarted.owned_tabs("task-b")) == ("tab-b1",)
    assert restarted_session.registry.pages == {}
    with pytest.raises(StaleTaskTabError):
        restarted.switch_to(task_id="task-a", tab_id="tab-a1")


def test_reconciliation_evidence_cannot_be_rebound_across_tasks_or_tabs() -> None:
    _session, manager, _foreign_page_id = _manager_with_isolation_fixture()

    with pytest.raises(TaskTabCleanupError):
        apply_task_tab_cleanup(
            manager,
            event=TaskTabCleanupEvent.UNCERTAIN_EXTERNAL_EFFECT,
            task_id="task-a",
            tab_id="tab-a1",
            reconciliation=(
                TaskTabReconciliationEvidence(
                    task_id="task-b",
                    tab_id="tab-b1",
                    effect_ref="tool:wrong-task",
                ),
            ),
        )

    with pytest.raises(TaskTabCleanupError):
        apply_task_tab_cleanup(
            manager,
            event=TaskTabCleanupEvent.UNCERTAIN_EXTERNAL_EFFECT,
            task_id="task-a",
            tab_id="tab-a1",
            reconciliation=(
                TaskTabReconciliationEvidence(
                    task_id="task-a",
                    tab_id="tab-a2",
                    effect_ref="tool:wrong-tab",
                ),
            ),
        )

    assert tuple(tab.tab_id for tab in manager.owned_tabs("task-a")) == ("tab-a1", "tab-a2")
    assert tuple(tab.tab_id for tab in manager.owned_tabs("task-b")) == ("tab-b1",)
