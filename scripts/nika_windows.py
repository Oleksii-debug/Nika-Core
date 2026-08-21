from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from nika_core.config import AppConfig
from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.action_registry import Keymap
from nika_core.kernel.agent_registry import AgentRegistry
from nika_core.kernel.audit import AuditLog
from nika_core.kernel.default_actions import build_default_action_registry
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.workspace_registry import WorkspaceRegistry
from nika_core.product_command.product_project_adapter import ProductProjectCommandService
from nika_core.product_command.routing import route_command
from nika_core.product_factory_packaged_journey import (
    PackagedProductCommandRouter,
    product_project_identity,
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
    products = ProductProjectCommandService(ProductProjectRepository(store))
    product_router = PackagedProductCommandRouter(
        products=products,
        ordinary_handler=backend.create_task,
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
        state_provider=backend.snapshot,
    )
    return bridge, products


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
    result = bridge.dispatch(
        {
            "request_id": "pf11-packaged-proof",
            "action_id": "task.create",
            "payload": {"command": command},
        }
    )
    if result.get("status") != "completed":
        raise RuntimeError(f"PF11 packaged ProductProject route failed: {result}")
    project_id = product_project_identity(decision.normalized_goal)
    detail = products.inspect_project(project_id)
    if detail.summary.project_id != project_id or detail.summary.version != 1:
        raise RuntimeError("PF11 packaged ProductProject identity/version proof failed")
    payload = {
        "route": decision.route.value,
        "project_id": project_id,
        "spec_version": detail.summary.version,
        "state": detail.summary.state,
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
