"""Strict Windows UIA adapter with live pre-effect semantic authority revalidation.

The incumbent implementation lives in ``windows_uia_adapter_impl`` unchanged so its
RuntimeId + generation identity and pywinauto tracking remain reviewable.  This
module adds the action-boundary fail-closed contract: an observed ControlNode may
cause an effect only while the exact live UIA identity still has the same semantic
role/name/actionability and the required UIA pattern.
"""

from __future__ import annotations

import time

from .domain import (
    AmbiguousTargetError,
    ControlNode,
    InteractionAction,
    StaleSnapshotError,
    UnsupportedInteractionError,
)
from .windows_uia_adapter_impl import (
    PywinautoUIABackend,
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


class WindowsUIAInteractionAdapter(_BaseWindowsUIAInteractionAdapter):
    """Incumbent adapter plus exact live semantic revalidation before effects."""

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

    def _await_focus_acknowledgement(
        self,
        node: ControlNode,
        *,
        hwnd: int,
        runtime_id: tuple[int, ...],
        generation: int,
    ) -> None:
        """Wait read-only for provider focus acknowledgement of one exact effect.

        Some Windows UIA providers acknowledge ``SetFocus`` before their focused
        identity property catches up.  The focus effect is therefore issued once;
        this helper only polls live authority for that exact RuntimeId/generation.
        Any replacement, ambiguity or semantic/actionability drift fails closed.
        """

        expected_identity = (runtime_id, generation)
        last_focused: tuple[tuple[int, ...], int] | None = None
        for attempt in range(_FOCUS_ACK_ATTEMPTS):
            if self._live_hwnd() != hwnd:
                raise StaleSnapshotError(
                    "UIA focus authority is stale: target window identity changed"
                )
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
            live = matches[0]
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

    def act(
        self,
        node: ControlNode,
        action: InteractionAction,
        value: str | None,
    ) -> None:
        if action is InteractionAction.SET_VALUE and value is None:
            raise ValueError("SET_VALUE requires a value")

        hwnd, runtime_id, generation = self._revalidate_action_authority(
            node,
            action,
        )
        if action is InteractionAction.FOCUS:
            self.backend.focus(hwnd, runtime_id, generation)
            self._await_focus_acknowledgement(
                node,
                hwnd=hwnd,
                runtime_id=runtime_id,
                generation=generation,
            )
            return

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
