"""Strict semantic Windows UI Automation adapter.

RuntimeId plus a Nika lifetime generation form control identity. UIA
``CompareElements`` is the only basis for collapsing duplicate wrappers. Name,
position, z-order, bounds, DPI and coordinates never select a target.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from dataclasses import dataclass, replace
from typing import Protocol

from .domain import (
    AmbiguousTargetError,
    ApplicationIdentity,
    ControlNode,
    InteractionAction,
    InteractionTarget,
    SemanticSnapshot,
    StaleSnapshotError,
    TargetNotFoundError,
    UnsupportedInteractionError,
    WindowIdentity,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class UIAControlRecord:
    runtime_id: tuple[int, ...] | None
    automation_id: str
    role: str
    name: str
    enabled: bool
    visible: bool
    focused: bool
    value: str | None
    bounds: tuple[int, int, int, int] | None
    class_name: str = ""
    framework_id: str = ""
    patterns: tuple[str, ...] = ()
    element_generation: int = 1


@dataclass(frozen=True, slots=True)
class UIAWindowRecord:
    hwnd: int
    pid: int
    title: str
    enabled: bool = True


class WindowsUIABackend(Protocol):
    def process_started_ns(self, pid: int) -> int: ...

    def executable(self, pid: int) -> str: ...

    def enumerate_windows(self, pid: int) -> tuple[UIAWindowRecord, ...]: ...

    def enumerate_controls(
        self,
        hwnd: int,
        view: str,
    ) -> tuple[UIAControlRecord, ...]: ...

    def focused_identity(self, hwnd: int) -> tuple[tuple[int, ...], int] | None: ...

    def focus(
        self,
        hwnd: int,
        runtime_id: tuple[int, ...],
        generation: int,
    ) -> None: ...

    def invoke(
        self,
        hwnd: int,
        runtime_id: tuple[int, ...],
        generation: int,
    ) -> None: ...

    def set_value(
        self,
        hwnd: int,
        runtime_id: tuple[int, ...],
        generation: int,
        value: str,
    ) -> None: ...

    def select(
        self,
        hwnd: int,
        runtime_id: tuple[int, ...],
        generation: int,
    ) -> None: ...

    def toggle(
        self,
        hwnd: int,
        runtime_id: tuple[int, ...],
        generation: int,
    ) -> None: ...

    def expand(
        self,
        hwnd: int,
        runtime_id: tuple[int, ...],
        generation: int,
    ) -> None: ...

    def collapse(
        self,
        hwnd: int,
        runtime_id: tuple[int, ...],
        generation: int,
    ) -> None: ...


@dataclass(slots=True)
class _TrackedElement:
    generation: int
    wrapper: object
    present: bool


class PywinautoUIABackend:
    """Thin pywinauto backend with strict AutomationElement identity tracking."""

    def __init__(self) -> None:
        self._tracked: dict[
            int,
            dict[tuple[int, ...], list[_TrackedElement]],
        ] = {}
        self._last_duplicate_runtime_ids: tuple[tuple[int, ...], ...] = ()
        self._last_unaddressable_count = 0

    @property
    def last_duplicate_runtime_ids(self) -> tuple[tuple[int, ...], ...]:
        """RuntimeIds that represented multiple distinct elements last observation."""

        return self._last_duplicate_runtime_ids

    @property
    def last_unaddressable_count(self) -> int:
        """UIA provider elements omitted because they exposed no usable RuntimeId."""

        return self._last_unaddressable_count

    def _desktop(self):
        try:
            from pywinauto import Desktop
        except ImportError as exc:  # pragma: no cover - optional Windows dependency
            raise RuntimeError(
                "pywinauto Windows interaction component is not installed"
            ) from exc
        return Desktop(backend="uia")

    @staticmethod
    def _open_process(pid: int):
        if os.name != "nt":  # pragma: no cover - real backend is Windows-only
            raise RuntimeError("Windows UIA backend requires Windows")
        import ctypes

        process_query_limited_information = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            process_query_limited_information,
            False,
            pid,
        )
        if not handle:
            raise TargetNotFoundError(f"process {pid} is not available")
        return handle

    def process_started_ns(self, pid: int) -> int:
        import ctypes
        from ctypes import wintypes

        handle = self._open_process(pid)
        try:
            creation = wintypes.FILETIME()
            exit_time = wintypes.FILETIME()
            kernel = wintypes.FILETIME()
            user = wintypes.FILETIME()
            if not ctypes.windll.kernel32.GetProcessTimes(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exit_time),
                ctypes.byref(kernel),
                ctypes.byref(user),
            ):
                raise TargetNotFoundError(
                    f"cannot read process start identity for {pid}"
                )
            ticks = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
            return ticks * 100
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)

    def executable(self, pid: int) -> str:
        import ctypes
        from ctypes import wintypes

        handle = self._open_process(pid)
        try:
            size = wintypes.DWORD(32768)
            buffer = ctypes.create_unicode_buffer(size.value)
            if not ctypes.windll.kernel32.QueryFullProcessImageNameW(
                handle,
                0,
                buffer,
                ctypes.byref(size),
            ):
                raise TargetNotFoundError(
                    f"cannot read executable identity for {pid}"
                )
            return os.path.normcase(os.path.abspath(buffer.value))
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)

    def enumerate_windows(self, pid: int) -> tuple[UIAWindowRecord, ...]:
        windows: list[UIAWindowRecord] = []
        for wrapper in self._desktop().windows(
            visible_only=True,
            enabled_only=False,
        ):
            info = wrapper.element_info
            actual_pid = int(getattr(info, "process_id", 0) or 0)
            hwnd = int(getattr(info, "handle", 0) or 0)
            if actual_pid != pid or not hwnd:
                continue
            windows.append(
                UIAWindowRecord(
                    hwnd=hwnd,
                    pid=actual_pid,
                    title=str(getattr(info, "name", "") or ""),
                    enabled=bool(getattr(info, "enabled", True)),
                )
            )
        return tuple(windows)

    def _window(self, hwnd: int):
        matches = [
            wrapper
            for wrapper in self._desktop().windows(handle=hwnd)
            if int(wrapper.handle) == hwnd
        ]
        if not matches:
            raise TargetNotFoundError(f"window hwnd={hwnd} is unavailable")
        if len(matches) != 1:
            raise AmbiguousTargetError(
                f"multiple top-level UIA wrappers expose hwnd={hwnd}"
            )
        return matches[0]

    @staticmethod
    def _runtime_id(info) -> tuple[int, ...] | None:
        raw = getattr(info, "runtime_id", None)
        if raw in (None, 0):
            return None
        try:
            value = tuple(int(item) for item in raw)
        except (TypeError, ValueError, OverflowError):
            return None
        return value or None

    @staticmethod
    def _same_element(left, right) -> bool | None:
        """Compare two wrappers through UI Automation CompareElements semantics."""

        try:
            return bool(left.element_info == right.element_info)
        except Exception as exc:  # noqa: BLE001 - stale COM comparison must fail closed
            logger.debug("UIA CompareElements failed: %r", exc)
            return None

    def _deduplicate_same_elements(self, wrappers) -> tuple:
        """Collapse only wrappers proven to represent the same AutomationElement.

        RuntimeId collisions are preserved as distinct elements and later receive
        different Nika generations. A failed COM comparison is not evidence that
        two wrappers are the same, so ambiguity remains fail-closed.
        """

        unique: list = []
        for candidate in wrappers:
            candidate_runtime = self._runtime_id(candidate.element_info)
            duplicate = False
            for existing in unique:
                existing_runtime = self._runtime_id(existing.element_info)
                same = self._same_element(existing, candidate)
                if same is True:
                    if existing_runtime != candidate_runtime:
                        raise AmbiguousTargetError(
                            "one AutomationElement exposed conflicting RuntimeIds"
                        )
                    duplicate = True
                    break
                if same is None and existing_runtime == candidate_runtime:
                    raise AmbiguousTargetError(
                        "cannot distinguish duplicate RuntimeId after UIA comparison failure"
                    )
            if not duplicate:
                unique.append(candidate)
        return tuple(unique)

    @staticmethod
    def _patterns(wrapper) -> tuple[str, ...]:
        probes = (
            ("Invoke", "iface_invoke"),
            ("Value", "iface_value"),
            ("SelectionItem", "iface_selection_item"),
            ("Toggle", "iface_toggle"),
            ("ExpandCollapse", "iface_expand_collapse"),
            ("ScrollItem", "iface_scroll_item"),
            ("Text", "iface_text"),
            ("Window", "iface_window"),
        )
        available: list[str] = []
        for name, attribute in probes:
            try:
                if getattr(wrapper, attribute, None) is not None:
                    available.append(name)
            except Exception as exc:  # noqa: BLE001 - unsupported COM pattern
                logger.debug("UIA pattern probe failed for %s: %r", name, exc)
        return tuple(available)

    def _record(self, wrapper) -> UIAControlRecord:
        info = wrapper.element_info
        rectangle = getattr(info, "rectangle", None)
        bounds = None
        if rectangle is not None:
            bounds = (
                int(rectangle.left),
                int(rectangle.top),
                int(rectangle.right),
                int(rectangle.bottom),
            )
        try:
            raw_value = wrapper.get_value()
        except Exception as exc:  # noqa: BLE001 - Value is optional per control
            logger.debug("UIA Value probe unavailable: %r", exc)
            raw_value = None
        return UIAControlRecord(
            runtime_id=self._runtime_id(info),
            automation_id=str(getattr(info, "automation_id", "") or ""),
            role=str(getattr(info, "control_type", "") or "").lower(),
            name=str(getattr(info, "name", "") or "").strip(),
            enabled=bool(getattr(info, "enabled", True)),
            visible=bool(getattr(info, "visible", True)),
            focused=bool(getattr(info, "has_keyboard_focus", False)),
            value=None if raw_value is None else str(raw_value),
            bounds=bounds,
            class_name=str(getattr(info, "class_name", "") or ""),
            framework_id=str(getattr(info, "framework_id", "") or ""),
            patterns=self._patterns(wrapper),
        )

    def _assign_generations(
        self,
        hwnd: int,
        pairs: tuple[tuple[object, UIAControlRecord], ...],
    ) -> tuple[tuple[object, UIAControlRecord], ...]:
        """Bind each live element to a stable generation within its RuntimeId."""

        tracked_by_runtime = self._tracked.setdefault(hwnd, {})
        for tracked_group in tracked_by_runtime.values():
            for tracked in tracked_group:
                tracked.present = False

        output: list[tuple[object, UIAControlRecord]] = []
        duplicate_runtime_ids: set[tuple[int, ...]] = set()
        current_count: dict[tuple[int, ...], int] = {}

        for wrapper, record in pairs:
            runtime_id = record.runtime_id
            if runtime_id is None:
                output.append((wrapper, record))
                continue

            current_count[runtime_id] = current_count.get(runtime_id, 0) + 1
            tracked_group = tracked_by_runtime.setdefault(runtime_id, [])
            true_matches: list[_TrackedElement] = []
            comparison_failed = False
            for tracked in tracked_group:
                same = self._same_element(tracked.wrapper, wrapper)
                if same is True:
                    true_matches.append(tracked)
                elif same is None:
                    comparison_failed = True

            if len(true_matches) > 1:
                raise AmbiguousTargetError(
                    f"AutomationElement matched multiple generations for {runtime_id!r}"
                )
            if len(true_matches) == 1:
                tracked = true_matches[0]
                if tracked.present:
                    raise AmbiguousTargetError(
                        f"same AutomationElement appeared twice after dedup for {runtime_id!r}"
                    )
                generation = tracked.generation
                tracked.wrapper = wrapper
                tracked.present = True
            else:
                if comparison_failed:
                    raise AmbiguousTargetError(
                        f"cannot establish generation for RuntimeId {runtime_id!r}"
                    )
                generation = 1 + max(
                    (tracked.generation for tracked in tracked_group),
                    default=0,
                )
                tracked_group.append(
                    _TrackedElement(
                        generation=generation,
                        wrapper=wrapper,
                        present=True,
                    )
                )

            output.append(
                (
                    wrapper,
                    replace(record, element_generation=generation),
                )
            )

        for runtime_id, count in current_count.items():
            if count > 1:
                duplicate_runtime_ids.add(runtime_id)
        self._last_duplicate_runtime_ids = tuple(sorted(duplicate_runtime_ids))
        return tuple(output)

    def _pairs(
        self,
        hwnd: int,
        view: str,
    ) -> tuple[tuple[object, UIAControlRecord], ...]:
        if view not in {"control", "content"}:
            raise ValueError("view must be 'control' or 'content'")
        window = self._window(hwnd)
        wrappers = self._deduplicate_same_elements(
            (window, *window.descendants())
        )
        flag_name = (
            "is_control_element"
            if view == "control"
            else "is_content_element"
        )
        pairs: list[tuple[object, UIAControlRecord]] = []
        unaddressable_count = 0
        for wrapper in wrappers:
            if getattr(wrapper.element_info, flag_name, None) is False:
                continue
            record = self._record(wrapper)
            if record.runtime_id is None:
                unaddressable_count += 1
                logger.debug(
                    "Ignoring UIA element without usable RuntimeId; "
                    "it cannot receive semantic action authority"
                )
                continue
            pairs.append((wrapper, record))
        self._last_unaddressable_count = unaddressable_count
        return self._assign_generations(hwnd, tuple(pairs))

    def enumerate_controls(
        self,
        hwnd: int,
        view: str,
    ) -> tuple[UIAControlRecord, ...]:
        return tuple(record for _, record in self._pairs(hwnd, view))

    def _wrapper(
        self,
        hwnd: int,
        runtime_id: tuple[int, ...],
        generation: int,
    ):
        matches = [
            (wrapper, record)
            for wrapper, record in self._pairs(hwnd, "control")
            if record.runtime_id == runtime_id
            and record.element_generation == generation
        ]
        if not matches:
            raise StaleSnapshotError(
                f"UIA RuntimeId/generation {(runtime_id, generation)!r} is stale"
            )
        if len(matches) != 1:
            raise AmbiguousTargetError(
                f"duplicate live UIA RuntimeId/generation {(runtime_id, generation)!r}"
            )
        return matches[0][0]

    def focused_identity(
        self,
        hwnd: int,
    ) -> tuple[tuple[int, ...], int] | None:
        """Return provider-native focused UIA identity mapped to Nika generation.

        ``CurrentHasKeyboardFocus`` observed while enumerating an entire tree can
        lag or be absent even after a successful UIA ``SetFocus``.  UI Automation
        exposes one authoritative focused element through ``GetFocusedElement``;
        pywinauto exposes that as ``UIAElementInfo.get_active()``.  We use it only
        as a read-side observation, then bind it back to the exact live Nika
        RuntimeId/generation using UIA CompareElements semantics.  No name,
        coordinates or approximate matching are accepted.
        """

        try:
            from pywinauto.controls.uiawrapper import UIAWrapper
            from pywinauto.windows.uia_element_info import UIAElementInfo

            focused_wrapper = UIAWrapper(UIAElementInfo.get_active())
        except Exception as exc:  # noqa: BLE001 - transient provider focus query
            logger.debug("UIA GetFocusedElement unavailable: %r", exc)
            return None

        focused_runtime_id = self._runtime_id(focused_wrapper.element_info)
        matches: list[UIAControlRecord] = []
        for wrapper, record in self._pairs(hwnd, "control"):
            same = self._same_element(wrapper, focused_wrapper)
            if same is True:
                matches.append(record)
                continue
            if same is None and record.runtime_id == focused_runtime_id:
                raise AmbiguousTargetError(
                    "cannot bind provider focused element to exact RuntimeId/generation"
                )

        if len(matches) > 1:
            raise AmbiguousTargetError(
                "provider focused element matched multiple UIA controls"
            )
        if not matches:
            return None
        record = matches[0]
        if record.runtime_id is None:
            return None
        return record.runtime_id, record.element_generation

    def focus(
        self,
        hwnd: int,
        runtime_id: tuple[int, ...],
        generation: int,
    ) -> None:
        self._wrapper(hwnd, runtime_id, generation).set_focus()

    def _pattern_action(
        self,
        hwnd: int,
        runtime_id: tuple[int, ...],
        generation: int,
        attribute: str,
        method: str,
        label: str,
        *args,
    ) -> None:
        wrapper = self._wrapper(hwnd, runtime_id, generation)
        try:
            getattr(getattr(wrapper, attribute), method)(*args)
        except Exception as exc:
            raise UnsupportedInteractionError(
                f"{label} pattern is unavailable"
            ) from exc

    def invoke(
        self,
        hwnd: int,
        runtime_id: tuple[int, ...],
        generation: int,
    ) -> None:
        self._pattern_action(
            hwnd,
            runtime_id,
            generation,
            "iface_invoke",
            "Invoke",
            "Invoke",
        )

    def set_value(
        self,
        hwnd: int,
        runtime_id: tuple[int, ...],
        generation: int,
        value: str,
    ) -> None:
        self._pattern_action(
            hwnd,
            runtime_id,
            generation,
            "iface_value",
            "SetValue",
            "Value",
            value,
        )

    def select(
        self,
        hwnd: int,
        runtime_id: tuple[int, ...],
        generation: int,
    ) -> None:
        self._pattern_action(
            hwnd,
            runtime_id,
            generation,
            "iface_selection_item",
            "Select",
            "SelectionItem",
        )

    def toggle(
        self,
        hwnd: int,
        runtime_id: tuple[int, ...],
        generation: int,
    ) -> None:
        self._pattern_action(
            hwnd,
            runtime_id,
            generation,
            "iface_toggle",
            "Toggle",
            "Toggle",
        )

    def expand(
        self,
        hwnd: int,
        runtime_id: tuple[int, ...],
        generation: int,
    ) -> None:
        self._pattern_action(
            hwnd,
            runtime_id,
            generation,
            "iface_expand_collapse",
            "Expand",
            "ExpandCollapse",
        )

    def collapse(
        self,
        hwnd: int,
        runtime_id: tuple[int, ...],
        generation: int,
    ) -> None:
        self._pattern_action(
            hwnd,
            runtime_id,
            generation,
            "iface_expand_collapse",
            "Collapse",
            "ExpandCollapse",
        )


class WindowsUIAInteractionAdapter:
    """Strict semantic UIA adapter backed by exact RuntimeId + generation identity."""

    def __init__(
        self,
        *,
        process_id: int,
        window_title: str,
        backend: WindowsUIABackend | None = None,
        view: str = "control",
    ) -> None:
        if process_id <= 0:
            raise ValueError("process_id must be positive")
        if not window_title.strip():
            raise ValueError("window_title is required")
        if view not in {"control", "content"}:
            raise ValueError("view must be 'control' or 'content'")
        self.process_id = process_id
        self.window_title = window_title
        self.backend = backend or PywinautoUIABackend()
        self.view = view
        started = self.backend.process_started_ns(process_id)
        executable = self.backend.executable(process_id)
        self._application = ApplicationIdentity(
            process_id,
            executable,
            started,
        )
        self._window: WindowIdentity | None = None
        self._window_handle: int | None = None
        self._window_generation = 0
        self._snapshot_revision = 0
        self._identity_by_node: dict[str, tuple[tuple[int, ...], int]] = {}

    @staticmethod
    def _identity_digest(runtime_id: tuple[int, ...], generation: int) -> str:
        payload = f"{','.join(str(item) for item in runtime_id)}|{generation}".encode()
        return hashlib.sha256(payload).hexdigest()

    def _live_hwnd(self) -> int:
        windows = [
            window
            for window in self.backend.enumerate_windows(self.process_id)
            if window.title == self.window_title
        ]
        if not windows:
            raise TargetNotFoundError(
                f"UIA window {self.window_title!r} is not available"
            )
        if len(windows) != 1:
            raise AmbiguousTargetError(
                f"multiple UIA windows match exact title {self.window_title!r}"
            )
        window = windows[0]
        if not window.enabled:
            raise UnsupportedInteractionError("target window is disabled")
        if self._window_handle is None:
            self._window_handle = window.hwnd
            self._window_generation = 1
        elif self._window_handle != window.hwnd:
            self._window_handle = window.hwnd
            self._window_generation += 1
        return window.hwnd

    def _semantic_snapshot(
        self,
        *,
        revision: int,
        controls: tuple[UIAControlRecord, ...],
    ) -> SemanticSnapshot:
        hwnd = self._live_hwnd()
        assert self._window_handle == hwnd
        self._window = WindowIdentity(
            native_handle=hwnd,
            generation=self._window_generation,
            title=self.window_title,
        )
        nodes: list[ControlNode] = []
        self._identity_by_node = {}
        for record in controls:
            runtime_id = record.runtime_id
            if runtime_id is None:
                continue
            identity = (runtime_id, record.element_generation)
            node_id = self._identity_digest(*identity)
            if node_id in self._identity_by_node:
                raise AmbiguousTargetError(
                    f"duplicate Nika UIA identity for {identity!r}"
                )
            self._identity_by_node[node_id] = identity
            nodes.append(
                ControlNode(
                    node_id=node_id,
                    role=record.role,
                    name=record.name,
                    enabled=record.enabled,
                    visible=record.visible,
                    focused=record.focused,
                    value=record.value,
                    bounds=record.bounds,
                    automation_id=record.automation_id,
                    class_name=record.class_name,
                    framework_id=record.framework_id,
                )
            )
        return SemanticSnapshot(
            revision=revision,
            target=InteractionTarget(self._application, self._window),
            controls=tuple(nodes),
        )

    def observe(self) -> SemanticSnapshot:
        hwnd = self._live_hwnd()
        records = self.backend.enumerate_controls(hwnd, self.view)
        self._snapshot_revision += 1
        return self._semantic_snapshot(
            revision=self._snapshot_revision,
            controls=records,
        )

    def capture_focus(self) -> str | None:
        hwnd = self._live_hwnd()
        focused = self.backend.focused_identity(hwnd)
        if focused is None:
            return None
        runtime_id, generation = focused
        node_id = self._identity_digest(runtime_id, generation)
        if self._identity_by_node.get(node_id) != focused:
            return None
        return node_id

    def focus(self, node: ControlNode) -> None:
        hwnd = self._live_hwnd()
        runtime_id, generation = self._control_identity(node)
        self.backend.focus(hwnd, runtime_id, generation)
        if self.backend.focused_identity(hwnd) != (runtime_id, generation):
            raise StaleSnapshotError(
                "UIA focus verification failed for RuntimeId/generation"
            )

    def restore_focus(self, node_id: str | None) -> bool:
        if node_id is None:
            return True
        identity = self._identity_by_node.get(node_id)
        if identity is None:
            return False
        hwnd = self._live_hwnd()
        runtime_id, generation = identity
        try:
            self.backend.focus(hwnd, runtime_id, generation)
        except (TargetNotFoundError, StaleSnapshotError):
            return False
        return self.backend.focused_identity(hwnd) == identity

    def pattern_capabilities(self, node: ControlNode) -> tuple[str, ...]:
        hwnd = self._live_hwnd()
        runtime_id, generation = self._control_identity(node)
        records = [
            record
            for record in self.backend.enumerate_controls(hwnd, self.view)
            if record.runtime_id == runtime_id
            and record.element_generation == generation
        ]
        if len(records) != 1:
            raise StaleSnapshotError(
                "UIA pattern capability target is stale or ambiguous"
            )

    def _control_identity(self, node: ControlNode) -> tuple[tuple[int, ...], int]:
        identity = self._identity_by_node.get(node.node_id)
        if identity is None:
            raise StaleSnapshotError("control is not from the latest UIA snapshot")
        expected = self._identity_digest(*identity)
        if expected != node.node_id:
            raise StaleSnapshotError("control identity digest mismatch")
        return identity

    def act(
        self,
        node: ControlNode,
        action: InteractionAction,
        value: str | None,
    ) -> None:
        if action is InteractionAction.SET_VALUE and value is None:
            raise ValueError("SET_VALUE requires a value")
        hwnd = self._live_hwnd()
        runtime_id, generation = self._control_identity(node)
        method = {
            InteractionAction.FOCUS: self.backend.focus,
            InteractionAction.INVOKE: self.backend.invoke,
            InteractionAction.SET_VALUE: self.backend.set_value,
            InteractionAction.SELECT: self.backend.select,
            InteractionAction.TOGGLE: self.backend.toggle,
            InteractionAction.EXPAND: self.backend.expand,
            InteractionAction.COLLAPSE: self.backend.collapse,
        }.get(action)
        if method is None:
            raise UnsupportedInteractionError(
                f"{action.value} has no semantic Windows UIA effect adapter"
            )
        if action is InteractionAction.SET_VALUE:
            assert value is not None
            method(hwnd, runtime_id, generation, value)  # type: ignore[call-arg]
        else:
            method(hwnd, runtime_id, generation)  # type: ignore[call-arg]
