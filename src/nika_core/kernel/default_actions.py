from __future__ import annotations

from nika_core.kernel.action_registry import ActionDefinition, ActionRegistry


def build_default_action_registry() -> ActionRegistry:
    registry = ActionRegistry()
    for action in (
        ActionDefinition(
            "task.create",
            "Створити завдання",
            "Завдання",
            "Ctrl+N",
            may_be_unbound=False,
        ),
        ActionDefinition("task.pause", "Призупинити завдання", "Завдання", "Ctrl+P"),
        ActionDefinition("task.resume", "Відновити завдання", "Завдання", "Ctrl+R"),
        ActionDefinition("agent.stop", "Зупинити агента", "Агенти", "Ctrl+Shift+S"),
        ActionDefinition("team.sources.configure", "Зберегти джерела команди", "Джерела", None),
        ActionDefinition(
            "settings.model.configure", "Зберегти модель", "Налаштування", None
        ),
        ActionDefinition(
            "settings.model.refresh", "Перечитати модель", "Налаштування", None
        ),
        ActionDefinition(
            "settings.autostart.configure", "Зберегти автозапуск", "Налаштування", None
        ),
        ActionDefinition(
            "settings.autostart.refresh", "Перечитати автозапуск", "Налаштування", None
        ),
        ActionDefinition("nav.tasks", "Відкрити завдання", "Навігація", "Alt+1"),
        ActionDefinition("nav.agents", "Відкрити агентів", "Навігація", "Alt+2"),
        ActionDefinition("nav.logs", "Відкрити журнал", "Навігація", "Alt+3"),
        ActionDefinition(
            "nav.workspaces", "Відкрити робочі простори", "Навігація", "Alt+4"
        ),
        ActionDefinition(
            "command.focus",
            "Відкрити пошук команд",
            "Навігація",
            "Ctrl+Shift+P",
        ),
    ):
        registry.register(action)
    return registry
