"""Strict Windows UIA adapter with live pre-effect semantic authority revalidation.

The incumbent implementation lives in ``windows_uia_adapter_impl`` unchanged so its
RuntimeId + generation identity and pywinauto tracking remain reviewable. This
module adds the action-boundary fail-closed contract and provider/OS-native focus
reads that map the authoritative keyboard-focus element back to exact Nika identity.
"""

from __future__ import annotations

import logging
import os
import time
from types import SimpleNamespace

from .domain import (
    AmbiguousTargetError,
    ControlNode,
    InteractionAction,
    StaleSnapshotError,
    TargetNotFoundError,
    UnsupportedInteractionError,
)
from .windows_uia_adapter_impl import (
    PywinautoUIABackend as _BasePywinautoUIABackend,
)
from .windows_uia_adapter_impl import (
    UIABackendMeasurement,
    UIAControlRecord,
    UIAWindowRecord,
    WindowsUIABackend,
    choose_measured_backend,
    measure_observation,
)
from .windows_uia_adapter_impl import (
    WindowsUIAInteractionAdapter as _BaseWindowsUIAInteractionAdapter,
)

logger = logging.getLogger(__name__)

_REQUIRED_PATTERN = {
    InteractionAction.INVOKE: "Invoke",
    InteractionAction.SET_VALUE: "Value",
    InteractionAction.SELECT: "SelectionItem",
    InteractionAction.TOGGLE: "Toggle",
    InteractionAction.EXPAND: "ExpandCollapse",
    InteractionAction.COLLAPSE: "ExpandCollapse",
}
_FOCUS_ACK_ATTEMPTS = 20
_FOCUS_ACK_DELAY_SECONDS = 0.05


class PywinautoUIABackend(_BasePywinautoUIABackend):
    """Incumbent backend with provider-native exact focus observation."""

    def _bind_focused_element_info(
        self,
        hwnd: int,
        focused_info,
    ) -> tuple[tuple[int, ...], int] | None:
        """Bind one provider element to exactly one tracked RuntimeId/generation."""

        if focused_info is None:
            return None
        focused_wrapper = SimpleNamespace(element_info=focused_info)
        focused_runtime_id = self._runtime_id(focused_info)
        matches: list[UIAControlRecord] = []
        for wrapper, record in self._pairs(hwnd, "control"):
            same = self._same_element(wrapper, focused_wrapper)
            if same is True:
                matches.append(record)
            elif same is None and record.runtime_id == focused_runtime_id:
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

    @staticmethod
    def _native_focused_element_info(hwnd: int, element_info_type):
        """Read target GUI-thread keyboard focus and expose it through UIA.

        This is a read-only fallback for providers whose GetFocusedElement result
        cannot be rebound on hosted Windows. It never accepts an HWND as Nika
        identity: the returned UIA element must still pass CompareElements against
        the tracked RuntimeId/generation before it can acknowledge focus.
        """

        if os.name != "nt":
            return None
        try:
            import ctypes
            from ctypes import wintypes

            class GUITHREADINFO(ctypes.Structure):
                _fields_ = [
                    ("cbSize", wintypes.DWORD),
                    ("flags", wintypes.DWORD),
                    ("hwndActive", wintypes.HWND),
                    ("hwndFocus", wintypes.HWND),
                    ("hwndCapture", wintypes.HWND),
                    ("hwndMenuOwner", wintypes.HWND),
                    ("hwndMoveSize", wintypes.HWND),
                    ("hwndCaret", wintypes.HWND),
                    ("rcCaret", wintypes.RECT),
                ]

            user32 = ctypes.windll.user32
            thread_id = int(user32.GetWindowThreadProcessId(hwnd, None) or 0)
            if not thread_id:
                return None
            info = GUITHREADINFO()
            info.cbSize = ctypes.sizeof(info)
            if not user32.GetGUIThreadInfo(thread_id, ctypes.byref(info)):
                return None
            focused_hwnd = int(info.hwndFocus or 0)
            if not focused_hwnd:
                return None
            return element_info_type(focused_hwnd)
        except Exception as exc:  # noqa: BLE001 - provider/OS focus query is fail-closed
            logger.debug("native HWND focus query unavailable: %r", exc)
            return None

    def focused_identity(
        self,
        hwnd: int,
    ) -> tuple[tuple[int, ...], int] | None:
        """Map authoritative keyboard focus to the exact live Nika identity.

        Tree-wide ``CurrentHasKeyboardFocus`` can lag or be omitted by a provider
        after successful ``SetFocus``. First use pywinauto's UIA GetFocusedElement.
        If that result cannot be bound, query the exact target window's GUI thread
        for its keyboard-focus HWND, create a UIA element through the same live
        pywinauto element-info provider, and bind it with UIA CompareElements.
        No name, bounds, coordinates, or RuntimeId-only fallback is used.
        """

        try:
            window = self._window(hwnd)
        except Exception as exc:  # noqa: BLE001 - transient provider focus query
            logger.debug("UIA target window unavailable for focus query: %r", exc)
            return None

        element_info_type = type(window.element_info)
        try:
            get_active = getattr(element_info_type, "get_active", None)
            if callable(get_active):
                focused_info = get_active()
                identity = self._bind_focused_element_info(hwnd, focused_info)
                if identity is not None:
                    return identity
            else:
                logger.debug(
                    "UIA element-info provider exposes no get_active() focus query"
                )
        except AmbiguousTargetError:
            raise
        except Exception as exc:  # noqa: BLE001 - transient provider focus query
            logger.debug("UIA GetFocusedElement unavailable: %r", exc)

        focused_info = self._native_focused_element_info(hwnd, element_info_type)
        return self._bind_focused_element_info(hwnd, focused_info)


