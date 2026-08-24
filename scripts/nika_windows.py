from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from nika_core.config import AppConfig
from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.action_registry import Keymap
from nika_core.kernel.agent_registry import AgentRegistry
from nika_core.kernel.audit import AuditLog
from nika_core.kernel.default_actions import build_default_action_registry
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.workspace_registry import WorkspaceRegistry
from nika_core.product_command.command_center import ProductCommandCenter
from nika_core.product_command.product_project_adapter import ProductProjectCommandService
from nika_core.product_command.routing import route_command
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
from nika_core.product_project import ProductProjectRepository
from nika_core.ui.bridge import UIActionBridge
from nika_core.ui.bridge_models import UIResult
from nika_core.ui.desktop_backend import DesktopBackend
from nika_core.ui.shell import launch_windows_shell


def _focus(focus_id: str, message: str) -> UIResult:
    return UIResult(
        request_id="desktop-handler",
        status="completed",
        message=message,
        focus_id=focus_id,
    )


def build_windows_bridge(
    config: AppConfig,
) -> tuple[UIActionBridge, ProductProjectCommandService]:
    store = SQLiteStore(config.database_path)
    store.initialize()
    actions = build_default_action_registry()
    keymap = Keymap(store, actions)
    backend = DesktopBackend(
        queue=TaskQueue(store),
        agents=AgentRegistry(store),
        workspaces=WorkspaceRegistry(store),
        audit=AuditLog(store),
    )
    repository = ProductProjectRepository(store)
    products = ProductProjectCommandService(repository)
    product_router = PackagedProductCommandRouter(
        products=products,
        ordinary_handler=backend.create_task,
        selection_store=PackagedProductSelectionStore(store),
        team_planner=PackagedProductFactoryTeamPlanner(repository),
    )
    command_center = ProductCommandCenter(products)
    product_state = PackagedProductStateProvider(
        base_state=backend.snapshot,
        router=product_router,
        command_center=command_center,
    )
    bridge = UIActionBridge(
        actions,
        keymap,
        handlers={
            "task.create": product_router.create,
            "task.pause": backend.pause_task,
            "task.resume": backend.resume_task,
            "agent.stop": backend.stop_agent,
            "nav.tasks": lambda _payload: _focus(
                "tasks-heading", "Завдання відкрито."
            ),
            "nav.agents": lambda _payload: _focus(
                "agents-heading", "Агенти відкрито."
            ),
            "nav.logs": lambda _payload: _focus("logs-heading", "Журнал відкрито."),
            "nav.workspaces": lambda _payload: _focus(
                "workspaces-heading", "Робочі простори відкрито."
            ),
            "command.focus": lambda _payload: _focus(
                "command-input", "Командне поле активне."
            ),
        },
        state_provider=product_state,
    )
    return bridge, products


def _require_product_state(
    response: Mapping[str, Any],
    *,
    project_id: str,
    spec_version: int,
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
    status_counts = product_state.get("status_counts")
    if not isinstance(status_counts, Mapping) or status_counts.get("team_role") != 1:
        raise RuntimeError("PF11 packaged state did not expose the durable team-plan reference")
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

    plan_response = bridge.dispatch(
        {
            "request_id": "pf11-packaged-plan-proof",
            "action_id": "task.create",
            "payload": {"command": "Plan current ProductProject"},
        }
    )
    if plan_response.get("status") != "completed":
        raise RuntimeError(f"PF11 packaged Product Factory planning failed: {plan_response}")

    detail = products.inspect_project(project_id)
    if detail.summary.project_id != project_id or detail.summary.version != 2:
        raise RuntimeError("PF11 packaged ProductProject identity/version proof failed")

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
    if any(
        role.permissions - frozenset({"read_project"})
        for role in team_plan.plan.roles
    ):
        raise RuntimeError("PF11 packaged team role exceeded planning-only permissions")

    _require_team_plan_result(plan_response, plan_result=team_plan)
    show_plan_response = bridge.dispatch(
        {
            "request_id": "pf11-packaged-show-plan-proof",
            "action_id": "task.create",
            "payload": {"command": "Show current Product Factory plan"},
        }
    )
    _require_team_plan_result(show_plan_response, plan_result=team_plan)

    product_state = _require_product_state(
        bridge.get_state(),
        project_id=project_id,
        spec_version=detail.summary.version,
    )
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
        "team_plan_id": team_plan.plan.plan_id,
        "team_plan_binding_ref": team_plan.binding_ref,
        "team_plan_role_count": len(team_plan.plan.roles),
        "team_plan_independent_review_count": team_plan.independent_review_count,
        "team_plan_permission_ceiling": sorted(team_plan.plan.permission_ceiling),
        "team_plan_persisted_proven": True,
        "team_plan_worker_dispatch_started": False,
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
    parser.add_argument(
        "--pf11-proof-command",
        default=(
            "Створи застосунок для керування витратами"
            " малого бізнесу"
        ),
    )
    args = parser.parse_args(argv)
    config = AppConfig.from_environment()
    if args.pf11_proof:
        return _run_pf11_proof(
            config,
            command=args.pf11_proof_command,
            output_path=args.pf11_proof_output,
        )
    bridge, _products = build_windows_bridge(config)
    launch_windows_shell(bridge, title=f"Nika Core {config.app_version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
