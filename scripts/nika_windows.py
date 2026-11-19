from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from pydantic_settings import SettingsError

from nika_core.config import AppConfig
from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.action_registry import Keymap
from nika_core.kernel.agent_registry import AgentRegistry
from nika_core.kernel.audit import AuditLog
from nika_core.kernel.default_actions import build_default_action_registry
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.workspace_registry import WorkspaceRegistry
from nika_core.packaged_task_scheduler import PackagedTaskScheduler
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
from nika_core.resource_profiles import (
    ResourceProfileConflict,
    ResourceProfileName,
    ResourceProfileSettings,
)
from nika_core.resources import PsutilResourceObserver, ResourceManager
from nika_core.ui.bridge import UIActionBridge
from nika_core.ui.bridge_models import UIResult
from nika_core.ui.desktop_backend import DesktopBackend
from nika_core.ui.shell import launch_windows_shell
from nika_core.v01_packaged_team_runtime import V01PackagedThreeAgentRuntime
from nika_core.v01_packaged_team_state import V01PackagedTeamStateProvider
from nika_core.v01_source_settings import V01SourceSettings
from nika_core.windows_autostart import WindowsAutostartService


def _focus(focus_id: str, message: str) -> UIResult:
    return UIResult(
        request_id="desktop-handler",
        status="completed",
        message=message,
        focus_id=focus_id,
    )


def _recover_machine_resources(resources: ResourceManager, audit: AuditLog) -> None:
    """Reconcile only process-proven dead owners; never replay tasks or sessions."""
    released = 0
    blocked = 0
    for owner in resources.stale_lease_owners():
        try:
            released += resources.recover_after_restart(stale_manager_id=owner)
        except (RuntimeError, ValueError):
            # Live, unknown or corrupt ownership is not evidence of death.
            # Neither owner identifiers nor private probe exceptions enter logs.
            blocked += 1
    if released or blocked:
        audit.append(
            event_type="desktop.resource_recovery",
            entity_type="resource_pool",
            entity_id="local_machine",
            payload={
                "outcome": "blocked" if blocked else "recovered",
                "released_leases": released,
                "unverified_owners": blocked,
            },
        )


