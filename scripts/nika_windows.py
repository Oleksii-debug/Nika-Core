from __future__ import annotations

from nika_core.config import AppConfig
from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.action_registry import ActionDefinition, Keymap
from nika_core.kernel.agent_registry import AgentRegistry
from nika_core.kernel.audit import AuditLog
from nika_core.kernel.default_actions import build_default_action_registry
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.workspace_registry import WorkspaceRegistry
from nika_core.security import ApprovalAuthority
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


def _register_r4_actions(actions) -> None:
    actions.register(
        ActionDefinition(
            action_id="approval.approve",
            label="Підтвердити небезпечну дію",
            category="approval",
            default_binding=None,
            may_be_unbound=True,
        )
    )
    actions.register(
        ActionDefinition(
            action_id="approval.deny",
            label="Відхилити небезпечну дію",
            category="approval",
            default_binding=None,
            may_be_unbound=True,
        )
    )


def main() -> None:
    config = AppConfig.from_environment()
    store = SQLiteStore(config.database_path)
    store.initialize()
    actions = build_default_action_registry()
    _register_r4_actions(actions)
    keymap = Keymap(store, actions)
    approval_authority = ApprovalAuthority()
    backend = DesktopBackend(
        queue=TaskQueue(store),
        agents=AgentRegistry(store),
        workspaces=WorkspaceRegistry(store),
        audit=AuditLog(store),
        approval_authority=approval_authority,
    )
    bridge = UIActionBridge(
        actions,
        keymap,
        handlers={
            "task.create": backend.create_task,
            "task.pause": backend.pause_task,
            "task.resume": backend.resume_task,
            "agent.stop": backend.stop_agent,
            "approval.approve": backend.approve_action,
            "approval.deny": backend.deny_action,
            "nav.tasks": lambda _payload: _focus("tasks-heading", "Завдання відкрито."),
            "nav.agents": lambda _payload: _focus("agents-heading", "Агенти відкрито."),
            "nav.logs": lambda _payload: _focus("logs-heading", "Журнал відкрито."),
            "nav.workspaces": lambda _payload: _focus(
                "workspaces-heading", "Робочі простори відкрито."
            ),
            "command.focus": lambda _payload: _focus("command-input", "Командне поле активне."),
        },
        state_provider=backend.snapshot,
    )
    launch_windows_shell(bridge, title=f"Nika Core {config.app_version}")


if __name__ == "__main__":
    main()
