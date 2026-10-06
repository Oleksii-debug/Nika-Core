from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from pydantic_settings import SettingsError

from nika_core.activity_report import DailyActivityReportService
from nika_core.builder.repository import AgentDefinitionRepository
from nika_core.config import AppConfig
from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.action_registry import Keymap
from nika_core.kernel.agent_registry import AgentRegistry
from nika_core.kernel.audit import AuditLog
from nika_core.kernel.checkpoint import CheckpointService
from nika_core.kernel.default_actions import build_default_action_registry
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState
from nika_core.kernel.workspace_registry import WorkspaceRegistry
from nika_core.packaged_agent_builder import (
    PackagedAgentBuilderDraftHandler,
    PackagedAgentBuilderStateProjector,
)
from nika_core.packaging.pf11_evidence import require_packaged_pf11_evidence
from nika_core.product_command.command_center import ProductCommandCenter
from nika_core.product_command.product_project_adapter import ProductProjectCommandService
from nika_core.product_command.routing import route_command
from nika_core.product_factory_packaged_journey import (
    PackagedProductCommandRouter,
    PackagedProductSelectionStore,
    PackagedProductStateProvider,
    product_project_identity,
)
from nika_core.product_project import ProductProjectRepository
from nika_core.ui.bridge import UIActionBridge
from nika_core.ui.bridge_models import UIResult
from nika_core.ui.desktop_backend import DesktopBackend
from nika_core.training_runtime import TrainingStatusService
from nika_core.ui.shell import launch_windows_shell, preflight_windows_shell
from nika_core.v01_cloud_model_permission import (
    CloudModelGrantRequest,
    CloudModelPermissionConfirm,
    CloudModelPermissionDenied,
    V01CloudModelPermissionService,
)
from nika_core.v01_model_settings import ModelSetupError, V01ModelSettings
from nika_core.v01_packaged_team_runtime import V01PackagedThreeAgentRuntime
from nika_core.v01_packaged_team_state import V01PackagedTeamStateProvider
from nika_core.v01_source_settings import V01SourceSettings
from nika_core.windows_autostart import WindowsAutostartService


class _StartupRecoveryInventoryError(RuntimeError):
    """Fail-closed packaged startup boundary; never exposes raw recovery diagnostics."""


_TERMINAL_TASK_STATES = frozenset(
    {
        TaskState.COMPLETED,
        TaskState.FAILED,
        TaskState.CANCELLED,
        TaskState.ARCHIVED,
    }
)


def _focus(focus_id: str, message: str) -> UIResult:
    return UIResult(
        request_id="desktop-handler",
        status="completed",
        message=message,
        focus_id=focus_id,
    )


def _confirm_cloud_model_on_windows(request: CloudModelGrantRequest) -> bool:
    """Use a standard native Windows dialog for explicit task-scoped cloud consent."""

    if sys.platform != "win32":
        return False
    try:
        import ctypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        private_text = "так" if request.private_data_allowed else "ні"
        message = (
            "Це завдання надсилатиме дані до зовнішнього API.\n\n"
            f"Постачальник: {request.provider_id}\n"
            f"Модель: {request.model}\n"
            f"Хост: {request.network_host}\n"
            f"Приватні дані дозволено: {private_text}\n\n"
            "Дозволити мережеві звернення цього завдання до цієї моделі? "
            "Дозвіл прив'язаний лише до цього завдання і діє до 24 годин."
        )
        flags = 0x00000004 | 0x00000030 | 0x00000100 | 0x00010000
        result = int(
            user32.MessageBoxW(
                None,
                message,
                "Nika Core — дозвіл зовнішньої моделі",
                flags,
            )
        )
    except Exception as exc:  # noqa: BLE001 - native confirmation must fail closed
        logging.getLogger(__name__).error(
            "Cloud model confirmation failed: exception_type=%s",
            type(exc).__name__,
        )
        return False
    return result == 6


