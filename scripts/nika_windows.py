from __future__ import annotations

import argparse
import json
import logging
import sqlite3
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
from nika_core.packaging.pf11_evidence import require_packaged_pf11_evidence
from nika_core.packaged_agent_builder import (
    PackagedAgentBuilderDraftHandler,
    PackagedAgentBuilderStateProjector,
)
from nika_core.packaged_intelligence_mode import PackagedIntelligenceModeCommandAdapter
from nika_core.product_command.product_project_adapter import ProductProjectCommandService
from nika_core.product_command.routing import route_command
from nika_core.product_factory_local_repository_binding import (
    ProductFactoryLocalRepositoryBindings,
)
from nika_core.product_factory_multi_repository import MultiRepositoryProductFactoryHost
from nika_core.product_factory_packaged_execution import (
    PackagedProductFactoryExecutionController,
    ProductFactoryExecutionPlanResolver,
)
from nika_core.product_factory_packaged_execution_plan_file import (
    PackagedProductFactoryExecutionPlanFileSource,
)
from nika_core.product_factory_packaged_local_settings import (
    PackagedLocalProductFactorySettings,
    PackagedLocalProductFactorySettingsError,
)
from nika_core.product_factory_packaged_local_startup import (
    PackagedLocalProductFactoryStartupError,
    build_packaged_local_product_factory_program,
    decode_packaged_local_product_factory_startup,
)
from nika_core.product_factory_packaged_journey import (
    PackagedProductCommandRouter,
    PackagedProductSelectionStore,
    PackagedProductStateProvider,
    product_project_identity,
)
from nika_core.product_factory_packaged_planning import (
    TEAM_PLAN_REF_PREFIX,
    PackagedProductFactoryTeamPlanner,
    PackagedTeamPlanResult,
)
from nika_core.product_factory_packaged_preparation import (
    PackagedProductFactoryPreparationService,
)
from nika_core.product_factory_packaged_repository_binding import (
    PackagedProductFactoryRepositoryBindingController,
)
from nika_core.product_factory_packaged_status import (
    PACKAGED_PRODUCT_FACTORY_WORKSPACE_ID,
    PackagedProductCommandCenter,
    PackagedProductFactoryStatusReader,
)
from nika_core.product_project import ProductProjectRepository
from nika_core.security import ApprovalAuthority
from nika_core.ui.bridge import UIActionBridge
from nika_core.ui.bridge_models import UIResult
from nika_core.ui.desktop_backend import DesktopBackend
from nika_core.ui.packaged_speech import PackagedSpeechFeature, build_packaged_speech
from nika_core.ui.packaged_voice import PackagedVoiceFeature, build_packaged_voice
from nika_core.ui.packaged_voice_model_setup import PackagedVoiceModelSetup
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
_NONTERMINAL_TASK_STATES = tuple(
    state for state in TaskState if state not in _TERMINAL_TASK_STATES
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


def _current_task_status_result(
    queue: TaskQueue,
    task_id: str | None = None,
) -> UIResult:
    try:
        if task_id is not None:
            record = queue.get(task_id)
            return UIResult(
                request_id="desktop-handler",
                status="completed",
                message=f"Завдання: {record.task_id}; state {record.state.value}.",
                focus_id="tasks-heading",
            )
        unfinished = queue.list_by_states(_NONTERMINAL_TASK_STATES, limit=2)
    except KeyError:
        return UIResult(
            request_id="desktop-handler",
            status="rejected",
            message=f"Завдання не знайдено: {task_id}.",
            focus_id="tasks-heading",
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
                "Є кілька незавершених завдань; "
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
    register_cleanup: Callable[[Callable[[], None]], None] | None = None,
    product_factory_execution_host: MultiRepositoryProductFactoryHost | None = None,
    product_factory_execution_plan_resolver: ProductFactoryExecutionPlanResolver | None = None,
) -> tuple[UIActionBridge, ProductProjectCommandService]:
    if (
        product_factory_execution_host is not None
        and config.product_factory_local_startup_json is not None
    ):
        raise ValueError(
            "explicit Product Factory execution host conflicts with "
            "packaged local startup authority"
        )
    if (
        product_factory_execution_host is None
        and product_factory_execution_plan_resolver is not None
    ):
        raise ValueError(
            "Product Factory execution-plan resolver requires an execution host"
        )

    store = SQLiteStore(config.database_path)
    store.initialize()
    activity_reports = DailyActivityReportService(store)
    training_status = TrainingStatusService(CheckpointService(store))
    task_queue = TaskQueue(store)
    audit_log = AuditLog(store)
    actions = build_default_action_registry()
    keymap = Keymap(store, actions)
    source_settings = V01SourceSettings(store, config)
    model_settings = V01ModelSettings(store)
    local_product_factory_settings = PackagedLocalProductFactorySettings(store)
    local_product_factory_environment_override = (
        config.product_factory_local_startup_json is not None
    )
    local_product_factory_settings_invalid = False
    if local_product_factory_environment_override:
        local_product_factory_startup_json = config.product_factory_local_startup_json
    else:
        try:
            local_product_factory_startup_json = (
                local_product_factory_settings.saved_json()
            )
        except PackagedLocalProductFactorySettingsError:
            local_product_factory_startup_json = None
            local_product_factory_settings_invalid = True

    local_product_factory_startup = None
    if local_product_factory_startup_json is not None:
        try:
            local_product_factory_startup = (
                decode_packaged_local_product_factory_startup(
                    local_product_factory_startup_json
                )
            )
        except PackagedLocalProductFactoryStartupError:
            local_product_factory_settings_invalid = True

    if (
        product_factory_execution_host is not None
        and local_product_factory_startup_json is not None
    ):
        raise ValueError(
            "explicit Product Factory execution host conflicts with "
            "packaged local startup authority"
        )

    local_product_factory_runtime_active = False
    local_product_factory_launch_model_revision: int | None = None
    local_product_factory_launch_settings_revision: int | None = None
    if (
        product_factory_execution_host is None
        and local_product_factory_startup is not None
        and not local_product_factory_settings_invalid
    ):
        try:
            # Both persisted authorities use BEGIN IMMEDIATE for mutation. Hold the
            # matching reservation through snapshot, composition and activation so a
            # second window/process cannot install a mixed startup/model authority.
            with store.connection() as local_factory_authority_guard:
                local_factory_authority_guard.execute("BEGIN IMMEDIATE")
                startup_settings_revision: int | None = None
                if not local_product_factory_environment_override:
                    startup_settings_snapshot = local_product_factory_settings.snapshot(
                        environment_override=False,
                        runtime_status="not_configured",
                    )
                    raw_settings_revision = startup_settings_snapshot.get("revision")
                    if (
                        startup_settings_snapshot.get("status") != "ready"
                        or startup_settings_snapshot.get("config_json")
                        != local_product_factory_startup_json
                        or type(raw_settings_revision) is not int
                        or raw_settings_revision < 1
                    ):
                        local_product_factory_settings_invalid = True
                    else:
                        startup_settings_revision = raw_settings_revision

                model_snapshot = model_settings.snapshot()
                model_revision = model_snapshot.get("revision")
                local_product_factory_model_ready = (
                    not local_product_factory_settings_invalid
                    and model_snapshot.get("status") == "ready"
                    and model_snapshot.get("route_kind") == "ollama"
                    and model_snapshot.get("provider_id") == "ollama"
                    and model_snapshot.get("provider_kind") == "local"
                    and type(model_revision) is int
                    and model_revision >= 1
                )
                if local_product_factory_model_ready:
                    try:
                        local_product_factory_program = (
                            build_packaged_local_product_factory_program(
                                store,
                                settings=model_settings,
                                startup=local_product_factory_startup,
                            )
                        )
                    except Exception as exc:  # noqa: BLE001 - optional backend must fail closed
                        logging.getLogger(__name__).error(
                            "Local Product Factory startup failed: exception_type=%s",
                            type(exc).__name__,
                        )
                        local_product_factory_settings_invalid = True
                    else:
                        model_after_build = model_settings.snapshot()
                        startup_settings_stable = True
                        if not local_product_factory_environment_override:
                            startup_settings_after = (
                                local_product_factory_settings.snapshot(
                                    environment_override=False,
                                    runtime_status="not_configured",
                                )
                            )
                            startup_settings_stable = (
                                startup_settings_after.get("status") == "ready"
                                and startup_settings_after.get("config_json")
                                == local_product_factory_startup_json
                                and startup_settings_after.get("revision")
                                == startup_settings_revision
                            )
                        if (
                            model_after_build.get("status") != "ready"
                            or model_after_build.get("revision") != model_revision
                            or not startup_settings_stable
                        ):
                            logging.getLogger(__name__).warning(
                                "Local Product Factory authority changed during startup; "
                                "restart is required before execution"
                            )
                            local_product_factory_settings_invalid = True
                        else:
                            product_factory_execution_host = (
                                local_product_factory_program.multi_repository_host
                            )
                            local_product_factory_runtime_active = True
                            local_product_factory_launch_model_revision = model_revision
                            local_product_factory_launch_settings_revision = (
                                startup_settings_revision
                            )
        except sqlite3.Error as exc:
            logging.getLogger(__name__).error(
                "Local Product Factory authority fence failed: exception_type=%s",
                type(exc).__name__,
            )
            local_product_factory_settings_invalid = True

    local_product_factory_launch_json = local_product_factory_startup_json

    def local_product_factory_restart_focus() -> str | None:
        if not local_product_factory_runtime_active:
            return None
        if not local_product_factory_environment_override:
            settings_snapshot = local_product_factory_settings.snapshot(
                environment_override=False,
                runtime_status="not_configured",
            )
            if (
                settings_snapshot.get("status") != "ready"
                or settings_snapshot.get("config_json")
                != local_product_factory_launch_json
                or settings_snapshot.get("revision")
                != local_product_factory_launch_settings_revision
            ):
                return "product-factory-local-startup-json"
        model_snapshot = model_settings.snapshot()
        if (
            model_snapshot.get("status") != "ready"
            or model_snapshot.get("revision")
            != local_product_factory_launch_model_revision
        ):
            return "model-route-kind"
        return None
    intelligence_mode_commands = PackagedIntelligenceModeCommandAdapter(model_settings)
    cloud_permissions = V01CloudModelPermissionService(
        store=store,
        settings=model_settings,
        confirm=(
            _confirm_cloud_model_on_windows
            if cloud_permission_confirm is None
            else cloud_permission_confirm
        ),
    )

    def local_product_factory_runtime_status() -> str:
        if local_product_factory_settings_invalid:
            return "invalid"
        if local_product_factory_runtime_active:
            if local_product_factory_restart_focus() is not None:
                return "restart_required"
            return "active"
        if local_product_factory_environment_override:
            if local_product_factory_startup is not None:
                return "model_required"
            return "invalid"

        settings_snapshot = local_product_factory_settings.snapshot(
            environment_override=False,
            runtime_status="not_configured",
        )
        if settings_snapshot.get("status") != "ready":
            return "invalid"
        current_saved_json = settings_snapshot.get("config_json")
        if current_saved_json != local_product_factory_launch_json:
            return "restart_required"
        if current_saved_json is None:
            return "not_configured"
        return "model_required"

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
        audit=audit_log,
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
    if register_cleanup is not None:
        register_cleanup(backend.close)
    voice: PackagedVoiceFeature = build_packaged_voice(
        config.database_path.parent,
        submit=backend.submit_packaged_coroutine,
    )
    if register_cleanup is not None and voice.available:
        register_cleanup(voice.close)
    voice_model_setup = PackagedVoiceModelSetup(
        config.database_path.parent,
        submit=backend.submit_packaged_coroutine,
    )
    if register_cleanup is not None:
        register_cleanup(voice_model_setup.close)
    speech: PackagedSpeechFeature = build_packaged_speech()
    if register_cleanup is not None:
        register_cleanup(speech.close)
    decision_approval_authority = ApprovalAuthority(audit_sink=audit_log)
    product_repository = ProductProjectRepository(store)
    product_factory_execution_plan_files = (
        PackagedProductFactoryExecutionPlanFileSource()
        if (
            product_factory_execution_host is not None
            and product_factory_execution_plan_resolver is None
        )
        else None
    )
    product_factory_repository_binding = (
        PackagedProductFactoryRepositoryBindingController(
            bindings=ProductFactoryLocalRepositoryBindings(
                store,
                product_repository,
            ),
            projects=product_repository,
            resolve_plan=product_factory_execution_plan_files.resolve,
        )
        if (
            local_product_factory_runtime_active
            and product_factory_execution_plan_files is not None
        )
        else None
    )
    products = ProductProjectCommandService(
        product_repository,
        approval_verifier=decision_approval_authority.verifier(),
    )
    product_factory_execution_handler = None
    if product_factory_execution_host is not None:
        execution_plan_resolver = product_factory_execution_plan_resolver
        if execution_plan_resolver is None:
            assert product_factory_execution_plan_files is not None
            execution_plan_resolver = product_factory_execution_plan_files.resolve
        product_factory_execution = PackagedProductFactoryExecutionController(
            preparation=PackagedProductFactoryPreparationService(
                repository=product_repository,
                tasks=task_queue,
                host=product_factory_execution_host,
                workspace_id=PACKAGED_PRODUCT_FACTORY_WORKSPACE_ID,
            ),
            host=product_factory_execution_host,
            resolve_plan=execution_plan_resolver,
            submit=backend.submit_packaged_coroutine,
        )
        def start_product_factory_execution(project_id: str) -> UIResult:
            restart_focus = local_product_factory_restart_focus()
            if (
                local_product_factory_runtime_active
                and restart_focus is not None
            ):
                return UIResult(
                    request_id="desktop-handler",
                    status="rejected",
                    message=(
                        "Налаштування локального Product Factory або моделі "
                        "змінилися після запуску Nika. Перезапустіть Nika перед "
                        "новим запуском Product Factory."
                    ),
                    focus_id=restart_focus,
                )
            return product_factory_execution.start(project_id)

        product_factory_execution_handler = start_product_factory_execution
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

    command_center = PackagedProductCommandCenter(
        products=products,
        status_reader=PackagedProductFactoryStatusReader(store),
    )
    product_router = PackagedProductCommandRouter(
        products=products,
        ordinary_handler=create_ordinary_task,
        agent_builder_handler=PackagedAgentBuilderDraftHandler(agent_definitions),
        task_pause_handler=backend.pause_task,
        task_resume_handler=resume_ordinary_task,
        task_stop_handler=backend.stop_agent,
        task_status_handler=lambda task_id: _current_task_status_result(
            task_queue,
            task_id,
        ),
        activity_report_handler=lambda: _daily_activity_report_result(
            activity_reports,
            day_provider=activity_report_day,
        ),
        training_status_handler=lambda task_id: _training_status_result(
            training_status,
            task_id,
        ),
        intelligence_mode_handler=intelligence_mode_commands.execute,
        selection_store=PackagedProductSelectionStore(store),
        decision_approval_authority=decision_approval_authority,
        team_planner=PackagedProductFactoryTeamPlanner(product_repository),
        product_factory_status_inspector=command_center.inspect_packaged_project,
        product_factory_execution_handler=product_factory_execution_handler,
    )
    agent_builder_state = PackagedAgentBuilderStateProjector(agent_definitions)
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
        state["product_factory_local_startup"] = (
            local_product_factory_settings.snapshot(
                environment_override=local_product_factory_environment_override,
                runtime_status=local_product_factory_runtime_status(),
            )
        )
        state["speech"] = speech.snapshot()
        state["voice"] = voice.snapshot()
        state["voice_model_setup"] = voice_model_setup.snapshot()
        state["product_factory_execution_plan"] = (
            product_factory_execution_plan_files.snapshot()
            if product_factory_execution_plan_files is not None
            else None
        )
        state["product_factory_repository_bindings"] = (
            product_factory_repository_binding.snapshot(
                product_router.active_project_id,
            )
            if product_factory_repository_binding is not None
            else None
        )
        return agent_builder_state.decorate(state)

    def refresh_local_product_factory_settings(
        payload: Mapping[str, Any],
    ) -> UIResult:
        if payload:
            return UIResult(
                request_id="product-factory-local-startup-settings",
                status="rejected",
                message="Перечитування Product Factory не приймає параметрів.",
                focus_id="product-factory-local-startup-json",
            )
        return UIResult(
            request_id="product-factory-local-startup-settings",
            status="completed",
            message="Збережені налаштування Product Factory перечитано.",
            focus_id="product-factory-local-startup-json",
        )

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

    def load_product_factory_execution_plan(payload: Mapping[str, Any]) -> UIResult:
        if product_factory_execution_plan_files is None:
            return UIResult(
                request_id="desktop-handler",
                status="rejected",
                message=(
                    "Файлове завантаження JSON-плану Product Factory "
                    "недоступне в поточній конфігурації виконання."
                ),
                focus_id="product-factory-execution-plan-path",
            )
        return product_factory_execution_plan_files.load(payload)

    def bind_product_factory_repository(payload: Mapping[str, Any]) -> UIResult:
        if product_factory_repository_binding is None:
            return UIResult(
                request_id="desktop-handler",
                status="rejected",
                message=(
                    "Прив’язка локального репозиторію доступна лише для "
                    "активного packaged локального Product Factory."
                ),
                focus_id="product-factory-repository-root",
            )
        return product_factory_repository_binding.bind(
            product_router.active_project_id,
            payload,
        )

    bridge = UIActionBridge(
        actions,
        keymap,
        handlers={
            "task.create": product_router.create,
            "task.pause": backend.pause_task,
            "task.resume": resume_ordinary_task,
            "task.page.previous": backend.previous_task_page,
            "task.page.next": backend.next_task_page,
            "agent.stop": backend.stop_agent,
            "voice.start": voice.start,
            "voice.cancel": voice.cancel,
            "voice.model.import": voice_model_setup.start,
            "voice.model.cancel": voice_model_setup.cancel,
            "product.factory.execution_plan.load": load_product_factory_execution_plan,
            "product.factory.repository.bind": bind_product_factory_repository,
            "settings.product_factory_local.configure": (
                local_product_factory_settings.configure
            ),
            "settings.product_factory_local.refresh": (
                refresh_local_product_factory_settings
            ),
            "speech.start": speech.speak,
            "speech.cancel": speech.cancel,
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
            try:
                speech.close()
            finally:
                try:
                    voice_model_setup.close()
                finally:
                    try:
                        voice.close()
                    finally:
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
    spec_version: int = 1,
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
        or product_state.get("spec_version") != spec_version
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


def _require_team_plan_result(
    response: Mapping[str, Any],
    *,
    plan_result: PackagedTeamPlanResult,
) -> None:
    permission_ceiling = ", ".join(sorted(plan_result.plan.permission_ceiling)) or "none"
    expected_message = (
        f"План Product Factory: {plan_result.plan.plan_id}; "
        f"ProductProject: {plan_result.project_id}; spec version {plan_result.spec_version}; "
        f"state {plan_result.state}; scale medium; roles {len(plan_result.plan.roles)}; "
        f"independent review roles {plan_result.independent_review_count}; "
        f"permission ceiling: {permission_ceiling}; worker dispatch: not started."
    )
    if (
        response.get("status") != "completed"
        or response.get("message") != expected_message
        or response.get("focus_id") != "tasks-heading"
    ):
        raise RuntimeError(
            "PF11 packaged Product Factory plan returned inconsistent identity/focus"
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


def _run_pf11_team_plan_proof(
    config: AppConfig,
    *,
    command: str,
    output_path: Path | None,
) -> int:
    bridge, products = build_windows_bridge(config, start_startup_recovery=False)
    decision = route_command(command)
    if decision.normalized_goal is None:
        raise RuntimeError(
            "PF11 team-plan proof command did not produce a normalized ProductProject goal"
        )
    project_id = product_project_identity(decision.normalized_goal)
    recovered_before_command = bridge.get_state()
    recovered_project = recovered_before_command.get("state", {}).get("product_project")
    if isinstance(recovered_project, Mapping) and recovered_project.get("project_id") != project_id:
        raise RuntimeError("PF11 team-plan restart restored a different ProductProject selection")

    created = bridge.dispatch(
        {
            "request_id": "pf11-team-plan-create",
            "action_id": "task.create",
            "payload": {"command": command},
        }
    )
    if created.get("status") != "completed":
        raise RuntimeError(f"PF11 team-plan ProductProject route failed: {created}")

    plan_response = bridge.dispatch(
        {
            "request_id": "pf11-team-plan-create-plan",
            "action_id": "task.create",
            "payload": {"command": "Plan current ProductProject"},
        }
    )
    if plan_response.get("status") != "completed":
        raise RuntimeError(f"PF11 packaged Product Factory planning failed: {plan_response}")

    detail = products.inspect_project(project_id)
    if detail.summary.project_id != project_id or detail.summary.version != 2:
        raise RuntimeError("PF11 packaged team plan did not persist as ProductProject spec v2")

    proof_store = SQLiteStore(config.database_path)
    proof_store.initialize()
    proof_repository = ProductProjectRepository(proof_store)
    team_plan = PackagedProductFactoryTeamPlanner(proof_repository).inspect(project_id)
    persisted_project = proof_repository.get(project_id)
    owned_refs = tuple(
        ref
        for ref in persisted_project.spec.team_refs
        if ref.startswith(TEAM_PLAN_REF_PREFIX)
    )
    if owned_refs != (team_plan.binding_ref,):
        raise RuntimeError("PF11 packaged team plan binding is not canonical ProductProject state")
    if team_plan.plan.permission_ceiling != frozenset({"read_project"}):
        raise RuntimeError("PF11 packaged team plan exceeded planning-only permission ceiling")
    if any(role.permissions - frozenset({"read_project"}) for role in team_plan.plan.roles):
        raise RuntimeError("PF11 packaged team role exceeded planning-only permissions")

    _require_team_plan_result(plan_response, plan_result=team_plan)
    show_plan_response = bridge.dispatch(
        {
            "request_id": "pf11-team-plan-show",
            "action_id": "task.create",
            "payload": {"command": "Show current Product Factory plan"},
        }
    )
    _require_team_plan_result(show_plan_response, plan_result=team_plan)

    product_state = _require_product_state(
        bridge.get_state(),
        project_id=project_id,
        spec_version=2,
    )
    current_result = bridge.dispatch(
        {
            "request_id": "pf11-team-plan-current",
            "action_id": "task.create",
            "payload": {"command": "Show current ProductProject"},
        }
    )
    _require_current_product_result(
        current_result,
        project_id=project_id,
        spec_version=2,
        state=detail.summary.state,
        goal=detail.summary.goal,
    )

    payload = {
        "route": decision.route.value,
        "project_id": project_id,
        "spec_version": 2,
        "state": detail.summary.state,
        "team_plan_id": team_plan.plan.plan_id,
        "team_plan_binding_ref": team_plan.binding_ref,
        "team_plan_role_count": len(team_plan.plan.roles),
        "team_plan_independent_review_count": team_plan.independent_review_count,
        "team_plan_permission_ceiling": sorted(team_plan.plan.permission_ceiling),
        "team_plan_persisted_proven": True,
        "team_plan_worker_dispatch_started": False,
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
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    if output_path is None:
        print(serialized)
    else:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(serialized + "\n", encoding="utf-8")
    return 0


def _run_voice_runtime_proof(output_path: Path | None) -> int:
    """Prove frozen local-voice imports without opening a microphone or model."""

    if sys.platform != "win32":
        raise RuntimeError("packaged voice runtime proof requires Windows")
    if output_path is None:
        raise ValueError("--voice-runtime-proof-output is required")
    try:
        import _sounddevice_data
        import numpy
        import sherpa_onnx
        import sounddevice
        from sherpa_onnx.lib import _sherpa_onnx
    except Exception as exc:  # noqa: BLE001 - frozen native dependency boundary
        raise RuntimeError(
            f"packaged voice dependency import failed: {type(exc).__name__}"
        ) from None

    sounddevice_roots = tuple(Path(item) for item in _sounddevice_data.__path__)
    portaudio_dlls = tuple(
        candidate
        for root in sounddevice_roots
        for candidate in (root / "portaudio-binaries").glob("libportaudio*.dll")
        if candidate.is_file()
    )
    if not portaudio_dlls:
        raise RuntimeError("packaged sounddevice data does not contain PortAudio DLLs")

    payload = {
        "schema": "nika.packaged-voice-runtime-proof:v1",
        "numpy_imported": numpy is not None,
        "sherpa_onnx_imported": sherpa_onnx is not None,
        "sherpa_native_imported": _sherpa_onnx is not None,
        "sounddevice_imported": sounddevice is not None,
        "sounddevice_data_proven": True,
        "microphone_opened": False,
        "model_loaded": False,
        "human_tested": False,
        "nvda_verified": False,
        "production_release_ready": False,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return 0


def _cleanup_packaged_resources(
    cleanup_callbacks: list[Callable[[], None]],
) -> None:
    """Release registered packaged resources in reverse construction order."""

    while cleanup_callbacks:
        cleanup = cleanup_callbacks.pop()
        try:
            cleanup()
        except Exception as exc:  # noqa: BLE001 - shutdown is best-effort and private
            logging.getLogger(__name__).error(
                "Packaged resource cleanup failed: exception_type=%s",
                type(exc).__name__,
            )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pf11-proof", action="store_true")
    parser.add_argument("--pf11-proof-output", type=Path)
    parser.add_argument("--pf11-team-plan-proof", action="store_true")
    parser.add_argument("--pf11-team-plan-proof-output", type=Path)
    parser.add_argument("--voice-runtime-proof", action="store_true")
    parser.add_argument("--voice-runtime-proof-output", type=Path)
    parser.add_argument(
        "--pf11-proof-command",
        default=("Створи застосунок для керування витратами малого бізнесу"),
    )
    args = parser.parse_args(argv)
    if args.pf11_team_plan_proof and (args.pf11_proof or args.voice_runtime_proof):
        parser.error("--pf11-team-plan-proof cannot be combined with another proof mode")
    if args.voice_runtime_proof:
        return _run_voice_runtime_proof(args.voice_runtime_proof_output)
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
    if args.pf11_team_plan_proof:
        return _run_pf11_team_plan_proof(
            config,
            command=args.pf11_proof_command,
            output_path=args.pf11_team_plan_proof_output,
        )
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
    cleanup_callbacks: list[Callable[[], None]] = []
    try:
        bridge, _products = build_windows_bridge(
            config,
            defer_startup_recovery=deferred_recovery.append,
            register_cleanup=cleanup_callbacks.append,
        )
        if len(deferred_recovery) != 1:
            raise RuntimeError("packaged startup recovery runner was not scheduled exactly once")
    except _StartupRecoveryInventoryError:
        _cleanup_packaged_resources(cleanup_callbacks)
        show_recovery_error(
            "Nika не може безпечно перевірити незавершену роботу після перезапуску. "
            "Запуск зупинено без автоматичного повторення дій."
        )
        return 1
    except Exception as exc:  # noqa: BLE001 - redact startup failures
        _cleanup_packaged_resources(cleanup_callbacks)
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
    finally:
        _cleanup_packaged_resources(cleanup_callbacks)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
