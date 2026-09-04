"""Task-scoped browser-tab ownership above the existing Playwright session.

The durable contract contains only Nika logical tab identity and explicit reopen policy. Runtime
Playwright page ids remain process-local bindings and are never trusted after restore.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit

from .interaction.domain import StaleSnapshotError
from .interaction.playwright_adapter import BrowserSession

_SNAPSHOT_SCHEMA = 1
_ALLOWED_URL_SCHEMES = frozenset({"http", "https"})


class TaskBrowserTabError(RuntimeError):
    """Base error for task-owned browser-tab control."""


class TaskTabOwnershipError(TaskBrowserTabError):
    """The requested logical tab is not owned by the supplied task."""


class StaleTaskTabError(TaskBrowserTabError):
    """The logical tab has no trustworthy page binding in the current browser session."""


class TaskTabReopenDeniedError(TaskBrowserTabError):
    """Reconstructing a stale logical tab is not allowed by its durable policy."""


class TaskTabNavigationError(TaskBrowserTabError):
    """A newly created task tab could not reach its declared target."""


class TaskTabReopenPolicy(StrEnum):
    """Durable policy for reconstructing a logical tab after runtime loss."""

    NEVER = "never"
    SAME_TARGET = "same_target"


@dataclass(frozen=True, slots=True)
class TaskOwnedTab:
    """Durable Nika identity for one browser tab owned by one task."""

    task_id: str
    tab_id: str
    reopen_policy: TaskTabReopenPolicy
    reopen_url: str | None = None

    def __post_init__(self) -> None:
        _validate_identity("task_id", self.task_id)
        _validate_identity("tab_id", self.tab_id)
        if not isinstance(self.reopen_policy, TaskTabReopenPolicy):
            raise TypeError("reopen policy must be a TaskTabReopenPolicy")
        if self.reopen_policy is TaskTabReopenPolicy.NEVER:
            if self.reopen_url is not None:
                raise ValueError("NEVER reopen policy must not persist a reopen URL")
            return
        if self.reopen_url is None:
            raise ValueError("reopen URL is required when reopen policy permits reconstruction")
        _validate_navigation_url(self.reopen_url)

    def to_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "tab_id": self.tab_id,
            "reopen_policy": self.reopen_policy.value,
            "reopen_url": self.reopen_url,
        }

    @classmethod
    def from_dict(cls, payload: object) -> TaskOwnedTab:
        if not isinstance(payload, dict):
            raise TypeError("task-tab record must be an object")
        expected = {"task_id", "tab_id", "reopen_policy", "reopen_url"}
        if set(payload) != expected:
            raise ValueError("task-tab record fields do not match schema")
        task_id = payload["task_id"]
        tab_id = payload["tab_id"]
        policy = payload["reopen_policy"]
        reopen_url = payload["reopen_url"]
        if not isinstance(task_id, str) or not isinstance(tab_id, str):
            raise TypeError("task-tab identities must be strings")
        if not isinstance(policy, str):
            raise TypeError("task-tab reopen policy must be a string")
        if reopen_url is not None and not isinstance(reopen_url, str):
            raise TypeError("task-tab reopen URL must be a string or null")
        try:
            parsed_policy = TaskTabReopenPolicy(policy)
        except ValueError as exc:
            raise ValueError("unsupported task-tab reopen policy") from exc
        return cls(
            task_id=task_id,
            tab_id=tab_id,
            reopen_policy=parsed_policy,
            reopen_url=reopen_url,
        )


@dataclass(frozen=True, slots=True)
class _RuntimeBinding:
    session_id: str
    page_id: str


@dataclass(slots=True)
class TaskBrowserTabs:
    """Own and control only pages explicitly created for Nika tasks."""

    session: BrowserSession
    _tabs: dict[tuple[str, str], TaskOwnedTab] = field(default_factory=dict, init=False)
    _bindings: dict[tuple[str, str], _RuntimeBinding] = field(default_factory=dict, init=False)

    def open_tab(
        self,
        *,
        task_id: str,
        target_url: str,
        tab_id: str | None = None,
        reopen_policy: TaskTabReopenPolicy = TaskTabReopenPolicy.NEVER,
    ) -> TaskOwnedTab:
        """Open a new Nika-owned page and return its stable task-scoped logical identity."""

        _validate_identity("task_id", task_id)
        _validate_navigation_url(target_url)
        logical_tab_id = uuid.uuid4().hex if tab_id is None else tab_id
        _validate_identity("tab_id", logical_tab_id)
        key = (task_id, logical_tab_id)
        if key in self._tabs:
            raise TaskBrowserTabError("task tab already exists")
        if any(existing.tab_id == logical_tab_id for existing in self._tabs.values()):
            raise TaskBrowserTabError("tab_id is already used by another task")

        record = TaskOwnedTab(
            task_id=task_id,
            tab_id=logical_tab_id,
            reopen_policy=reopen_policy,
            reopen_url=(target_url if reopen_policy is TaskTabReopenPolicy.SAME_TARGET else None),
        )
        self._open_runtime_binding(record, target_url)
        self._tabs[key] = record
        return record

    def switch_to(
        self,
        *,
        task_id: str,
        tab_id: str,
        reopen_if_stale: bool = False,
    ) -> TaskOwnedTab:
        """Focus one task-owned tab, optionally reconstructing it under durable policy."""

        record = self._require_owned(task_id, tab_id)
        try:
            page = self._bound_page(record)
        except StaleTaskTabError:
            if not reopen_if_stale:
                raise
            if record.reopen_policy is not TaskTabReopenPolicy.SAME_TARGET:
                raise TaskTabReopenDeniedError("task-tab reopen policy denies reconstruction")
            if record.reopen_url is None:  # defensive against corrupted in-memory construction
                raise TaskTabReopenDeniedError("task-tab reopen target is unavailable")
            self._open_runtime_binding(record, record.reopen_url)
            page = self._bound_page(record)
        page.bring_to_front()
        return record

    def close_tab(self, *, task_id: str, tab_id: str) -> TaskOwnedTab:
        """Close only the page bound to this task-owned logical tab."""

        record = self._require_owned(task_id, tab_id)
        key = (task_id, tab_id)
        binding = self._bindings.get(key)
        if binding is not None and binding.session_id == self.session.session_id:
            try:
                page = self._page_for_id(binding.page_id)
            except StaleTaskTabError:
                self._bindings.pop(key, None)
            else:
                try:
                    page.close()
                except Exception as exc:
                    raise TaskBrowserTabError("failed to close task-owned browser tab") from exc
        self._bindings.pop(key, None)
        self._tabs.pop(key, None)
        return record

    def cleanup_task(self, task_id: str) -> tuple[TaskOwnedTab, ...]:
        """Close/remove exactly the logical tabs owned by ``task_id`` and nothing else."""

        _validate_identity("task_id", task_id)
        owned = tuple(tab for tab in self._tabs.values() if tab.task_id == task_id)
        for record in owned:
            self.close_tab(task_id=record.task_id, tab_id=record.tab_id)
        return owned

    def owned_tabs(self, task_id: str) -> tuple[TaskOwnedTab, ...]:
        _validate_identity("task_id", task_id)
        return tuple(tab for tab in self._tabs.values() if tab.task_id == task_id)

    def snapshot(self) -> dict[str, object]:
        """Return JSON-safe durable state without Playwright page/session handles."""

        return {
            "schema_version": _SNAPSHOT_SCHEMA,
            "tabs": [tab.to_dict() for tab in self._tabs.values()],
        }

    @classmethod
    def from_snapshot(cls, *, session: BrowserSession, payload: object) -> TaskBrowserTabs:
        """Restore logical ownership only; all runtime page bindings intentionally start stale."""

        if not isinstance(payload, dict):
            raise TypeError("task-browser snapshot must be an object")
        if set(payload) != {"schema_version", "tabs"}:
            raise ValueError("task-browser snapshot fields do not match schema")
        schema_version = payload["schema_version"]
        if not isinstance(schema_version, int) or isinstance(schema_version, bool):
            raise TypeError("task-browser snapshot schema version must be an integer")
        if schema_version != _SNAPSHOT_SCHEMA:
            raise ValueError("unsupported task-browser snapshot schema")
        raw_tabs = payload["tabs"]
        if not isinstance(raw_tabs, list):
            raise TypeError("task-browser snapshot tabs must be a list")
        manager = cls(session=session)
        seen_tab_ids: set[str] = set()
        for raw_tab in raw_tabs:
            record = TaskOwnedTab.from_dict(raw_tab)
            key = (record.task_id, record.tab_id)
            if key in manager._tabs or record.tab_id in seen_tab_ids:
                raise ValueError("duplicate task-tab identity in snapshot")
            manager._tabs[key] = record
            seen_tab_ids.add(record.tab_id)
        return manager

    def _require_owned(self, task_id: str, tab_id: str) -> TaskOwnedTab:
        _validate_identity("task_id", task_id)
        _validate_identity("tab_id", tab_id)
        record = self._tabs.get((task_id, tab_id))
        if record is None:
            raise TaskTabOwnershipError("browser tab is not owned by the supplied task")
        return record

    def _open_runtime_binding(self, record: TaskOwnedTab, target_url: str) -> None:
        page_id = self.session.new_page()
        try:
            page = self._page_for_id(page_id)
        except StaleTaskTabError as exc:
            raise TaskTabNavigationError("new task-owned browser page was not registered") from exc
        try:
            page.goto(
                target_url,
                wait_until="domcontentloaded",
                timeout=self.session.timeout_ms,
            )
        except Exception as exc:
            page.close()
            raise TaskTabNavigationError("task-owned browser tab navigation failed") from exc
        self._bindings[(record.task_id, record.tab_id)] = _RuntimeBinding(
            session_id=self.session.session_id,
            page_id=page_id,
        )

    def _bound_page(self, record: TaskOwnedTab) -> Any:
        key = (record.task_id, record.tab_id)
        binding = self._bindings.get(key)
        if binding is None or binding.session_id != self.session.session_id:
            raise StaleTaskTabError("task-owned browser tab has no current runtime binding")
        try:
            return self._page_for_id(binding.page_id)
        except StaleTaskTabError:
            self._bindings.pop(key, None)
            raise

    def _page_for_id(self, page_id: str) -> Any:
        registry = self.session.registry
        if registry is None:
            raise StaleTaskTabError("browser session is not active")
        try:
            return registry.get(page_id).page
        except StaleSnapshotError as exc:
            raise StaleTaskTabError("task-owned browser page binding is stale") from exc


def _validate_identity(name: str, value: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if not value or value != value.strip() or len(value) > 256:
        raise ValueError(f"{name} must be a non-empty bounded string without outer whitespace")


def _validate_navigation_url(value: str) -> None:
    if not isinstance(value, str):
        raise TypeError("browser target URL must be a string")
    if not value or value != value.strip():
        raise ValueError("browser target URL must be a non-empty string without outer whitespace")
    if any(ord(character) < 32 for character in value):
        raise ValueError("browser target URL must not contain control characters")
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise ValueError("browser target URL is invalid") from exc
    if parsed.scheme.casefold() not in _ALLOWED_URL_SCHEMES or not parsed.netloc:
        raise ValueError("browser target URL must use http or https with an explicit host")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("browser target URL must not contain userinfo credentials")
