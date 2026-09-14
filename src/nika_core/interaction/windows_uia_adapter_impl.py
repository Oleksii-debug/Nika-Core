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
        focused = [
            record
            for record in self.enumerate_controls(hwnd, "control")
            if record.focused and record.runtime_id is not None
        ]
        if len(focused) > 1:
            raise AmbiguousTargetError(
                "multiple UIA controls report keyboard focus"
            )
        if not focused:
            return None
        record = focused[0]
        assert record.runtime_id is not None
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
    """Semantic Windows adapter with exact process/window/control identity."""

    def __init__(
        self,
        *,
        process_id: int,
        window_title: str | None = None,
        native_handle: int | None = None,
        view: str = "control",
        backend: WindowsUIABackend | None = None,
    ) -> None:
        if process_id <= 0:
            raise ValueError("process_id must be positive")
        if (window_title is None) == (native_handle is None):
            raise ValueError(
                "provide exactly one of window_title or native_handle"
            )
        if view not in {"control", "content"}:
            raise ValueError("view must be 'control' or 'content'")
        self.process_id = process_id
        self.window_title = window_title
        self.native_handle = native_handle
        self.view = view
        self.backend = backend or PywinautoUIABackend()
        self._application: ApplicationIdentity | None = None
        self._hwnd: int | None = None
        self._generation = 0
        self._identity_by_node: dict[
            str,
            tuple[tuple[int, ...], int],
        ] = {}

    def _exact_window(self) -> UIAWindowRecord:
        windows = tuple(
            window
            for window in self.backend.enumerate_windows(self.process_id)
            if window.pid == self.process_id
        )
        if self.native_handle is not None:
            matches = [
                window
                for window in windows
                if window.hwnd == self.native_handle
            ]
        else:
            matches = [
                window
                for window in windows
                if window.title == self.window_title
            ]
        if not matches:
            raise TargetNotFoundError(
                "exact top-level UIA window was not found"
            )
        if len(matches) != 1:
            raise AmbiguousTargetError(
                "multiple top-level UIA windows match the exact identity"
            )
        return matches[0]

    def _identity(
        self,
        window: UIAWindowRecord,
    ) -> tuple[ApplicationIdentity, WindowIdentity]:
        app = ApplicationIdentity(
            executable=self.backend.executable(self.process_id),
            pid=self.process_id,
            process_started_ns=self.backend.process_started_ns(self.process_id),
        )
        if self._application is None:
            self._application = app
            self._hwnd = window.hwnd
            self._generation = 1
        elif app != self._application or window.hwnd != self._hwnd:
            raise StaleSnapshotError(
                "process/window identity changed; rebind is required"
            )
        return app, WindowIdentity(
            application=app,
            native_handle=window.hwnd,
            generation=self._generation,
        )

    @staticmethod
    def _node_id(
        hwnd: int,
        window_generation: int,
        runtime_id: tuple[int, ...],
        element_generation: int,
    ) -> str:
        payload = (
            f"{hwnd}:{window_generation}:{element_generation}:"
            + ".".join(str(part) for part in runtime_id)
        )
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        return "uia:" + digest[:32]

    @staticmethod
    def _revision(
        records: tuple[UIAControlRecord, ...],
        generation: int,
    ) -> int:
        stable = [
            (
                record.runtime_id,
                record.element_generation,
                record.automation_id,
                record.role,
                record.name,
                record.enabled,
                record.visible,
                record.focused,
                record.value,
                record.class_name,
                record.framework_id,
                record.patterns,
            )
            for record in records
        ]
        payload = repr((generation, stable)).encode("utf-8")
        return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")

    def observe(self) -> SemanticSnapshot:
        window = self._exact_window()
        application, window_identity = self._identity(window)
        records = self.backend.enumerate_controls(window.hwnd, self.view)
        identity_by_node: dict[
            str,
            tuple[tuple[int, ...], int],
        ] = {}
        controls: list[ControlNode] = []
        for record in records:
            if record.runtime_id is None:
                raise UnsupportedInteractionError(
                    "UIA control has no RuntimeId; positional fallback is forbidden"
                )
            node_id = self._node_id(
                window.hwnd,
                self._generation,
                record.runtime_id,
                record.element_generation,
            )
            if node_id in identity_by_node:
                raise AmbiguousTargetError(
                    "duplicate UIA RuntimeId/generation identity after normalization"
                )
            identity_by_node[node_id] = (
                record.runtime_id,
                record.element_generation,
            )
            attributes = tuple(
                (key, value)
                for key, value in (
                    (
                        "runtime_id",
                        ".".join(str(part) for part in record.runtime_id),
                    ),
                    (
                        "element_generation",
                        str(record.element_generation),
                    ),
                    ("automation_id", record.automation_id),
                    ("class_name", record.class_name),
                    ("framework_id", record.framework_id),
                    ("patterns", ",".join(record.patterns)),
                    ("view", self.view),
                )
                if value
            )
            controls.append(
                ControlNode(
                    node_id=node_id,
                    role=record.role,
                    name=record.name,
                    enabled=record.enabled,
                    visible=record.visible,
                    focused=record.focused,
                    value=record.value,
                    bounds=record.bounds,
                    attributes=attributes,
                )
            )
        self._identity_by_node = identity_by_node
        return SemanticSnapshot(
            target=InteractionTarget(
                application=application,
                window=window_identity,
            ),
            generation=self._generation,
            revision=self._revision(records, self._generation),
            controls=tuple(controls),
        )

    def _live_hwnd(self) -> int:
        window = self._exact_window()
        self._identity(window)
        assert self._hwnd is not None
        return self._hwnd

    def _control_identity(
        self,
        node: ControlNode,
    ) -> tuple[tuple[int, ...], int]:
        identity = self._identity_by_node.get(node.node_id)
        if identity is None:
            raise StaleSnapshotError(
                "control does not belong to the current observation"
            )
        return identity

    def capture_focus(self) -> str | None:
        identity = self.backend.focused_identity(self._live_hwnd())
        if identity is None:
            return None
        matches = [
            node_id
            for node_id, known_identity in self._identity_by_node.items()
            if known_identity == identity
        ]
        if len(matches) > 1:
            raise AmbiguousTargetError(
                "focused UIA identity maps to multiple semantic nodes"
            )
        return matches[0] if matches else None

    def focus(self, node: ControlNode) -> None:
        hwnd = self._live_hwnd()
        runtime_id, generation = self._control_identity(node)
        self.backend.focus(hwnd, runtime_id, generation)
        if self.backend.focused_identity(hwnd) != (runtime_id, generation):
            raise StaleSnapshotError("UIA focus verification failed")

    def restore_focus(self, node_id: str | None) -> bool:
        if node_id is None:
            return True
        identity = self._identity_by_node.get(node_id)
        if identity is None:
            return False
        hwnd = self._live_hwnd()
        try:
            self.backend.focus(hwnd, *identity)
        except (TargetNotFoundError, StaleSnapshotError):
            return False
        return self.backend.focused_identity(hwnd) == identity

    @staticmethod
    def pattern_capabilities(node: ControlNode) -> tuple[str, ...]:
        value = dict(node.attributes).get("patterns", "")
        return tuple(item for item in value.split(",") if item)

    def act(
        self,
        node: ControlNode,
        action: InteractionAction,
        value: str | None,
    ) -> None:
        hwnd = self._live_hwnd()
        runtime_id, generation = self._control_identity(node)
        if not node.enabled or not node.visible:
            raise UnsupportedInteractionError(
                "disabled/hidden controls cannot be acted on"
            )
        if action is InteractionAction.FOCUS:
            self.focus(node)
            return

        required_pattern = {
            InteractionAction.INVOKE: "Invoke",
            InteractionAction.SET_VALUE: "Value",
            InteractionAction.SELECT: "SelectionItem",
            InteractionAction.TOGGLE: "Toggle",
            InteractionAction.EXPAND: "ExpandCollapse",
            InteractionAction.COLLAPSE: "ExpandCollapse",
        }.get(action)
        if required_pattern not in self.pattern_capabilities(node):
            raise UnsupportedInteractionError(
                f"{required_pattern or action.value} pattern is unavailable"
            )
        if action is InteractionAction.SET_VALUE and value is None:
            raise ValueError("SET_VALUE requires a value")

        method = {
            InteractionAction.INVOKE: self.backend.invoke,
            InteractionAction.SET_VALUE: self.backend.set_value,
            InteractionAction.SELECT: self.backend.select,
            InteractionAction.TOGGLE: self.backend.toggle,
            InteractionAction.EXPAND: self.backend.expand,
            InteractionAction.COLLAPSE: self.backend.collapse,
        }[action]
        if action is InteractionAction.SET_VALUE:
            assert value is not None
            method(hwnd, runtime_id, generation, value)  # type: ignore[call-arg]
        else:
            method(hwnd, runtime_id, generation)  # type: ignore[call-arg]

    def verify(
        self,
        before: SemanticSnapshot,
        after: SemanticSnapshot,
        node: ControlNode,
        action: InteractionAction,
        value: str | None,
    ) -> bool:
        if before.target != after.target or before.generation != after.generation:
            return False
        if action is InteractionAction.FOCUS:
            return any(
                control.node_id == node.node_id and control.focused
                for control in after.controls
            )
        if action is InteractionAction.SET_VALUE:
            return any(
                control.node_id == node.node_id and control.value == value
                for control in after.controls
            )
        return after.revision != before.revision


