"""Strict Windows UI Automation adapter for Nika computer interaction.

pywinauto/UIA objects are confined to this module. Window selection is explicit and
fail-closed; no ``top_window()`` or positional control selection is used.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from dataclasses import dataclass
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
    parent_runtime_id: tuple[int, ...] | None = None
    patterns: tuple[str, ...] = ()


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
    def enumerate_controls(self, hwnd: int, view: str) -> tuple[UIAControlRecord, ...]: ...
    def focused_runtime_id(self, hwnd: int) -> tuple[int, ...] | None: ...
    def focus(self, hwnd: int, runtime_id: tuple[int, ...]) -> None: ...
    def invoke(self, hwnd: int, runtime_id: tuple[int, ...]) -> None: ...
    def set_value(self, hwnd: int, runtime_id: tuple[int, ...], value: str) -> None: ...
    def select(self, hwnd: int, runtime_id: tuple[int, ...]) -> None: ...
    def toggle(self, hwnd: int, runtime_id: tuple[int, ...]) -> None: ...
    def expand(self, hwnd: int, runtime_id: tuple[int, ...]) -> None: ...
    def collapse(self, hwnd: int, runtime_id: tuple[int, ...]) -> None: ...


class PywinautoUIABackend:
    """Thin pywinauto backend. Import is lazy so non-Windows/core installs stay clean."""

    def _desktop(self):
        try:
            from pywinauto import Desktop
        except ImportError as exc:  # pragma: no cover - optional Windows dependency
            raise RuntimeError("pywinauto Windows interaction component is not installed") from exc
        return Desktop(backend="uia")

    def process_started_ns(self, pid: int) -> int:
        if os.name != "nt":  # pragma: no cover - real backend is Windows-only
            raise RuntimeError("Windows UIA backend requires Windows")
        import ctypes
        from ctypes import wintypes

        process_query_limited_information = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            process_query_limited_information, False, pid
        )
        if not handle:
            raise TargetNotFoundError(f"process {pid} is not available")
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
                raise TargetNotFoundError(f"cannot read process start identity for {pid}")
            ticks = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
            return ticks * 100
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)

    def executable(self, pid: int) -> str:
        if os.name != "nt":  # pragma: no cover
            raise RuntimeError("Windows UIA backend requires Windows")
        import ctypes
        from ctypes import wintypes

        process_query_limited_information = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            process_query_limited_information, False, pid
        )
        if not handle:
            raise TargetNotFoundError(f"process {pid} is not available")
        try:
            size = wintypes.DWORD(32768)
            buf = ctypes.create_unicode_buffer(size.value)
            if not ctypes.windll.kernel32.QueryFullProcessImageNameW(
                handle, 0, buf, ctypes.byref(size)
            ):
                raise TargetNotFoundError(f"cannot read executable identity for {pid}")
            return os.path.normcase(os.path.abspath(buf.value))
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)

    def enumerate_windows(self, pid: int) -> tuple[UIAWindowRecord, ...]:
        windows: list[UIAWindowRecord] = []
        # pywinauto's process= filter can miss freshly-created top-level UIA windows on
        # hosted Windows runners. Enumerate the top-level UIA surface, then enforce PID
        # identity ourselves. This remains semantic and fail-closed: no z-order/top_window
        # guessing and no positional selection are introduced.
        for wrapper in self._desktop().windows(visible_only=True, enabled_only=False):
            info = wrapper.element_info
            process_id = int(getattr(info, "process_id", 0) or 0)
            if process_id != pid:
                continue
            handle = int(getattr(info, "handle", 0) or 0)
            if not handle:
                continue
            windows.append(
                UIAWindowRecord(
                    hwnd=handle,
                    pid=process_id,
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
        if len(matches) != 1:
            raise TargetNotFoundError(f"window hwnd={hwnd} is unavailable")
        return matches[0]

    @staticmethod
    def _runtime_id(info) -> tuple[int, ...] | None:
        raw = getattr(info, "runtime_id", None)
        if raw is None:
            return None
        try:
            return tuple(int(value) for value in raw)
        except TypeError:
            return None

    @staticmethod
    def _same_element(left, right) -> bool:
        """Use pywinauto's UIAElementInfo equality, which delegates to CompareElements.

        RuntimeId is opaque identity evidence but may appear more than once when a provider
        or traversal returns multiple wrappers for the same live AutomationElement. Those
        wrappers may be collapsed only when UI Automation itself confirms they are the same
        element. Comparison failure is treated as distinct so the caller remains fail-closed.
        """
        try:
            return bool(left.element_info == right.element_info)
        except Exception as exc:  # noqa: BLE001 - stale/failed COM comparison must fail closed
            logger.debug("UIA CompareElements-equivalent comparison failed: %r", exc)
            return False

    def _dedupe_same_elements(self, wrappers) -> tuple:
        unique: list = []
        by_runtime: dict[tuple[int, ...], list] = {}
        for wrapper in wrappers:
            runtime_id = self._runtime_id(wrapper.element_info)
            if runtime_id is None:
                unique.append(wrapper)
                continue
            same_runtime = by_runtime.setdefault(runtime_id, [])
            if any(self._same_element(existing, wrapper) for existing in same_runtime):
                continue
            same_runtime.append(wrapper)
            unique.append(wrapper)
        return tuple(unique)

    @staticmethod
    def _patterns(wrapper) -> tuple[str, ...]:
        pairs = (
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
        for name, attribute in pairs:
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
        value = None
        try:
            value = wrapper.get_value()
        except Exception as exc:  # noqa: BLE001 - not every control supports Value
            logger.debug("UIA Value probe unavailable: %r", exc)
        return UIAControlRecord(
            runtime_id=self._runtime_id(info),
            automation_id=str(getattr(info, "automation_id", "") or ""),
            role=str(getattr(info, "control_type", "") or "").lower(),
            name=str(getattr(info, "name", "") or "").strip(),
            enabled=bool(getattr(info, "enabled", True)),
            visible=bool(getattr(info, "visible", True)),
            focused=bool(getattr(info, "has_keyboard_focus", False)),
            value=None if value is None else str(value),
            bounds=bounds,
            class_name=str(getattr(info, "class_name", "") or ""),
            framework_id=str(getattr(info, "framework_id", "") or ""),
            patterns=self._patterns(wrapper),
        )

    def enumerate_controls(self, hwnd: int, view: str) -> tuple[UIAControlRecord, ...]:
        if view not in {"control", "content"}:
            raise ValueError("view must be 'control' or 'content'")
        window = self._window(hwnd)
        wrappers = self._dedupe_same_elements((window, *window.descendants()))
        records: list[UIAControlRecord] = []
        for wrapper in wrappers:
            info = wrapper.element_info
            flag_name = "is_control_element" if view == "control" else "is_content_element"
            marker = getattr(info, flag_name, None)
            if marker is False:
                continue
            records.append(self._record(wrapper))
        return tuple(records)

    def _wrapper_by_runtime_id(self, hwnd: int, runtime_id: tuple[int, ...]):
        window = self._window(hwnd)
        candidates = self._dedupe_same_elements((window, *window.descendants()))
        matches = [
            wrapper
            for wrapper in candidates
            if self._runtime_id(wrapper.element_info) == runtime_id
        ]
        if not matches:
            raise TargetNotFoundError(f"UIA runtime id {runtime_id!r} is unavailable")
        if len(matches) > 1:
            raise AmbiguousTargetError(f"duplicate UIA runtime id {runtime_id!r}")
        return matches[0]

    def focused_runtime_id(self, hwnd: int) -> tuple[int, ...] | None:
        for record in self.enumerate_controls(hwnd, "control"):
            if record.focused:
                return record.runtime_id
        return None

    def focus(self, hwnd: int, runtime_id: tuple[int, ...]) -> None:
        self._wrapper_by_runtime_id(hwnd, runtime_id).set_focus()

    def invoke(self, hwnd: int, runtime_id: tuple[int, ...]) -> None:
        wrapper = self._wrapper_by_runtime_id(hwnd, runtime_id)
        try:
            wrapper.iface_invoke.Invoke()
        except Exception as exc:
            raise UnsupportedInteractionError("Invoke pattern is unavailable") from exc

    def set_value(self, hwnd: int, runtime_id: tuple[int, ...], value: str) -> None:
        wrapper = self._wrapper_by_runtime_id(hwnd, runtime_id)
        try:
            wrapper.iface_value.SetValue(value)
        except Exception as exc:
            raise UnsupportedInteractionError("Value pattern is unavailable") from exc

    def select(self, hwnd: int, runtime_id: tuple[int, ...]) -> None:
        wrapper = self._wrapper_by_runtime_id(hwnd, runtime_id)
        try:
            wrapper.iface_selection_item.Select()
        except Exception as exc:
            raise UnsupportedInteractionError("SelectionItem pattern is unavailable") from exc

    def toggle(self, hwnd: int, runtime_id: tuple[int, ...]) -> None:
        wrapper = self._wrapper_by_runtime_id(hwnd, runtime_id)
        try:
            wrapper.iface_toggle.Toggle()
        except Exception as exc:
            raise UnsupportedInteractionError("Toggle pattern is unavailable") from exc

    def expand(self, hwnd: int, runtime_id: tuple[int, ...]) -> None:
        wrapper = self._wrapper_by_runtime_id(hwnd, runtime_id)
        try:
            wrapper.iface_expand_collapse.Expand()
        except Exception as exc:
            raise UnsupportedInteractionError("ExpandCollapse pattern is unavailable") from exc

    def collapse(self, hwnd: int, runtime_id: tuple[int, ...]) -> None:
        wrapper = self._wrapper_by_runtime_id(hwnd, runtime_id)
        try:
            wrapper.iface_expand_collapse.Collapse()
        except Exception as exc:
            raise UnsupportedInteractionError("ExpandCollapse pattern is unavailable") from exc


class WindowsUIAInteractionAdapter:
    """Production-intended semantic Windows adapter with exact window identity."""

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
            raise ValueError("provide exactly one of window_title or native_handle")
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
        self._last_revision = 0
        self._runtime_by_node: dict[str, tuple[int, ...]] = {}

    def _exact_window(self) -> UIAWindowRecord:
        windows = tuple(
            window
            for window in self.backend.enumerate_windows(self.process_id)
            if window.pid == self.process_id
        )
        if self.native_handle is not None:
            matches = [window for window in windows if window.hwnd == self.native_handle]
        else:
            matches = [window for window in windows if window.title == self.window_title]
        if not matches:
            raise TargetNotFoundError("exact top-level UIA window was not found")
        if len(matches) > 1:
            raise AmbiguousTargetError("multiple top-level UIA windows match the exact identity")
        return matches[0]

    def _identity(self, window: UIAWindowRecord) -> tuple[ApplicationIdentity, WindowIdentity]:
        started = self.backend.process_started_ns(self.process_id)
        executable = self.backend.executable(self.process_id)
        app = ApplicationIdentity(
            executable=executable,
            pid=self.process_id,
            process_started_ns=started,
        )
        if self._application is None:
            self._application = app
            self._hwnd = window.hwnd
            self._generation = 1
        elif app != self._application or window.hwnd != self._hwnd:
            raise StaleSnapshotError("process/window identity changed; rebind is required")
        return app, WindowIdentity(app, window.hwnd, self._generation)

    @staticmethod
    def _node_id(hwnd: int, record: UIAControlRecord) -> str:
        if record.runtime_id is None:
            raise UnsupportedInteractionError(
                "UIA control has no runtime id; positional fallback is forbidden"
            )
        payload = f"{hwnd}:" + ".".join(str(part) for part in record.runtime_id)
        return "uia:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]

    @staticmethod
    def _revision(records: tuple[UIAControlRecord, ...], generation: int) -> int:
        stable = [
            (
                record.runtime_id,
                record.automation_id,
                record.role,
                record.name,
                record.enabled,
                record.visible,
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
        runtime_by_node: dict[str, tuple[int, ...]] = {}
        controls: list[ControlNode] = []
        for record in records:
            node_id = self._node_id(window.hwnd, record)
            if node_id in runtime_by_node:
                raise AmbiguousTargetError("duplicate UIA semantic node identity")
            assert record.runtime_id is not None
            runtime_by_node[node_id] = record.runtime_id
            attributes = tuple(
                (key, value)
                for key, value in (
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
        revision = self._revision(records, self._generation)
        self._last_revision = revision
        self._runtime_by_node = runtime_by_node
        return SemanticSnapshot(
            target=InteractionTarget(application=application, window=window_identity),
            generation=self._generation,
            revision=revision,
            controls=tuple(controls),
        )

    def _runtime_id(self, node: ControlNode) -> tuple[int, ...]:
        runtime_id = self._runtime_by_node.get(node.node_id)
        if runtime_id is None:
            raise StaleSnapshotError("control does not belong to the current observation")
        return runtime_id

    def _assert_live(self) -> int:
        window = self._exact_window()
        self._identity(window)
        assert self._hwnd is not None
        return self._hwnd

    def capture_focus(self) -> str | None:
        hwnd = self._assert_live()
        runtime_id = self.backend.focused_runtime_id(hwnd)
        if runtime_id is None:
            return None
        for node_id, known_runtime_id in self._runtime_by_node.items():
            if known_runtime_id == runtime_id:
                return node_id
        return None

    def focus(self, node: ControlNode) -> None:
        hwnd = self._assert_live()
        runtime_id = self._runtime_id(node)
        self.backend.focus(hwnd, runtime_id)
        if self.backend.focused_runtime_id(hwnd) != runtime_id:
            raise StaleSnapshotError("UIA focus verification failed")

    def restore_focus(self, node_id: str | None) -> bool:
        if node_id is None:
            return True
        runtime_id = self._runtime_by_node.get(node_id)
        if runtime_id is None:
            return False
        hwnd = self._assert_live()
        self.backend.focus(hwnd, runtime_id)
        return self.backend.focused_runtime_id(hwnd) == runtime_id

    def act(self, node: ControlNode, action: InteractionAction, value: str | None) -> None:
        hwnd = self._assert_live()
        runtime_id = self._runtime_id(node)
        if not node.enabled or not node.visible:
            raise UnsupportedInteractionError("disabled/hidden controls cannot be acted on")
        if action is InteractionAction.FOCUS:
            self.focus(node)
        elif action is InteractionAction.INVOKE:
            self.backend.invoke(hwnd, runtime_id)
        elif action is InteractionAction.SET_VALUE:
            if value is None:
                raise ValueError("SET_VALUE requires a value")
            self.backend.set_value(hwnd, runtime_id, value)
        elif action is InteractionAction.SELECT:
            self.backend.select(hwnd, runtime_id)
        elif action is InteractionAction.TOGGLE:
            self.backend.toggle(hwnd, runtime_id)
        elif action is InteractionAction.EXPAND:
            self.backend.expand(hwnd, runtime_id)
        elif action is InteractionAction.COLLAPSE:
            self.backend.collapse(hwnd, runtime_id)
        else:  # pragma: no cover - enum exhaustiveness
            raise UnsupportedInteractionError(f"unsupported UIA action: {action}")

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

    def pattern_capabilities(self, node: ControlNode) -> tuple[str, ...]:
        attributes = dict(node.attributes)
        value = attributes.get("patterns", "")
        return tuple(item for item in value.split(",") if item)


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
    """Keep pywinauto primary unless a comparable raw-UIA proof is strictly better.

    A raw adapter is not promoted merely for lower latency. It must preserve the complete
    safety/identity baseline and improve either pattern coverage or median observation latency.
    """
    if pywinauto.backend != "pywinauto" or pywinauto.sample_count < 3:
        raise ValueError("pywinauto baseline requires at least three measured samples")
    required = (
        pywinauto.exact_identity
        and pywinauto.strict_ambiguity
        and pywinauto.focus_verified
    )
    if not required:
        raise ValueError("pywinauto measurement does not prove the required safety baseline")
    if raw_uia is None:
        return "pywinauto"
    if raw_uia.backend != "raw-uia" or raw_uia.sample_count < 3:
        raise ValueError("raw UIA comparison requires at least three measured samples")
    raw_safe = raw_uia.exact_identity and raw_uia.strict_ambiguity and raw_uia.focus_verified
    if not raw_safe:
        return "pywinauto"
    py_patterns = set(pywinauto.pattern_coverage)
    raw_patterns = set(raw_uia.pattern_coverage)
    coverage_better = raw_patterns > py_patterns
    latency_better = raw_uia.median_observe_ms <= pywinauto.median_observe_ms * 0.8
    return "raw-uia" if coverage_better or latency_better else "pywinauto"


def measure_observation(
    adapter: WindowsUIAInteractionAdapter, samples: int = 5
) -> tuple[float, ...]:
    if samples < 3 or samples > 20:
        raise ValueError("samples must be between 3 and 20")
    results: list[float] = []
    for _ in range(samples):
        started = time.perf_counter()
        adapter.observe()
        results.append((time.perf_counter() - started) * 1000.0)
    return tuple(results)