def _daily_activity_report_result(
    service: DailyActivityReportService,
    *,
    day_provider: Callable[[], date] | None = None,
) -> UIResult:
    try:
        day = datetime.now(UTC).date() if day_provider is None else day_provider()
        if type(day) is not date:
            raise TypeError("activity report day must be an exact date")
        report = service.build_utc_day(day)
        message = report.render_text()
    except Exception as exc:  # noqa: BLE001 - packaged boundary must fail closed
        logging.getLogger(__name__).error(
            "Daily activity report failed: exception_type=%s",
            type(exc).__name__,
        )
        return UIResult(
            request_id="desktop-handler",
            status="failed",
            message="Не вдалося сформувати щоденний звіт активності.",
            focus_id="logs-heading",
        )
    return UIResult(
        request_id="desktop-handler",
        status="completed",
        message=message,
        focus_id="logs-heading",
    )


def _training_status_result(
    service: TrainingStatusService,
    task_id: str,
) -> UIResult:
    try:
        status = service.read(task_id)
        message = (
            status.render_text()
            if status is not None
            else (
                "Для цього task_id немає збереженого durable checkpoint "
                "стану навчання."
            )
        )
    except Exception as exc:  # noqa: BLE001 - packaged boundary must fail closed
        logging.getLogger(__name__).error(
            "Training status read failed: exception_type=%s",
            type(exc).__name__,
        )
        return UIResult(
            request_id="desktop-handler",
            status="failed",
            message="Не вдалося безпечно прочитати стан навчання.",
            focus_id="logs-heading",
        )
    return UIResult(
        request_id="desktop-handler",
        status="completed",
        message=message,
        focus_id="logs-heading",
    )


def _current_task_status_result(queue: TaskQueue) -> UIResult:
    try:
        unfinished = tuple(
            record
            for record in queue.list_recent(limit=50)
            if record.state not in _TERMINAL_TASK_STATES
        )
    except Exception as exc:  # noqa: BLE001 - packaged boundary must fail closed
        logging.getLogger(__name__).error(
            "Current task status read failed: exception_type=%s",
            type(exc).__name__,
        )
        return UIResult(
            request_id="desktop-handler",
            status="failed",
            message="Не вдалося безпечно прочитати стан поточного завдання.",
            focus_id="tasks-heading",
        )
    if not unfinished:
        return UIResult(
            request_id="desktop-handler",
            status="completed",
            message="Немає незавершеного завдання.",
            focus_id="tasks-heading",
        )
    if len(unfinished) > 1:
        return UIResult(
            request_id="desktop-handler",
            status="rejected",
            message=(
                f"Є кілька незавершених завдань ({len(unfinished)}); "
                "відкрийте список «Завдання» для явного вибору."
            ),
            focus_id="tasks-heading",
        )
    record = unfinished[0]
    return UIResult(
        request_id="desktop-handler",
        status="completed",
        message=f"Поточне завдання: {record.task_id}; state {record.state.value}.",
        focus_id="tasks-heading",
    )


