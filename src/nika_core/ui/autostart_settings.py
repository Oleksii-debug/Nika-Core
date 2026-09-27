from __future__ import annotations

import sqlite3
import threading
from collections.abc import Mapping
from typing import Any

from nika_core.kernel.audit import AuditLog
from nika_core.ui.bridge_models import UIResult
from nika_core.windows_autostart import AutostartState, WindowsAutostartService

_MESSAGES = {
    "enabled": "Автозапуск увімкнено для цього застосунку.",
    "disabled": "Автозапуск вимкнено.",
    "stale": (
        "Збережено застарілий або інший запис автозапуску. "
        "Позначте прапорець і збережіть, щоб прив’язати поточний застосунок, "
        "або зніміть позначку і збережіть, щоб прибрати запис."
    ),
    "unavailable": "Автозапуск доступний лише у зібраному застосунку Windows.",
    "error": "Не вдалося прочитати автозапуск. Перечитайте стан або перевірте доступ Windows.",
}
_SETTING_ERRORS = (OSError, RuntimeError, ValueError, sqlite3.Error)


class AutostartSettings:
    """Thin command/query adapter; the incumbent service alone owns the Run key.

    No command line, executable path or registry value crosses the UI boundary.
    Reading settings never enables autostart or starts/replays a task.
    """

    def __init__(self, service: WindowsAutostartService | None, audit: AuditLog) -> None:
        self._service = service
        self._audit = audit
        self._lock = threading.RLock()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            state = "unavailable"
            if self._service is not None:
                try:
                    state = self._service.status().state.value
                except _SETTING_ERRORS:
                    state = "error"
            return {
                "schema_version": 1,
                "state": state,
                "can_change": state in {"enabled", "disabled", "stale"},
                "message": _MESSAGES[state],
            }

    def configure(self, payload: Mapping[str, Any]) -> UIResult:
        if set(payload) != {"enabled"} or type(payload["enabled"]) is not bool:
            return self._result("rejected", "Потрібна лише явна позначка автозапуску: так або ні.")
        with self._lock:
            if self._service is None:
                return self._result("rejected", _MESSAGES["unavailable"])
            enabled = payload["enabled"]
            try:
                # Record intent before an OS mutation. A failed audit stops the write.
                self._record("requested", enabled)
                status = self._service.enable() if enabled else self._service.disable()
                expected = AutostartState.ENABLED if enabled else AutostartState.DISABLED
                if status.state is not expected:
                    raise RuntimeError("Autostart acknowledgement did not match intent")
                self._record("confirmed", enabled)
            except _SETTING_ERRORS:
                # OS errors may contain account paths or a foreign command. Never echo them.
                # A write may already have happened; only a fresh query owns current truth.
                return self._result(
                    "failed",
                    "Не вдалося підтвердити зміну автозапуску. Перечитайте стан перед повтором.",
                )
            return self._result("completed", "Налаштування автозапуску збережено.")

    def refresh(self, payload: Mapping[str, Any]) -> UIResult:
        if payload:
            return self._result("rejected", "Перечитування автозапуску не приймає параметрів.")
        state = self.snapshot()
        return self._result(
            "failed" if state["state"] == "error" else "completed",
            "Перечитування автозапуску не вдалося."
            if state["state"] == "error"
            else "Стан автозапуску перечитано.",
        )

    def _record(self, outcome: str, enabled: bool) -> None:
        self._audit.append(
            event_type=f"settings.autostart.{outcome}",
            entity_type="application_setting",
            entity_id="windows.autostart",
            payload={"enabled": enabled},
        )

    @staticmethod
    def _result(status: str, message: str) -> UIResult:
        return UIResult(
            request_id="desktop-handler",
            status=status,
            message=message,
            focus_id="autostart-enabled",
        )
