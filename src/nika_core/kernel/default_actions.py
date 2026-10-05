from __future__ import annotations

from nika_core.kernel.action_registry import ActionDefinition, ActionRegistry


def build_default_action_registry() -> ActionRegistry:
    registry = ActionRegistry()
    for action in (
        ActionDefinition("task.create", "Create task", "Tasks", "Ctrl+N", may_be_unbound=False),
        ActionDefinition("task.pause", "Pause task", "Tasks", "Ctrl+P"),
        ActionDefinition("task.resume", "Resume task", "Tasks", "Ctrl+R"),
        ActionDefinition("agent.stop", "Stop agent", "Agents", "Ctrl+Shift+S"),
        ActionDefinition("nav.tasks", "Open tasks", "Navigation", "Alt+1"),
        ActionDefinition("nav.agents", "Open agents", "Navigation", "Alt+2"),
        ActionDefinition("nav.logs", "Open logs", "Navigation", "Alt+3"),
        ActionDefinition("nav.workspaces", "Open workspaces", "Navigation", "Alt+4"),
        ActionDefinition("command.focus", "Open command search", "Navigation", "Ctrl+Shift+P"),
    ):
        registry.register(action)
    return registry
