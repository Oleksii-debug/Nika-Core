from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from nika_core.interaction.domain import StaleSnapshotError
from nika_core.task_browser_tabs import (
    StaleTaskTabError,
    TaskBrowserTabs,
    TaskTabOwnershipError,
    TaskTabReopenDeniedError,
    TaskTabReopenPolicy,
)


@dataclass
class _FakePage:
    page_id: str
    url: str | None = None
    closed: bool = False
    focused: int = 0

    def goto(self, url: str, *, wait_until: str, timeout: float) -> None:
        assert wait_until == "domcontentloaded"
        assert timeout == 250
        self.url = url

    def bring_to_front(self) -> None:
        if self.closed:
            raise RuntimeError("closed")
        self.focused += 1

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


def test_open_returns_stable_task_scoped_identity_and_switches_owned_tab() -> None:
    session = _FakeSession("session-a")
    manager = TaskBrowserTabs(session=session)  # type: ignore[arg-type]

    tab = manager.open_tab(
        task_id="task-a",
        tab_id="tab-one",
        target_url="http://127.0.0.1/local/one",
    )

    assert (tab.task_id, tab.tab_id) == ("task-a", "tab-one")
    assert _page(session, "page-1").url == "http://127.0.0.1/local/one"
    assert manager.switch_to(task_id="task-a", tab_id="tab-one") == tab
    assert _page(session, "page-1").focused == 1


def test_wrong_task_cannot_switch_or_close_another_tasks_tab() -> None:
    session = _FakeSession("session-a")
    manager = TaskBrowserTabs(session=session)  # type: ignore[arg-type]
    manager.open_tab(
        task_id="task-a",
        tab_id="tab-one",
        target_url="http://127.0.0.1/local/one",
    )

    with pytest.raises(TaskTabOwnershipError):
        manager.switch_to(task_id="task-b", tab_id="tab-one")
    with pytest.raises(TaskTabOwnershipError):
        manager.close_tab(task_id="task-b", tab_id="tab-one")
    assert _page(session, "page-1").closed is False


def test_cleanup_closes_only_task_owned_pages_and_leaves_unrelated_page_alone() -> None:
    session = _FakeSession("session-a")
    foreign_page_id = session.new_page()
    manager = TaskBrowserTabs(session=session)  # type: ignore[arg-type]
    manager.open_tab(
        task_id="task-a",
        tab_id="tab-a",
        target_url="http://127.0.0.1/local/a",
    )
    manager.open_tab(
        task_id="task-b",
        tab_id="tab-b",
        target_url="http://127.0.0.1/local/b",
    )

    removed = manager.cleanup_task("task-a")

    assert tuple(tab.tab_id for tab in removed) == ("tab-a",)
    assert _page(session, foreign_page_id).closed is False
    assert _page(session, "page-2").closed is True
    assert _page(session, "page-3").closed is False
    assert manager.owned_tabs("task-a") == ()
    assert tuple(tab.tab_id for tab in manager.owned_tabs("task-b")) == ("tab-b",)


def test_restart_restores_logical_identity_but_never_runtime_page_handle() -> None:
    original_session = _FakeSession("session-a")
    original = TaskBrowserTabs(session=original_session)  # type: ignore[arg-type]
    original.open_tab(
        task_id="task-a",
        tab_id="stable-tab",
        target_url="http://127.0.0.1/local/reopen",
        reopen_policy=TaskTabReopenPolicy.SAME_TARGET,
    )
    encoded = json.dumps(original.snapshot(), ensure_ascii=False)
    assert "page-1" not in encoded
    assert "session-a" not in encoded

    restarted_session = _FakeSession("session-b")
    restarted = TaskBrowserTabs.from_snapshot(  # type: ignore[arg-type]
        session=restarted_session,
        payload=json.loads(encoded),
    )

    with pytest.raises(StaleTaskTabError):
        restarted.switch_to(task_id="task-a", tab_id="stable-tab")

    tab = restarted.switch_to(
        task_id="task-a",
        tab_id="stable-tab",
        reopen_if_stale=True,
    )
    assert (tab.task_id, tab.tab_id) == ("task-a", "stable-tab")
    assert _page(restarted_session, "page-1").url == "http://127.0.0.1/local/reopen"
    assert _page(restarted_session, "page-1").focused == 1


def test_restart_reopen_fails_closed_when_durable_policy_denies_it() -> None:
    original = TaskBrowserTabs(session=_FakeSession("session-a"))  # type: ignore[arg-type]
    original.open_tab(
        task_id="task-a",
        tab_id="never-reopen",
        target_url="http://127.0.0.1/local/private-step",
        reopen_policy=TaskTabReopenPolicy.NEVER,
    )
    snapshot = json.loads(json.dumps(original.snapshot()))
    assert snapshot["tabs"][0]["reopen_url"] is None

    restarted_session = _FakeSession("session-b")
    restarted = TaskBrowserTabs.from_snapshot(  # type: ignore[arg-type]
        session=restarted_session,
        payload=snapshot,
    )
    with pytest.raises(TaskTabReopenDeniedError):
        restarted.switch_to(
            task_id="task-a",
            tab_id="never-reopen",
            reopen_if_stale=True,
        )
    assert restarted_session.registry.pages == {}


def test_closed_runtime_page_is_stale_and_reopens_only_when_allowed() -> None:
    session = _FakeSession("session-a")
    manager = TaskBrowserTabs(session=session)  # type: ignore[arg-type]
    manager.open_tab(
        task_id="task-a",
        tab_id="recoverable",
        target_url="http://127.0.0.1/local/recoverable",
        reopen_policy=TaskTabReopenPolicy.SAME_TARGET,
    )
    _page(session, "page-1").close()

    with pytest.raises(StaleTaskTabError):
        manager.switch_to(task_id="task-a", tab_id="recoverable")

    manager.switch_to(task_id="task-a", tab_id="recoverable", reopen_if_stale=True)
    assert _page(session, "page-2").url == "http://127.0.0.1/local/recoverable"
    assert _page(session, "page-2").focused == 1


def test_navigation_contract_rejects_coordinate_like_and_unsafe_url_bypasses() -> None:
    session = _FakeSession("session-a")
    manager = TaskBrowserTabs(session=session)  # type: ignore[arg-type]

    with pytest.raises(ValueError):
        manager.open_tab(task_id="task-a", target_url="file:///C:/Users/test/secret.txt")
    with pytest.raises(ValueError):
        manager.open_tab(task_id="task-a", target_url="javascript:alert(1)")
    with pytest.raises(ValueError):
        manager.open_tab(task_id="task-a", target_url="https://user:secret@example.test/")
    assert not hasattr(manager, "click_at")
    assert not hasattr(manager, "move_mouse")


def test_snapshot_rejects_duplicate_or_malformed_task_tab_identity() -> None:
    session = _FakeSession("session-b")
    duplicate = {
        "schema_version": 1,
        "tabs": [
            {
                "task_id": "task-a",
                "tab_id": "tab-x",
                "reopen_policy": "never",
                "reopen_url": None,
            },
            {
                "task_id": "task-b",
                "tab_id": "tab-x",
                "reopen_policy": "never",
                "reopen_url": None,
            },
        ],
    }
    with pytest.raises(ValueError):
        TaskBrowserTabs.from_snapshot(session=session, payload=duplicate)  # type: ignore[arg-type]

    malformed = {"schema_version": 1, "tabs": [{"task_id": "task-a"}]}
    with pytest.raises(ValueError):
        TaskBrowserTabs.from_snapshot(session=session, payload=malformed)  # type: ignore[arg-type]