def build_windows_bridge(
    config: AppConfig,
) -> tuple[UIActionBridge, ProductProjectCommandService]:
    store = SQLiteStore(config.database_path)
    store.initialize()
    actions = build_default_action_registry()
    keymap = Keymap(store, actions)
    source_settings = V01SourceSettings(store, config)
    audit = AuditLog(store)
    resource_settings = ResourceProfileSettings(
        store, default_profile=ResourceProfileName(config.resource_profile)
    )
    resources = ResourceManager(
        store, PsutilResourceObserver(), admission_policy=resource_settings
    )
    _recover_machine_resources(resources, audit)
    backend = DesktopBackend(
        queue=TaskQueue(store),
        resources=resources,
        agents=AgentRegistry(store),
        workspaces=WorkspaceRegistry(store),
        audit=audit,
        runtime=V01PackagedThreeAgentRuntime(
            store=store, config=config, source_settings=source_settings
        ),
        prepare_task_payload=source_settings.prepare_task_payload,
        autostart_service=(
            WindowsAutostartService(Path(sys.executable))
            if sys.platform == "win32" and getattr(sys, "frozen", False)
            else None
        ),
    )
    schedules = PackagedTaskScheduler(
        store=store, audit=audit, start_task=backend.start_scheduled_task
    )
    products = ProductProjectCommandService(ProductProjectRepository(store))
    product_router = PackagedProductCommandRouter(
        products=products,
        ordinary_handler=backend.create_task,
        selection_store=PackagedProductSelectionStore(store),
    )
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
    schedule_after_job_id: str | None = None

    def source_state() -> Mapping[str, Any]:
        try:
            profile_state = resource_settings.snapshot()
        except (OSError, RuntimeError, ValueError):
            profile_state = None
        try:
            schedule_page = schedules.snapshot_page(after_job_id=schedule_after_job_id)
            schedule_state = schedule_page["items"]
        except (OSError, RuntimeError, TypeError, ValueError):
            schedule_state = None
            schedule_page = None
        return {
            **packaged_state(), "v01_sources": source_settings.snapshot(),
            "resource_profile": profile_state, "schedules": schedule_state,
            "schedule_page": schedule_page,
        }

    def configure_resources(payload: Mapping[str, Any]) -> UIResult:
        try:
            if set(payload) != {"profile", "revision"} or type(payload["profile"]) is not str:
                raise ValueError("invalid profile request")
            resource_settings.configure(
                profile=ResourceProfileName(payload["profile"]),
                expected_revision=payload["revision"],
            )
        except ResourceProfileConflict:
            return _focus("resource-profile", "Профіль уже змінено в іншому вікні. Перечитайте стан і повторіть вибір.").model_copy(update={"status": "rejected"})
        except (OSError, RuntimeError, TypeError, ValueError):
            return UIResult(request_id="desktop-handler", status="rejected", message="Не вдалося зберегти профіль ресурсів. Перевірте стан програми та повторіть дію.", focus_id="resource-profile")
        return _focus("resource-profile", "Профіль ресурсів збережено.")

    def schedule_action(action: str, payload: Mapping[str, Any]) -> UIResult:
        nonlocal schedule_after_job_id
        task_id = None
        try:
            if action == "page":
                if set(payload) != {"after_job_id"}:
                    raise ValueError("invalid schedule page")
                cursor = payload["after_job_id"]
                if cursor is not None and type(cursor) is not str:
                    raise ValueError("invalid schedule cursor")
                schedules.snapshot_page(after_job_id=cursor)
                schedule_after_job_id = cursor
                return _focus("schedule-task", "Сторінку розкладу перечитано.")
            if action == "create":
                if set(payload) != {"command", "delay_seconds", "retry_interval_seconds", "start_window_seconds"}:
                    raise ValueError("invalid schedule request")
                command = payload["command"]
                if type(command) is not str or not command.strip() or len(command.encode("utf-8")) > 65536:
                    raise ValueError("invalid command")
                for key, maximum in (("delay_seconds", 604800), ("retry_interval_seconds", 86400), ("start_window_seconds", 1209600)):
                    if type(payload[key]) is not int or not 1 <= payload[key] <= maximum:
                        raise ValueError("invalid schedule duration")
                if payload["start_window_seconds"] <= payload["delay_seconds"]:
                    raise ValueError("deadline must follow start")
                now = datetime.now(UTC)
                task_id = backend.prepare_scheduled_task({"command": command}).task_id
                schedules.create(
                    task_id=task_id,
                    start_at=now + timedelta(seconds=payload["delay_seconds"]),
                    retry_interval_seconds=payload["retry_interval_seconds"],
                    deadline_at=now + timedelta(seconds=payload["start_window_seconds"]),
                )
                return _focus("schedule-task", f"Одноразовий запуск заплановано: {task_id}. Після допуску виконана дія не повторюється.")
            if set(payload) != {"task_id"} or type(payload["task_id"]) is not str:
                raise ValueError("invalid task identity")
            task_id = payload["task_id"]
            operation = {"pause": schedules.pause, "resume": schedules.resume, "cancel": schedules.cancel}[action]
            operation(task_id)
            if action == "cancel":
                backend.cancel_prepared_task(task_id)
            return _focus("schedule-task", {"pause": "Запланований запуск призупинено.", "resume": "Запланований запуск перевірено й відновлено, якщо він ще не завершений.", "cancel": "Запланований запуск скасовано. Уже розпочате виконання зупиняється окремою дією «Зупинити агента»."}[action])
        except (KeyError, OSError, RuntimeError, TypeError, ValueError):
            if action == "create" and task_id is not None:
                # Failed activation may have left a durable timer. Disable it
                # before cancelling the never-started ordinary task.
                try:
                    if schedules.get(task_id) is not None:
                        schedules.cancel(task_id)
                    backend.cancel_prepared_task(task_id)
                except (KeyError, OSError, RuntimeError, TypeError, ValueError):
                    logging.getLogger(__name__).warning("Scheduled preparation retained for recovery")
            return UIResult(request_id="desktop-handler", status="rejected", message="Не вдалося змінити розклад. Перевірте команду, цілі значення секунд і крайній час запуску, а потім перечитайте стан.", focus_id="schedule-delay" if action == "create" else "schedule-task")

    def close_host() -> None:
        schedules.shutdown(wait=False)
        try:
            backend.close()
        except RuntimeError:
            # Closing the window does not authorize replay or destroy durable
            # session truth. Active work is left for existing recovery policy.
            logging.getLogger(__name__).warning("Desktop runtime state retained during shutdown")

    bridge = UIActionBridge(
        actions,
        keymap,
        handlers={
            "task.create": product_router.create,
            "task.pause": backend.pause_task,
            "task.resume": backend.resume_task,
            "agent.stop": backend.stop_agent,
            "team.sources.configure": source_settings.configure,
            "settings.autostart.configure": backend.autostart_settings.configure,
            "settings.autostart.refresh": backend.autostart_settings.refresh,
            "settings.resources.configure": configure_resources,
            "schedule.create": lambda payload: schedule_action("create", payload),
            "schedule.pause": lambda payload: schedule_action("pause", payload),
            "schedule.resume": lambda payload: schedule_action("resume", payload),
            "schedule.cancel": lambda payload: schedule_action("cancel", payload),
            "schedule.page": lambda payload: schedule_action("page", payload),
            "nav.tasks": lambda _payload: _focus("tasks-heading", "Завдання відкрито."),
            "nav.agents": lambda _payload: _focus("agents-heading", "Агенти відкрито."),
            "nav.logs": lambda _payload: _focus("logs-heading", "Журнал відкрито."),
            "nav.workspaces": lambda _payload: _focus(
                "workspaces-heading", "Робочі простори відкрито."
            ),
            "command.focus": lambda _payload: _focus("command-input", "Командне поле активне."),
        },
        state_provider=source_state,
        on_close=close_host,
    )
    schedules.start()
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
    bridge, products = build_windows_bridge(config)
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
    parser.add_argument("--resource-schedule-proof", action="store_true")
    parser.add_argument("--resource-schedule-proof-output", type=Path)
    parser.add_argument(
        "--pf11-proof-command",
        default=("Створи застосунок для керування витратами малого бізнесу"),
    )
    args = parser.parse_args(argv)
    if args.resource_schedule_proof:
        # Isolated acceptance never loads user environment/default database or
        # invokes legacy adoption. Only the proof's temporary fixture is used.
        from nika_core.packaged_resource_schedule_proof import run_resource_schedule_proof

        proof = run_resource_schedule_proof(build_windows_bridge)
        serialized = json.dumps(proof, ensure_ascii=False, sort_keys=True)
        if args.resource_schedule_proof_output is None:
            print(serialized)
        else:
            args.resource_schedule_proof_output.parent.mkdir(parents=True, exist_ok=True)
            args.resource_schedule_proof_output.write_text(serialized + "\n", encoding="utf-8")
        return 0
    from nika_core.reliability.legacy_database import LegacyDatabaseConflict
    from nika_core.ui.startup_error import show_recovery_error

    try:
        config = AppConfig.from_environment()
    except LegacyDatabaseConflict as exc:
        show_recovery_error(str(exc))
        return 1
    except (ValidationError, SettingsError):
        # Validation details can contain private paths or environment values.
        show_recovery_error(
            "Некоректні налаштування Nika (NIKA_*). Перевірте конфігурацію "
            "та перезапустіть програму. Дані не змінено."
        )
        return 1
    if args.pf11_proof:
        return _run_pf11_proof(
            config,
            command=args.pf11_proof_command,
            output_path=args.pf11_proof_output,
        )
    try:
        bridge, _products = build_windows_bridge(config)
    except (OSError, RuntimeError, ValueError, sqlite3.DatabaseError):
        show_recovery_error(
            "Не вдалося відкрити збережені налаштування або розклад. "
            "Автоматичний запуск дій не дозволено. Перевірте дані програми "
            "та повторіть запуск."
        )
        return 1
    try:
        launch_windows_shell(bridge, title=f"Nika Core {config.app_version}")
    finally:
        bridge._close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
