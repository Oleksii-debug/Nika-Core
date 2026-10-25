from __future__ import annotations

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.action_registry import ActionDefinition, ActionRegistry, Keymap


def _definition() -> ActionDefinition:
    return ActionDefinition(
        "test.action",
        "Тестова дія",
        "Тест",
        "Ctrl+1",
        scope="app",
        may_be_unbound=False,
    )


def _registry() -> tuple[ActionRegistry, ActionDefinition]:
    definition = _definition()
    registry = ActionRegistry()
    registry.register(definition)
    return registry, definition


def test_register_detaches_caller_owned_definition() -> None:
    registry, definition = _registry()

    object.__setattr__(definition, "action_id", "forged.action")
    object.__setattr__(definition, "label", "Підроблено")
    object.__setattr__(definition, "scope", "forged")
    object.__setattr__(definition, "default_binding", "Ctrl+9")

    stored = registry.get("test.action")
    assert stored == _definition()
    with pytest.raises(KeyError, match="Unknown action"):
        registry.get("forged.action")
    assert registry.find_by_binding("Ctrl+1", "app") == _definition()
    assert registry.find_by_binding("Ctrl+9", "forged") is None


def test_get_returns_detached_action_snapshot() -> None:
    registry, _definition_owner = _registry()

    returned = registry.get("test.action")
    object.__setattr__(returned, "action_id", "forged.action")
    object.__setattr__(returned, "default_binding", "Ctrl+9")

    assert registry.get("test.action") == _definition()
    with pytest.raises(KeyError, match="Unknown action"):
        registry.get("forged.action")


def test_all_returns_detached_action_snapshots() -> None:
    registry, _definition_owner = _registry()

    returned = registry.all()
    assert len(returned) == 1
    object.__setattr__(returned[0], "scope", "forged")
    object.__setattr__(returned[0], "label", "Підроблено")

    assert registry.all() == (_definition(),)


def test_find_by_binding_returns_detached_action_snapshot() -> None:
    registry, _definition_owner = _registry()

    found = registry.find_by_binding("Control+1", "app")
    assert found == _definition()
    assert found is not None
    object.__setattr__(found, "default_binding", "Ctrl+9")
    object.__setattr__(found, "scope", "forged")

    assert registry.find_by_binding("Ctrl+1", "app") == _definition()
    assert registry.find_by_binding("Ctrl+9", "forged") is None


def test_keymap_identity_survives_external_definition_mutation(tmp_path) -> None:
    registry, definition = _registry()
    store = SQLiteStore(tmp_path / "ніка з пробілами.db")
    store.initialize()
    keymap = Keymap(store, registry)

    object.__setattr__(definition, "action_id", "forged.action")
    object.__setattr__(definition, "default_binding", "Ctrl+9")
    object.__setattr__(definition, "may_be_unbound", True)

    assert keymap.resolve("test.action") == "Ctrl+1"
    keymap.set_binding("test.action", "Shift+Control+Ї")
    assert keymap.resolve("test.action") == "Ctrl+Shift+Ї"
    with pytest.raises(KeyError, match="Unknown action"):
        keymap.resolve("forged.action")


@pytest.mark.parametrize(
    ("field", "value", "error_type", "message"),
    [
        ("action_id", "not-dotted", ValueError, "stable dotted identifier"),
        ("label", "", ValueError, "metadata must not be empty"),
        ("category", 7, TypeError, "metadata must be text"),
        ("default_binding", 7, TypeError, "default binding must be text"),
        ("may_be_unbound", 1, TypeError, "must be a boolean"),
    ],
)
def test_register_revalidates_definition_tampered_after_construction(
    field: str,
    value: object,
    error_type: type[Exception],
    message: str,
) -> None:
    definition = _definition()
    object.__setattr__(definition, field, value)
    registry = ActionRegistry()

    with pytest.raises(error_type, match=message):
        registry.register(definition)

    assert registry.all() == ()


def test_register_rejects_required_action_tampered_to_unbound() -> None:
    definition = _definition()
    object.__setattr__(definition, "default_binding", None)
    registry = ActionRegistry()

    with pytest.raises(ValueError, match="required action must have a default binding"):
        registry.register(definition)

    assert registry.all() == ()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("action_id", "test.\u202eaction"),
        ("label", "Cafe\u0301"),
        ("category", "Категорія\nприхована"),
        ("scope", "\ud800"),
    ],
)
def test_action_metadata_rejects_noncanonical_or_invisible_text(
    field: str,
    value: str,
) -> None:
    arguments = {
        "action_id": "test.action",
        "label": "Тестова дія",
        "category": "Тест",
        "default_binding": "Ctrl+1",
        "scope": "app",
        "may_be_unbound": False,
    }
    arguments[field] = value

    with pytest.raises(ValueError, match="canonical UTF-8 text"):
        ActionDefinition(**arguments)


def test_action_metadata_accepts_composed_ukrainian_and_emoji() -> None:
    definition = ActionDefinition(
        "test.action",
        "Дія Ніки 🧭",
        "Навігація",
        "Ctrl+Ї",
        scope="вікно",
    )

    registry = ActionRegistry()
    registry.register(definition)
    assert registry.get("test.action") == definition