class WindowsUIAInteractionAdapter(_BaseWindowsUIAInteractionAdapter):
    """Incumbent adapter plus exact live semantic revalidation before effects."""

    def __init__(
        self,
        *,
        process_id: int,
        window_title: str | None = None,
        native_handle: int | None = None,
        view: str = "control",
        backend: WindowsUIABackend | None = None,
    ) -> None:
        super().__init__(
            process_id=process_id,
            window_title=window_title,
            native_handle=native_handle,
            view=view,
            backend=backend if backend is not None else PywinautoUIABackend(),
        )

    def _semantic_authority(self, node: ControlNode) -> ControlNode:
        if type(node.node_id) is not str:
            raise ValueError("UIA control node_id must be an exact string")
        expected = self._semantic_by_node.get(node.node_id)
        if expected is None:
            raise StaleSnapshotError(
                "control does not belong to the current semantic observation"
            )
        return expected

    def _revalidate_action_authority(
        self,
        node: ControlNode,
        action: InteractionAction,
    ) -> tuple[
        int,
        tuple[int, ...],
        int,
        ControlNode,
        UIAControlRecord,
    ]:
        expected = self._semantic_authority(node)
        hwnd = self._live_hwnd()
        runtime_id, generation = self._control_identity(expected)
        matches = [
            record
            for record in self._backend_controls(hwnd)
            if record.runtime_id == runtime_id
            and record.element_generation == generation
        ]
        if not matches:
            raise StaleSnapshotError(
                "UIA action authority is stale: RuntimeId/generation is no longer live"
            )
        if len(matches) != 1:
            raise AmbiguousTargetError(
                "UIA action authority is ambiguous for the exact RuntimeId/generation"
            )

        live = matches[0]
        if live.role != expected.role or live.name != expected.name:
            raise StaleSnapshotError(
                "UIA semantic action authority changed: accessible role/name drifted"
            )
        if live.enabled != expected.enabled or live.visible != expected.visible:
            raise StaleSnapshotError(
                "UIA semantic action authority changed: enabled/visible state drifted"
            )
        if not live.enabled or not live.visible:
            raise UnsupportedInteractionError(
                "disabled/hidden controls cannot receive UIA action authority"
            )

        required_pattern = _REQUIRED_PATTERN.get(action)
        if required_pattern is not None:
            observed_patterns = self.pattern_capabilities(expected)
            if required_pattern not in observed_patterns:
                raise UnsupportedInteractionError(
                    f"{required_pattern} pattern was not present in validated semantic authority"
                )
            if required_pattern not in live.patterns:
                raise UnsupportedInteractionError(
                    f"{required_pattern} pattern changed before UIA effect; "
                    "semantic authority is stale"
                )

        return hwnd, runtime_id, generation, expected, live

    def _guarded_focus_effect(
        self,
        *,
        hwnd: int,
        runtime_id: tuple[int, ...],
        generation: int,
        fence: UIAControlRecord,
    ) -> None:
        guarded = getattr(self.backend, "guarded_focus", None)
        if not callable(guarded):
            raise UnsupportedInteractionError(
                "Windows UIA backend does not implement guarded focus authority"
            )
        guarded(hwnd, runtime_id, generation, fence)

    def _guarded_action_effect(
        self,
        *,
        hwnd: int,
        runtime_id: tuple[int, ...],
        generation: int,
        fence: UIAControlRecord,
        action: InteractionAction,
        value: str | None,
    ) -> None:
        guarded = getattr(self.backend, "guarded_action", None)
        if not callable(guarded):
            raise UnsupportedInteractionError(
                "Windows UIA backend does not implement guarded action authority"
            )
        guarded(
            hwnd,
            runtime_id,
            generation,
            fence,
            action,
            value,
        )

    def _exact_live_focus_match(
        self,
        *,
        hwnd: int,
        runtime_id: tuple[int, ...],
        generation: int,
    ) -> UIAControlRecord:
        matches = [
            record
            for record in self._backend_controls(hwnd)
            if record.runtime_id == runtime_id
            and record.element_generation == generation
        ]
        if not matches:
            raise StaleSnapshotError(
                "UIA focus authority is stale: RuntimeId/generation is no longer live"
            )
        if len(matches) != 1:
            raise AmbiguousTargetError(
                "UIA focus authority is ambiguous for the exact RuntimeId/generation"
            )
        return matches[0]

    def _await_focus_acknowledgement(
        self,
        expected: ControlNode,
        *,
        hwnd: int,
        runtime_id: tuple[int, ...],
        generation: int,
    ) -> None:
        """Wait read-only for provider focus acknowledgement of one exact effect."""

        expected_identity = (runtime_id, generation)
        last_focused: tuple[tuple[int, ...], int] | None = None
        for attempt in range(_FOCUS_ACK_ATTEMPTS):
            if self._live_hwnd() != hwnd:
                raise StaleSnapshotError(
                    "UIA focus authority is stale: target window identity changed"
                )
            live = self._exact_live_focus_match(
                hwnd=hwnd,
                runtime_id=runtime_id,
                generation=generation,
            )
            if live.role != expected.role or live.name != expected.name:
                raise StaleSnapshotError(
                    "UIA focus authority changed: accessible role/name drifted"
                )
            if live.enabled != expected.enabled or live.visible != expected.visible:
                raise StaleSnapshotError(
                    "UIA focus authority changed: enabled/visible state drifted"
                )
            if not live.enabled or not live.visible:
                raise UnsupportedInteractionError(
                    "disabled/hidden controls cannot receive UIA focus authority"
                )

            last_focused = self._backend_focused_identity(hwnd)
            if last_focused == expected_identity:
                return
            if attempt + 1 < _FOCUS_ACK_ATTEMPTS:
                time.sleep(_FOCUS_ACK_DELAY_SECONDS)

        raise StaleSnapshotError(
            "UIA focus verification timed out waiting for exact "
            f"RuntimeId/generation acknowledgement; last focused identity={last_focused!r}"
        )

    def focus(self, node: ControlNode) -> None:
        """Issue exactly one SetFocus effect and await exact provider acknowledgement."""

        if type(node) is not ControlNode:
            raise ValueError("UIA focus target must be an exact ControlNode")
        (
            hwnd,
            runtime_id,
            generation,
            expected,
            live,
        ) = self._revalidate_action_authority(
            node,
            InteractionAction.FOCUS,
        )
        self._guarded_focus_effect(
            hwnd=hwnd,
            runtime_id=runtime_id,
            generation=generation,
            fence=live,
        )
        self._await_focus_acknowledgement(
            expected,
            hwnd=hwnd,
            runtime_id=runtime_id,
            generation=generation,
        )

    @staticmethod
    def _restore_focus_semantics_match(
        live: UIAControlRecord,
        expected: ControlNode,
    ) -> bool:
        return (
            live.role == expected.role
            and live.name == expected.name
            and live.enabled == expected.enabled
            and live.visible == expected.visible
        )

    def restore_focus(self, node_id: str | None) -> bool:
        """Restore focus only while the captured semantic authority remains exact."""

        if node_id is None:
            return True
        if type(node_id) is not str:
            raise ValueError("UIA focus identity must be an exact string")
        identity = self._identity_by_node.get(node_id)
        expected = self._semantic_by_node.get(node_id)
        if identity is None or expected is None:
            return False
        runtime_id, generation = identity
        try:
            hwnd = self._live_hwnd()
            live = self._exact_live_focus_match(
                hwnd=hwnd,
                runtime_id=runtime_id,
                generation=generation,
            )
        except (TargetNotFoundError, StaleSnapshotError, AmbiguousTargetError):
            return False
        if not self._restore_focus_semantics_match(live, expected):
            return False
        if not live.enabled or not live.visible:
            return False

        try:
            self._guarded_focus_effect(
                hwnd=hwnd,
                runtime_id=runtime_id,
                generation=generation,
                fence=live,
            )
        except (
            TargetNotFoundError,
            StaleSnapshotError,
            AmbiguousTargetError,
            UnsupportedInteractionError,
            ValueError,
        ):
            return False

        for attempt in range(_FOCUS_ACK_ATTEMPTS):
            try:
                if self._live_hwnd() != hwnd:
                    return False
                live = self._exact_live_focus_match(
                    hwnd=hwnd,
                    runtime_id=runtime_id,
                    generation=generation,
                )
            except (TargetNotFoundError, StaleSnapshotError, AmbiguousTargetError):
                return False
            if not self._restore_focus_semantics_match(live, expected):
                return False
            if not live.enabled or not live.visible:
                return False
            if self._backend_focused_identity(hwnd) == identity:
                return True
            if attempt + 1 < _FOCUS_ACK_ATTEMPTS:
                time.sleep(_FOCUS_ACK_DELAY_SECONDS)
        return False

    def act(
        self,
        node: ControlNode,
        action: InteractionAction,
        value: str | None,
    ) -> None:
        if type(node) is not ControlNode:
            raise ValueError("UIA action target must be an exact ControlNode")
        if type(action) is not InteractionAction:
            raise ValueError("UIA action must be an exact InteractionAction")
        if action is InteractionAction.SET_VALUE:
            if type(value) is not str:
                raise ValueError("SET_VALUE requires an exact string value")
        elif value is not None:
            raise ValueError("only SET_VALUE accepts a UIA action value")
        if action is InteractionAction.FOCUS:
            self.focus(node)
            return

        (
            hwnd,
            runtime_id,
            generation,
            _,
            live,
        ) = self._revalidate_action_authority(
            node,
            action,
        )
        self._guarded_action_effect(
            hwnd=hwnd,
            runtime_id=runtime_id,
            generation=generation,
            fence=live,
            action=action,
            value=value,
        )


__all__ = [
    "PywinautoUIABackend",
    "UIABackendMeasurement",
    "UIAControlRecord",
    "UIAWindowRecord",
    "WindowsUIABackend",
    "WindowsUIAInteractionAdapter",
    "choose_measured_backend",
    "measure_observation",
]
