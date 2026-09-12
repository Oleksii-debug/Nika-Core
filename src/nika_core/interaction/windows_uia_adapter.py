"""Strict Windows UIA adapter with live pre-effect semantic authority revalidation.

The incumbent implementation lives in ``windows_uia_adapter_impl`` unchanged so its
RuntimeId + generation identity and pywinauto tracking remain reviewable. This
module adds the action-boundary fail-closed contract and a provider-native focus
read that maps UIA ``GetFocusedElement`` back to the incumbent exact identity.
"""

from __future__ import annotations

import logging
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

    def focused_identity(
        self,
        hwnd: int,
    ) -> tuple[tuple[int, ...], int] | None:
        """Map UIA GetFocusedElement to the exact live Nika identity.

        Tree-wide ``CurrentHasKeyboardFocus`` can lag or be omitted by a provider
        after successful ``SetFocus``. UIA exposes the actual input-focus element
        directly. This remains read-only: the focused element is accepted only
        when UIA CompareElements binds it to exactly one current tracked
        RuntimeId/generation. No name, bounds, coordinates, or RuntimeId-only
        fallback is used.
        """

        try:
            window = self._window(hwnd)
            get_active = getattr(type(window.element_info), "get_active", None)
            if not callable(get_active):
                logger.debug(
                    "UIA element-info provider exposes no get_active() focus query"
                )
                return None
            focused_info = get_active()
            if focused_info is None:
                return None
            focused_wrapper = SimpleNamespace(element_info=focused_info)
        except Exception as exc:  # noqa: BLE001 - transient provider focus query
            logger.debug("UIA GetFocusedElement unavailable: %r", exc)
            return None

        focused_runtime_id = self._runtime_id(focused_wrapper.element_info)
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
            backend=backend or PywinautoUIABackend(),
        )

    def _revalidate_action_authority(
        self,
        node: ControlNode,
        action: InteractionAction,
    ) -> tuple[int, tuple[int, ...], int]:
        hwnd = self._live_hwnd()
        runtime_id, generation = self._control_identity(node)
        matches = [
            record
            for record in self.backend.enumerate_controls(hwnd, self.view)
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
        if live.role != node.role or live.name != node.name:
            raise StaleSnapshotError(
                "UIA semantic action authority changed: accessible role/name drifted"
            )
        if live.enabled != node.enabled or live.visible != node.visible:
            raise StaleSnapshotError(
                "UIA semantic action authority changed: enabled/visible state drifted"
            )
        if not live.enabled or not live.visible:
            raise UnsupportedInteractionError(
                "disabled/hidden controls cannot receive UIA action authority"
            )

        required_pattern = _REQUIRED_PATTERN.get(action)
        if required_pattern is not None:
            observed_patterns = self.pattern_capabilities(node)
            if required_pattern not in observed_patterns:
                raise UnsupportedInteractionError(
                    f"{required_pattern} pattern was not present in validated semantic authority"
                )
            if required_pattern not in live.patterns:
                raise UnsupportedInteractionError(
                    f"{required_pattern} pattern changed before UIA effect; semantic authority is stale"
                )

        return hwnd, runtime_id, generation

    def _exact_live_focus_match(
        self,
        *,
        hwnd: int,
        runtime_id: tuple[int, ...],
        generation: int,
    ) -> UIAControlRecord:
        matches = [
            record
            for record in self.backend.enumerate_controls(hwnd, self.view)
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
        node: ControlNode,
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
            if live.role != node.role or live.name != node.name:
                raise StaleSnapshotError(
                    "UIA focus authority changed: accessible role/name drifted"
                )
            if live.enabled != node.enabled or live.visible != node.visible:
                raise StaleSnapshotError(
                    "UIA focus authority changed: enabled/visible state drifted"
                )
            if not live.enabled or not live.visible:
                raise UnsupportedInteractionError(
                    "disabled/hidden controls cannot receive UIA focus authority"
                )

            last_focused = self.backend.focused_identity(hwnd)
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

        hwnd, runtime_id, generation = self._revalidate_action_authority(
            node,
            InteractionAction.FOCUS,
        )
        self.backend.focus(hwnd, runtime_id, generation)
        self._await_focus_acknowledgement(
            node,
            hwnd=hwnd,
            runtime_id=runtime_id,
            generation=generation,
        )

    def restore_focus(self, node_id: str | None) -> bool:
        """Restore one exact prior identity with bounded read-only acknowledgement."""

        if node_id is None:
            return True
        identity = self._identity_by_node.get(node_id)
        if identity is None:
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
        if not live.enabled or not live.visible:
            return False

        try:
            self.backend.focus(hwnd, runtime_id, generation)
        except (TargetNotFoundError, StaleSnapshotError):
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
            if not live.enabled or not live.visible:
                return False
            if self.backend.focused_identity(hwnd) == identity:
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
        if action is InteractionAction.SET_VALUE and value is None:
            raise ValueError("SET_VALUE requires a value")
        if action is InteractionAction.FOCUS:
            self.focus(node)
            return

        hwnd, runtime_id, generation = self._revalidate_action_authority(
            node,
            action,
        )
        method = {
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
