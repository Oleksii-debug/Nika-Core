from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

from pydantic import ValidationError

from nika_core.kernel.action_registry import ActionRegistry, Keymap
from nika_core.ui.bridge_models import UIActionView, UICommand, UIResult

logger = logging.getLogger(__name__)

ActionHandler = Callable[[Mapping[str, Any]], UIResult | str | None]
StateProvider = Callable[[], Mapping[str, Any]]


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
        try:
            command = UICommand.model_validate(raw)
        except ValidationError:
            return UIResult(
                request_id=self._rejected_request_id(raw),
                status="rejected",
                message="Некоректна команда інтерфейсу.",
            ).model_dump()

        try:
            self._actions.get(command.action_id)
        except KeyError:
            return UIResult(
                request_id=command.request_id,
                status="rejected",
                message="Невідома дія інтерфейсу.",
            ).model_dump()

        handler = self._handlers.get(command.action_id)
        if handler is None:
            return UIResult(
                request_id=command.request_id,
                status="rejected",
                message="Ця дія недоступна в поточному контексті.",
            ).model_dump()

        try:
            outcome = handler(command.payload)
        except (KeyError, TypeError, ValueError) as exc:
            return UIResult(
                request_id=command.request_id,
                status="rejected",
                message=str(exc),
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
        return UIResult(
            request_id=command.request_id,
            status="completed",
            message=message,
        ).model_dump()

    @staticmethod
    def _rejected_request_id(raw: object) -> str:
        if type(raw) is not dict:
            return "invalid"
        request_id = raw.get("request_id")
        if type(request_id) is not str:
            return "invalid"
        if not request_id or len(request_id) > 120:
            return "invalid"
        return request_id

    def get_state(self) -> dict[str, Any]:
        if self._state_provider is None:
            return {"ok": False, "message": "Джерело стану програми недоступне."}
        try:
            state = dict(self._state_provider())
        except (KeyError, TypeError, ValueError) as exc:
            return {"ok": False, "message": str(exc)}
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

    def set_binding(self, action_id: str, binding: str | None) -> dict[str, Any]:
        try:
            self._keymap.set_binding(action_id, binding)
        except (KeyError, TypeError, ValueError):
            return {
                "ok": False,
                "message": (
                    "Не вдалося зберегти комбінацію: "
                    "перевірте дію, формат і конфлікти."
                ),
            }
        except Exception as exc:  # noqa: BLE001 - final pywebview transport boundary
            return self._unexpected_keymap_failure("set_binding", exc)
        return {"ok": True, "message": "Комбінацію клавіш збережено."}

    def restore_default(self, action_id: str) -> dict[str, Any]:
        try:
            self._keymap.restore_default(action_id)
        except KeyError:
            return {
                "ok": False,
                "message": (
                    "Не вдалося відновити комбінацію за замовчуванням: "
                    "невідома дія."
                ),
            }
        except (TypeError, ValueError):
            return {
                "ok": False,
                "message": (
                    "Не вдалося відновити комбінацію за замовчуванням: "
                    "перевірте конфлікти карти клавіш."
                ),
            }
        except Exception as exc:  # noqa: BLE001 - final pywebview transport boundary
            return self._unexpected_keymap_failure("restore_default", exc)
        return {
            "ok": True,
            "message": "Комбінацію за замовчуванням відновлено.",
        }

    def export_keymap(self) -> dict[str, Any]:
        try:
            data = self._keymap.export_json()
        except (KeyError, TypeError, ValueError):
            return {
                "ok": False,
                "message": (
                    "Не вдалося експортувати карту клавіш: "
                    "перевірте збережені налаштування."
                ),
            }
        except Exception as exc:  # noqa: BLE001 - final pywebview transport boundary
            return self._unexpected_keymap_failure("export_keymap", exc)
        return {
            "ok": True,
            "data": data,
            "message": "Карту клавіш експортовано.",
        }

    def import_keymap(self, data: str) -> dict[str, Any]:
        if not isinstance(data, str):
            return {"ok": False, "message": "Карта клавіш має бути текстом JSON."}
        try:
            self._keymap.import_json(data)
        except (KeyError, TypeError, ValueError):
            return {
                "ok": False,
                "message": (
                    "Не вдалося імпортувати карту клавіш: "
                    "перевірте JSON, дії та конфлікти."
                ),
            }
        except Exception as exc:  # noqa: BLE001 - final pywebview transport boundary
            return self._unexpected_keymap_failure("import_keymap", exc)
        return {"ok": True, "message": "Карту клавіш імпортовано."}

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
