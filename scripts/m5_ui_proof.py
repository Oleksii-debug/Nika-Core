from __future__ import annotations

import tempfile
from pathlib import Path

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.action_registry import Keymap
from nika_core.kernel.default_actions import build_default_action_registry
from nika_core.ui.bridge import UIActionBridge
from nika_core.ui.bridge_models import UIResult
from nika_core.ui.shell import launch_windows_shell


def focus_result(focus_id: str, message: str) -> UIResult:
    return UIResult(
        request_id="bridge-handler",
        status="completed",
        message=message,
        focus_id=focus_id,
    )


def main() -> None:
    data_dir = Path(tempfile.gettempdir()) / "nika-core-m5-proof"
    data_dir.mkdir(parents=True, exist_ok=True)
    store = SQLiteStore(data_dir / "nika.db")
    store.initialize()
    actions = build_default_action_registry()
    keymap = Keymap(store, actions)
    bridge = UIActionBridge(
        actions,
        keymap,
        handlers={
            "nav.tasks": lambda _payload: focus_result("tasks-heading", "Завдання відкрито."),
            "nav.agents": lambda _payload: focus_result("agents-heading", "Агенти відкрито."),
            "nav.logs": lambda _payload: focus_result("logs-heading", "Журнал відкрито."),
            "nav.workspaces": lambda _payload: focus_result(
                "workspaces-heading", "Робочі простори відкрито."
            ),
            "command.focus": lambda _payload: focus_result("command-input", "Командне поле активне."),
        },
    )
    launch_windows_shell(bridge, title="Nika Core M5 Proof")


if __name__ == "__main__":
    main()
