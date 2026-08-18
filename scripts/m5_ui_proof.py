from __future__ import annotations

import tempfile
from pathlib import Path

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.action_registry import Keymap
from nika_core.kernel.default_actions import build_default_action_registry
from nika_core.ui.bridge import UIActionBridge
from nika_core.ui.shell import launch_windows_shell


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
            "nav.tasks": lambda _payload: "Завдання відкрито.",
            "nav.agents": lambda _payload: "Агенти відкрито.",
            "nav.logs": lambda _payload: "Журнал відкрито.",
        },
    )
    launch_windows_shell(bridge, title="Nika Core M5 Proof")


if __name__ == "__main__":
    main()
