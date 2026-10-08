from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any
from unicodedata import category

from pydantic import ValidationError

from nika_core.kernel.action_registry import ActionRegistry, Keymap
from nika_core.ui.bridge_models import UIActionView, UICommand, UIResult
from nika_core.ui.payload_safety import validate_ui_payload

logger = logging.getLogger(__name__)

ActionHandler = Callable[[Mapping[str, Any]], UIResult | str | None]
StateProvider = Callable[[], dict[str, Any]]


class UIActionBridge:
    """Narrow validated pywebview facade.

    JavaScript can only invoke registered Nika action IDs, explicit keymap methods,
    or a read-only product-state snapshot supplied by the desktop facade.
    No arbitrary Python object, filesystem, shell, or provider object is exposed.
    """

    def __init__(
        self,
        actions: ActionRegistry,
        keymap: Keymap,
        handlers: Mapping[str, ActionHandler] | None = None,
        state_provider: StateProvider | None = None,
    ) -> None:
        self._actions = actions
        self._keymap = keymap
        self._handlers = dict(handlers or {})
        self._state_provider = state_provider

    def dispatch(self, raw: object) -> dict[str, Any]:
        # A pywebview command must originate as a plain JSON object. Do not
        # introspect arbitrary mappings/objects at this security boundary.
        if type(raw) is not dict:
            return UIResult(
                request_id="invalid",
                status="rejected",
                message="Invalid UI command: expected a plain JSON object.",
            ).model_dump()
        try:
            command = UICommand.model_validate(raw)
        except ValidationError as exc:
            return UIResult(
                request_id=self._rejected_request_id(raw),
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
        except Exception as exc:  # noqa: BLE001 - final pywebview transport boundary
            logger.error(
                "UI action lookup failed: action_id=%s exception_type=%s",
                command.action_id,
                type(exc).__name__,
            )
            return UIResult(
                request_id=command.request_id,
                status="failed",
                message="Не вдалося виконати дію через внутрішню помилку.",
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
        except (KeyError, TypeError, ValueError) as exc:
            return UIResult(
                request_id=command.request_id,
                status="rejected",
                message=self._safe_input_error_message(exc),
            ).model_dump()
        except Exception as exc:  # noqa: BLE001 - final pywebview transport boundary
            # This is the final pywebview boundary. Keep unexpected backend failures inside
            # a serializable result without swallowing process-shutdown BaseException signals.
            logger.error(
                "UI action failed: action_id=%s exception_type=%s",
                command.action_id,
                type(exc).__name__,
            )
            return UIResult(
                request_id=command.request_id,
                status="failed",
                message="Не вдалося виконати дію через внутрішню помилку.",
            ).model_dump()

        if isinstance(outcome, UIResult):
            if (
                not self._safe_accessible_status(outcome.message)
                or not self._safe_focus_target(outcome.focus_id)
            ):
                return self._unsafe_handler_status(command.action_id, command.request_id)
            if outcome.request_id != command.request_id:
                return UIResult(
                    request_id=command.request_id,
                    status=outcome.status,
                    message=outcome.message,
                    focus_id=outcome.focus_id,
                ).model_dump()
            return outcome.model_dump()
        if outcome is None:
            message = ""
        elif type(outcome) is str:
            message = outcome
        else:
            logger.error(
                "UI action returned invalid result: action_id=%s result_type=%s",
                command.action_id,
                type(outcome).__name__,
            )
            return UIResult(
                request_id=command.request_id,
                status="failed",
                message="Не вдалося виконати дію через внутрішню помилку.",
            ).model_dump()
        if not self._safe_accessible_status(message):
            return self._unsafe_handler_status(command.action_id, command.request_id)
        return UIResult(
            request_id=command.request_id,
            status="completed",
            message=message,
        ).model_dump()

    @staticmethod
    def _safe_accessible_status(message: object) -> bool:
        """Keep returned action text bounded and single-line for NVDA/status logs."""
        if type(message) is not str:
            return False
        try:
            if len(message.encode("utf-8")) > 2048:
                return False
        except UnicodeEncodeError:
            return False
        return not any(category(char) in {"Cc", "Cf", "Cs"} for char in message)

    @staticmethod
    def _safe_focus_target(focus_id: object) -> bool:
        if focus_id is None:
            return True
        return (
            type(focus_id) is str
            and 1 <= len(focus_id) <= 120
            and all(
                char.isascii() and (char.isalnum() or char in "-_.:")
                for char in focus_id
            )
        )

    @staticmethod
    def _unsafe_handler_status(action_id: str, request_id: str) -> dict[str, Any]:
        logger.error("UI action returned unsafe status: action_id=%s", action_id)
        return UIResult(
            request_id=request_id,
            status="failed",
            message="Не вдалося виконати дію через некоректний текст стану.",
        ).model_dump()

    @staticmethod
    def _rejected_request_id(raw: object) -> str:
        if type(raw) is not dict:
            return "invalid"
        request_id = raw.get("request_id")
        if type(request_id) is not str:
            return "invalid"
        if (
            not 1 <= len(request_id) <= 120
            or not all(
                char.isascii() and (char.isalnum() or char in "-_.:")
                for char in request_id
            )
        ):
            return "invalid"
        return request_id

    def get_state(self) -> dict[str, Any]:
        if self._state_provider is None:
            return {"ok": False, "message": "Desktop state provider is unavailable."}
        try:
            # The state provider is host-owned, but its nested data may include plugin
            # projections. Return only bounded, detached JSON to the WebView transport.
            raw_state = self._state_provider()
            # Only the canonical built-in dict is admitted. Coercing arbitrary
            # providers with dict(...) can run user-defined iteration or lose
            # noncanonical state semantics before the bounded JSON snapshot.
            if type(raw_state) is not dict:
                raise ValueError("desktop state must be a plain JSON object")
            state = validate_ui_payload(raw_state)
        except Exception as exc:  # noqa: BLE001 - final pywebview transport boundary
            logger.error(
                "Desktop state provider failed: exception_type=%s",
                type(exc).__name__,
            )
            return {
                "ok": False,
                "message": "Не вдалося отримати стан програми через внутрішню помилку.",
            }
        return {"ok": True, "state": state}

    def list_actions(self) -> list[dict[str, Any]]:
        try:
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
        except Exception as exc:  # noqa: BLE001 - final pywebview transport boundary
            logger.error(
                "UI action list failed: exception_type=%s",
                type(exc).__name__,
            )
            raise RuntimeError(
                "Не вдалося завантажити список дій через внутрішню помилку."
            ) from None

    @staticmethod
    def _bounded_keymap_text(value: object, *, max_bytes: int = 1_048_576) -> bool:
        # Inspect exact built-in strings only; encoding catches invalid Unicode
        # without invoking behavioral subclasses at the WebView boundary.
        # Unicode's UTF-8 encoding never uses fewer bytes than code points.
        # Refuse huge inputs before allocating a second (encoded) copy.
        if type(value) is not str or not value or len(value) > max_bytes:
            return False
        try:
            return len(value.encode("utf-8")) <= max_bytes
        except UnicodeEncodeError:
            return False

    def set_binding(self, action_id: str, binding: str | None) -> dict[str, Any]:
        # These methods are directly callable by pywebview. Bound all input
        # before invoking the stateful Keymap resolver or persistence layer.
        if not self._bounded_keymap_text(action_id, max_bytes=120) or (
            binding is not None
            and not (type(binding) is str and (
                not binding or self._bounded_keymap_text(binding, max_bytes=256)
            ))
        ):
            return {"ok": False, "message": "Action or shortcut text is invalid or too long."}
        try:
            self._keymap.set_binding(action_id, binding)
        except (KeyError, TypeError, ValueError) as exc:
            return {"ok": False, "message": self._safe_input_error_message(exc)}
        except Exception as exc:  # noqa: BLE001 - final pywebview transport boundary
            return self._unexpected_keymap_failure("set_binding", exc)
        return {"ok": True, "message": "Shortcut saved."}

    def restore_default(self, action_id: str) -> dict[str, Any]:
        if not self._bounded_keymap_text(action_id, max_bytes=120):
            return {"ok": False, "message": "Action ID must be bounded plain text."}
        try:
            self._keymap.restore_default(action_id)
        except (KeyError, TypeError, ValueError) as exc:
            return {"ok": False, "message": self._safe_input_error_message(exc)}
        except Exception as exc:  # noqa: BLE001 - final pywebview transport boundary
            return self._unexpected_keymap_failure("restore_default", exc)
        return {"ok": True, "message": "Default shortcut restored."}

    def export_keymap(self) -> dict[str, Any]:
        try:
            data = self._keymap.export_json()
        except Exception as exc:  # noqa: BLE001 - final pywebview transport boundary
            return self._unexpected_keymap_failure("export_keymap", exc)
        if not self._bounded_keymap_text(data):
            return {
                "ok": False,
                "message": (
                    "Не вдалося експортувати комбінації клавіш: "
                    "некоректний розмір або текст."
                ),
            }
        return {"ok": True, "data": data, "message": "Shortcut map exported."}

    def import_keymap(self, data: str) -> dict[str, Any]:
        # json.loads in the canonical Keymap must never receive an unbounded
        # WebView string; failed admission must have zero persisted effects.
        if not self._bounded_keymap_text(data):
            return {
                "ok": False,
                "message": "Файл комбінацій клавіш має некоректний розмір або текст.",
            }
        try:
            self._keymap.import_json(data)
        except (KeyError, TypeError, ValueError) as exc:
            return {"ok": False, "message": self._safe_input_error_message(exc)}
        except Exception as exc:  # noqa: BLE001 - final pywebview transport boundary
            return self._unexpected_keymap_failure("import_keymap", exc)
        return {"ok": True, "message": "Shortcut map imported."}

    @staticmethod
    def _safe_input_error_message(exc: Exception) -> str:
        """Project only bounded plain-text admission errors to assistive status.

        A built-in ValueError can still carry an object in args whose __str__
        invokes provider code or exposes secrets. Never stringify that object.
        """
        fallback = "Некоректний запит або стан операції."
        if type(exc) not in (KeyError, TypeError, ValueError):
            return fallback
        if len(exc.args) != 1 or type(exc.args[0]) is not str:
            return fallback
        message = exc.args[0]
        try:
            if not message or len(message.encode("utf-8")) > 2048:
                return fallback
        except UnicodeEncodeError:
            return fallback
        if any(category(char) in {"Cc", "Cf", "Cs"} for char in message):
            return fallback
        return message

    @staticmethod
    def _unexpected_keymap_failure(operation: str, exc: Exception) -> dict[str, Any]:
        logger.error(
            "UI keymap operation failed: operation=%s exception_type=%s",
            operation,
            type(exc).__name__,
        )
        return {
            "ok": False,
            "message": "Не вдалося змінити комбінації клавіш через внутрішню помилку.",
        }
