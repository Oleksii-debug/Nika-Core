from __future__ import annotations

from nika_core.config import AppConfig
from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.action_registry import Keymap
from nika_core.kernel.default_actions import build_default_action_registry
from nika_core.ui.bridge import UIActionBridge
from nika_core.ui.bridge_models import UIResult
from nika_core.ui.shell import launch_windows_shell


def _focus(focus_id: str, message: str) -> UIResult:
    return UIResult(
        request_id="desktop-handler",
        status="completed",
        message=message,
        focus_id=focus_id,
    )


def main() -> None:
    config = AppConfig.from_environment()
    store = SQLiteStore(config.database_path)
    store.initialize()
    actions = build_default_action_registry()
    keymap = Keymap(store, actions)
    bridge = UIActionBridge(
        actions,
        keymap,
        handlers={
            "nav.tasks": lambda _payload: _focus("tasks-heading", "Завдання відкрито."),
            "nav.agents": lambda _payload: _focus("agents-heading", "Агенти відкрито."),
            "nav.logs": lambda _payload: _focus("logs-heading", "Журнал відкрито."),
            "nav.workspaces": lambda _payload: _focus(
                "workspaces-heading", "Робочі простори відкрито."
            ),
            "command.focus": lambda _payload: _focus("command-input", "Командне поле активне."),
        },
    )
    launch_windows_shell(bridge, title=f"Nika Core {config.app_version}")


if __name__ == "__main__":
    main()