def build_windows_bridge(
    config: AppConfig,
    *,
    cloud_permission_confirm: CloudModelPermissionConfirm | None = None,
    activity_report_day: Callable[[], date] | None = None,
    start_startup_recovery: bool = True,
    defer_startup_recovery: Callable[[Callable[[], None]], None] | None = None,
) -> tuple[UIActionBridge, ProductProjectCommandService]:
    store = SQLiteStore(config.database_path)
    store.initialize()
    activity_reports = DailyActivityReportService(store)
    training_status = TrainingStatusService(CheckpointService(store))
    task_queue = TaskQueue(store)
    actions = build_default_action_registry()
    keymap = Keymap(store, actions)
    source_settings = V01SourceSettings(store, config)
    model_settings = V01ModelSettings(store)
    cloud_permissions = V01CloudModelPermissionService(
        store=store,
        settings=model_settings,
        confirm=(
            _confirm_cloud_model_on_windows
            if cloud_permission_confirm is None
            else cloud_permission_confirm
        ),
    )

    def prepare_task_payload(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        source_bound = source_settings.prepare_task_payload(payload)
        return model_settings.prepare_task_payload(source_bound)

    runtime = V01PackagedThreeAgentRuntime(
        store=store,
        config=config,
        source_settings=source_settings,
        model_settings=model_settings,
        cloud_effect_authorizer=cloud_permissions.cloud_effect_authorizer,
        cloud_execution_authority_resolver=cloud_permissions.execution_authority_for_task,
    )
    backend = DesktopBackend(
        queue=task_queue,
        agents=AgentRegistry(store),
        workspaces=WorkspaceRegistry(store),
        audit=AuditLog(store),
        runtime=runtime,
        prepare_task_payload=prepare_task_payload,
        admit_created_task=cloud_permissions.admit_created_task,
        admit_resumed_task=cloud_permissions.admit_resumed_task,
        admit_recovered_task=cloud_permissions.admit_recovered_task,
        autostart_service=(
            WindowsAutostartService(Path(sys.executable))
            if sys.platform == "win32" and getattr(sys, "frozen", False)
            else None
        ),
    )
    products = ProductProjectCommandService(ProductProjectRepository(store))
    agent_definitions = AgentDefinitionRepository(store)

    def create_ordinary_task(payload: Mapping[str, Any]) -> UIResult:
        try:
            return backend.create_task(payload)
        except (ModelSetupError, CloudModelPermissionDenied) as exc:
            return UIResult(
                request_id="desktop-handler",
                status="rejected",
                message=str(exc),
                focus_id="model-route-kind",
            )

    def resume_ordinary_task(payload: Mapping[str, Any]) -> UIResult:
        try:
            return backend.resume_task(payload)
        except CloudModelPermissionDenied as exc:
            return UIResult(
                request_id="desktop-handler",
                status="rejected",
                message=str(exc),
                focus_id="model-route-kind",
            )

    product_router = PackagedProductCommandRouter(
        products=products,
        ordinary_handler=create_ordinary_task,
        agent_builder_handler=PackagedAgentBuilderDraftHandler(agent_definitions),
        task_pause_handler=backend.pause_task,
        task_resume_handler=resume_ordinary_task,
        task_stop_handler=backend.stop_agent,
        task_status_handler=lambda: _current_task_status_result(task_queue),
        activity_report_handler=lambda: _daily_activity_report_result(
            activity_reports,
            day_provider=activity_report_day,
        ),
        training_status_handler=lambda task_id: _training_status_result(
            training_status,
            task_id,
        ),
        selection_store=PackagedProductSelectionStore(store),
    )
    agent_builder_state = PackagedAgentBuilderStateProjector(agent_definitions)
    command_center = ProductCommandCenter(products)
    product_state = PackagedProductStateProvider(
        base_state=backend.snapshot,
        router=product_router,
        command_center=command_center,
    )
    packaged_state = V01PackagedTeamStateProvider(
        base_state=product_state,
        store=store,
    )

    def source_state() -> Mapping[str, Any]:
        state = {**packaged_state(), "v01_sources": source_settings.snapshot()}
        state["v01_model_settings"] = model_settings.snapshot()
        return agent_builder_state.decorate(state)

    def refresh_model_settings(payload: Mapping[str, Any]) -> UIResult:
        if payload:
            return UIResult(
                request_id="model-settings",
                status="rejected",
                message="Перечитування моделі не приймає параметрів.",
                focus_id="model-route-kind",
            )
        snapshot = model_settings.snapshot()
        if snapshot.get("status") == "invalid":
            return UIResult(
                request_id="model-settings",
                status="failed",
                message="Не вдалося прочитати збережені налаштування моделі.",
                focus_id="model-route-kind",
            )
        return UIResult(
            request_id="model-settings",
            status="completed",
            message="Збережені налаштування моделі перечитано.",
            focus_id="model-route-kind",
        )

    bridge = UIActionBridge(
        actions,
        keymap,
        handlers={
            "task.create": product_router.create,
            "task.pause": backend.pause_task,
            "task.resume": resume_ordinary_task,
            "agent.stop": backend.stop_agent,
            "team.sources.configure": source_settings.configure,
            "settings.autostart.configure": backend.autostart_settings.configure,
            "settings.autostart.refresh": backend.autostart_settings.refresh,
            "settings.model.configure": model_settings.configure,
            "settings.model.refresh": refresh_model_settings,
            "nav.tasks": lambda _payload: _focus("tasks-heading", "Завдання відкрито."),
            "nav.agents": lambda _payload: _focus("agents-heading", "Агенти відкрито."),
            "nav.logs": lambda _payload: _focus("logs-heading", "Журнал відкрито."),
            "nav.workspaces": lambda _payload: _focus(
                "workspaces-heading", "Робочі простори відкрито."
            ),
            "command.focus": lambda _payload: _focus("command-input", "Командне поле активне."),
        },
        state_provider=source_state,
    )

    def start_recovery() -> None:
        try:
            backend.start_startup_recovery()
        except Exception as exc:
            backend.close()
            raise _StartupRecoveryInventoryError(
                "packaged startup recovery inventory failed"
            ) from exc

    if start_startup_recovery:
        if defer_startup_recovery is None:
            start_recovery()
        else:
            defer_startup_recovery(start_recovery)
    return bridge, products


def _require_product_state(
    response: Mapping[str, Any],
    *,
    project_id: str,
) -> Mapping[str, Any]:
    if response.get("ok") is not True:
        raise RuntimeError(f"PF11 packaged bridge state failed: {response}")
    state = response.get("state")
    if not isinstance(state, Mapping):
        raise TypeError("PF11 packaged bridge did not return a state mapping")
    product_state = state.get("product_project")
    if not isinstance(product_state, Mapping):
        raise TypeError("PF11 packaged bridge did not expose ProductCommandCenter state")
    if (
        product_state.get("project_id") != project_id
        or product_state.get("spec_version") != 1
        or not isinstance(product_state.get("status_count"), int)
        or isinstance(product_state.get("status_count"), bool)
        or not isinstance(product_state.get("decision_count"), int)
        or isinstance(product_state.get("decision_count"), bool)
    ):
        raise RuntimeError("PF11 packaged ProductCommandCenter state identity is invalid")
    forbidden_fields = {
        "evidence",
        "evidence_refs",
        "credential_refs",
        "authorization_ref",
        "provider_session",
        "protected_store_handle",
    }
    if forbidden_fields.intersection(product_state):
        raise RuntimeError("PF11 packaged state exposed a forbidden authority/evidence field")
    return product_state


def _require_current_product_result(
    response: Mapping[str, Any],
    *,
    project_id: str,
    spec_version: int,
    state: str,
    goal: str,
) -> None:
    expected_message = (
        f"Поточний ProductProject: {project_id}; "
        f"spec version {spec_version}; state {state}; goal: {goal}."
    )
    if (
        response.get("status") != "completed"
        or response.get("message") != expected_message
        or response.get("focus_id") != "tasks-heading"
    ):
        raise RuntimeError(
            "PF11 packaged Current ProductProject command returned inconsistent identity/focus"
        )


def _run_pf11_proof(
    config: AppConfig,
    *,
    command: str,
    output_path: Path | None,
) -> int:
    bridge, products = build_windows_bridge(config, start_startup_recovery=False)
    decision = route_command(command)
    if decision.normalized_goal is None:
        raise RuntimeError("PF11 proof command did not produce a normalized ProductProject goal")
    project_id = product_project_identity(decision.normalized_goal)
    recovered_before_command = bridge.get_state()
    recovered_project = recovered_before_command.get("state", {}).get("product_project")
    if isinstance(recovered_project, Mapping) and recovered_project.get("project_id") != project_id:
        raise RuntimeError("PF11 restart restored a different ProductProject selection")
    result = bridge.dispatch(
        {
            "request_id": "pf11-packaged-proof",
            "action_id": "task.create",
            "payload": {"command": command},
        }
    )
    if result.get("status") != "completed":
        raise RuntimeError(f"PF11 packaged ProductProject route failed: {result}")
    detail = products.inspect_project(project_id)
    if detail.summary.project_id != project_id or detail.summary.version != 1:
        raise RuntimeError("PF11 packaged ProductProject identity/version proof failed")
    product_state = _require_product_state(bridge.get_state(), project_id=project_id)
    current_result = bridge.dispatch(
        {
            "request_id": "pf11-packaged-current-proof",
            "action_id": "task.create",
            "payload": {"command": "Show current ProductProject"},
        }
    )
    _require_current_product_result(
        current_result,
        project_id=project_id,
        spec_version=detail.summary.version,
        state=detail.summary.state,
        goal=detail.summary.goal,
    )
    payload = {
        "route": decision.route.value,
        "project_id": project_id,
        "spec_version": detail.summary.version,
        "state": detail.summary.state,
        "command_center_state_proven": True,
        "current_command_proven": True,
        "current_command_focus_proven": True,
        "bridge_state_project_id": product_state["project_id"],
        "bridge_state_spec_version": product_state["spec_version"],
        "bridge_state_status_count": product_state["status_count"],
        "bridge_state_decision_count": product_state["decision_count"],
        "restart_selection_integrity_proven": True,
        "bounded_projection_proven": True,
        "human_tested": False,
        "nvda_verified": False,
        "production_release_ready": False,
    }
    require_packaged_pf11_evidence(payload)
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    if output_path is None:
        print(serialized)
    else:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(serialized + "\n", encoding="utf-8")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pf11-proof", action="store_true")
    parser.add_argument("--pf11-proof-output", type=Path)
    parser.add_argument(
        "--pf11-proof-command",
        default=("Створи застосунок для керування витратами малого бізнесу"),
    )
    args = parser.parse_args(argv)
    from nika_core.reliability.legacy_database import LegacyDatabaseConflict
    from nika_core.ui.startup_error import show_recovery_error

    try:
        config = AppConfig.from_environment()
    except LegacyDatabaseConflict as exc:
        show_recovery_error(str(exc))
        return 1
    except (ValidationError, SettingsError):
        # Validation errors can embed private paths or environment values.
        show_recovery_error(
            "Некоректні налаштування Nika (NIKA_*). Перевірте конфігурацію "
            "та перезапустіть програму. Дані не змінено."
        )
        return 1
    except Exception as exc:  # noqa: BLE001 - redact configuration failures
        logging.getLogger(__name__).error(
            "Packaged configuration failed: exception_type=%s", type(exc).__name__
        )
        show_recovery_error(
            "Не вдалося прочитати налаштування Nika. Збережіть наявні дані, "
            "перевірте конфігурацію та повторіть запуск."
        )
        return 1
    if args.pf11_proof:
        return _run_pf11_proof(
            config,
            command=args.pf11_proof_command,
            output_path=args.pf11_proof_output,
        )
    try:
        preflight_windows_shell()
    except Exception as exc:  # noqa: BLE001 - redact packaged UI preflight failures
        logging.getLogger(__name__).error(
            "Packaged shell preflight failed: exception_type=%s", type(exc).__name__
        )
        show_recovery_error(
            "Не вдалося підготувати інтерфейс Nika. Перевірте цілісність "
            "встановлення та повторіть запуск. Незавершені завдання не відновлювалися."
        )
        return 1
    deferred_recovery: list[Callable[[], None]] = []
    try:
        bridge, _products = build_windows_bridge(
            config,
            defer_startup_recovery=deferred_recovery.append,
        )
        if len(deferred_recovery) != 1:
            raise RuntimeError("packaged startup recovery runner was not scheduled exactly once")
    except _StartupRecoveryInventoryError:
        show_recovery_error(
            "Nika не може безпечно перевірити незавершену роботу після перезапуску. "
            "Запуск зупинено без автоматичного повторення дій."
        )
        return 1
    except Exception as exc:  # noqa: BLE001 - redact startup failures
        logging.getLogger(__name__).error(
            "Packaged startup failed: exception_type=%s", type(exc).__name__
        )
        show_recovery_error(
            "Не вдалося відкрити дані або підготувати запуск Nika. "
            "Перевірте доступність папки даних; наявну базу не видаляйте."
        )
        return 1
    try:
        launch_windows_shell(
            bridge,
            title=f"Nika Core {config.app_version}",
            on_gui_started=deferred_recovery[0],
        )
    except _StartupRecoveryInventoryError:
        show_recovery_error(
            "Nika не може безпечно перевірити незавершену роботу після перезапуску. "
            "Запуск зупинено без автоматичного повторення дій."
        )
        return 1
    except Exception as exc:  # noqa: BLE001 - redact packaged GUI startup failures
        logging.getLogger(__name__).error(
            "Packaged shell launch failed: exception_type=%s", type(exc).__name__
        )
        show_recovery_error(
            "Не вдалося відкрити інтерфейс Nika. Перезапустіть програму. "
            "Якщо помилка повторюється, перевірте компонент WebView2 або "
            "перевстановіть застосунок."
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