@dataclass(frozen=True, slots=True)
class UIABackendMeasurement:
    backend: str
    sample_count: int
    median_observe_ms: float
    exact_identity: bool
    strict_ambiguity: bool
    focus_verified: bool
    pattern_coverage: tuple[str, ...]


def choose_measured_backend(
    pywinauto: UIABackendMeasurement,
    raw_uia: UIABackendMeasurement | None,
) -> str:
    """Retain pywinauto unless a raw UIA adapter is safely and materially better."""

    if pywinauto.backend != "pywinauto" or pywinauto.sample_count < 3:
        raise ValueError(
            "pywinauto baseline requires at least three measured samples"
        )
    if not (
        pywinauto.exact_identity
        and pywinauto.strict_ambiguity
        and pywinauto.focus_verified
    ):
        raise ValueError(
            "pywinauto measurement does not prove the required safety baseline"
        )
    if raw_uia is None:
        return "pywinauto"
    if raw_uia.backend != "raw-uia" or raw_uia.sample_count < 3:
        raise ValueError(
            "raw UIA comparison requires at least three measured samples"
        )
    if not (
        raw_uia.exact_identity
        and raw_uia.strict_ambiguity
        and raw_uia.focus_verified
    ):
        return "pywinauto"
    coverage_better = set(raw_uia.pattern_coverage) > set(
        pywinauto.pattern_coverage
    )
    latency_better = (
        raw_uia.median_observe_ms
        <= pywinauto.median_observe_ms * 0.8
    )
    return "raw-uia" if coverage_better or latency_better else "pywinauto"


def measure_observation(
    adapter: WindowsUIAInteractionAdapter,
    samples: int = 5,
) -> tuple[float, ...]:
    if not 3 <= samples <= 20:
        raise ValueError("samples must be between 3 and 20")
    results: list[float] = []
    for _ in range(samples):
        started = time.perf_counter()
        adapter.observe()
        results.append((time.perf_counter() - started) * 1000.0)
    return tuple(results)
