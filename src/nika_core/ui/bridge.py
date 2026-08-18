from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from pydantic import ValidationError

from nika_core.kernel.action_registry import ActionRegistry, Keymap
from nika_core.ui.bridge_models import UIActionView, UICommand, UIResult

ActionHandler = Callable[[Mapping[str, Any]], UIResult | str | None]


class UIActionBridge:
    """Narrow validated pywebview facade.

    JavaScript can only invoke registered Nika action IDs or explicit keymap methods.
    No arbitrary Python object, filesystem, shell, or provider object is exposed.
    Handler implementations must translate internal failures into typed/user-safe
    ValueError or TypeError failures; unexpected programmer errors are not hidden here.
    """

    def __init__(
        self,
        actions: ActionRegistry,
        keymap: Keymap,
        handlers: Mapping[str, ActionHandler] | None = None,
    ) -> None:
        self._actions = actions
        self._keymap = keymap
        self._handlers = dict(handlers or {})

    def dispatch(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        try:
            command = UICommand.model_validate(raw)
        except ValidationError as exc:
            return UIResult(
                request_id=str(raw.get("request_id", "invalid")),
                status="rejected",
                message=f"Invalid UI command: {exc.errors()[0]['msg']}",
            ).model_dump()

        try:
            self._actions.get(command.action_id)
        except KeyError:
            return UIResult(
                request_id=command.request_id,
                status="rejected",
                message=f"Unknown action: {command.action_id}",
            ).model_dump()

        handler = self._handlers.get(command.action_id)
        if handler is None:
            return UIResult(
                request_id=command.request_id,
                status="rejected",
                message=f"Action is not available in this UI context: {command.action_id}",
            ).model_dump()

        try:
            outcome = handler(command.payload)
        except (TypeError, ValueError) as exc:
            return UIResult(
                request_id=command.request_id,
                status="rejected",
                message=str(exc),
            ).model_dump()

        if isinstance(outcome, UIResult):
            if outcome.request_id != command.request_id:
                return UIResult(
                    request_id=command.request_id,
                    status=outcome.status,
                    message=outcome.message,
                    focus_id=outcome.focus_id,
                ).model_dump()
            return outcome.model_dump()
        return UIResult(
            request_id=command.request_id,
            status="completed",
            message="" if outcome is None else str(outcome),
        ).model_dump()

    def list_actions(self) -> list[dict[str, Any]]:
        return [
            UIActionView(
                action_id=action.action_id,
                label=action.label,
                category=action.category,
                scope=action.scope,
                binding=self._keymap.resolve(action.action_id),
                may_be_unbound=action.may_be_unbound,
            ).model_dump()
            for action in self._actions.all()
        ]

    def set_binding(self, action_id: str, binding: str | None) -> dict[str, Any]:
        try:
            self._keymap.set_binding(action_id, binding)
        except (KeyError, TypeError, ValueError) as exc:
            return {"ok": False, "message": str(exc)}
        return {"ok": True, "message": "Shortcut saved."}

    def restore_default(self, action_id: str) -> dict[str, Any]:
        try:
            self._keymap.restore_default(action_id)
        except KeyError as exc:
            return {"ok": False, "message": str(exc)}
        return {"ok": True, "message": "Default shortcut restored."}

    def export_keymap(self) -> dict[str, Any]:
        return {"ok": True, "data": self._keymap.export_json(), "message": "Shortcut map exported."}

    def import_keymap(self, data: str) -> dict[str, Any]:
        if not isinstance(data, str):
            return {"ok": False, "message": "Shortcut map must be JSON text."}
        try:
            self._keymap.import_json(data)
        except (KeyError, TypeError, ValueError) as exc:
            return {"ok": False, "message": str(exc)}
        return {"ok": True, "message": "Shortcut map imported."}
