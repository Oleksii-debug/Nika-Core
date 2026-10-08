from __future__ import annotations

from nika_core.kernel.default_actions import build_default_action_registry


def test_default_action_metadata_matches_ukrainian_webview_language() -> None:
    actions = {action.action_id: action for action in build_default_action_registry().all()}

    expected = {
        "task.create": ("Створити завдання", "Завдання", "Ctrl+N", False),
        "task.pause": ("Призупинити завдання", "Завдання", "Ctrl+P", True),
        "task.resume": ("Відновити завдання", "Завдання", "Ctrl+R", True),
        "agent.stop": ("Зупинити агента", "Агенти", "Ctrl+Shift+S", True),
        "team.sources.configure": (
            "Зберегти джерела команди",
            "Джерела",
            None,
            True,
        ),
        "product.factory.execution_plan.load": (
            "Завантажити план виконання Product Factory",
            "Product Factory",
            None,
            True,
        ),
        "settings.model.configure": (
            "Зберегти модель",
            "Налаштування",
            None,
            True,
        ),
        "settings.model.refresh": (
            "Перечитати модель",
            "Налаштування",
            None,
            True,
        ),
        "settings.autostart.configure": (
            "Зберегти автозапуск",
            "Налаштування",
            None,
            True,
        ),
        "settings.autostart.refresh": (
            "Перечитати автозапуск",
            "Налаштування",
            None,
            True,
        ),
        "nav.tasks": ("Відкрити завдання", "Навігація", "Alt+1", True),
        "nav.agents": ("Відкрити агентів", "Навігація", "Alt+2", True),
        "nav.logs": ("Відкрити журнал", "Навігація", "Alt+3", True),
        "nav.workspaces": (
            "Відкрити робочі простори",
            "Навігація",
            "Alt+4",
            True,
        ),
        "command.focus": (
            "Відкрити пошук команд",
            "Навігація",
            "Ctrl+Shift+P",
            True,
        ),
    }

    assert set(actions) == set(expected)
    for action_id, (label, category, binding, may_be_unbound) in expected.items():
        action = actions[action_id]
        assert action.label == label
        assert action.category == category
        assert action.default_binding == binding
        assert action.may_be_unbound is may_be_unbound
        assert action.scope == "app"


def test_default_action_metadata_has_no_legacy_english_ui_tokens() -> None:
    forbidden = {
        "Create task",
        "Pause task",
        "Resume task",
        "Stop agent",
        "Open tasks",
        "Open agents",
        "Open logs",
        "Open workspaces",
        "Open command search",
        "Tasks",
        "Agents",
        "Navigation",
    }

    for action in build_default_action_registry().all():
        assert action.label not in forbidden
        assert action.category not in forbidden
