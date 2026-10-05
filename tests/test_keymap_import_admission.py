from __future__ import annotations

import json
import sqlite3

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.action_registry import ActionDefinition, ActionRegistry, Keymap


@pytest.fixture
def keymap(tmp_path) -> Keymap:
    store = SQLiteStore(tmp_path / "ніка з пробілами.db")
    store.initialize()
    actions = ActionRegistry()
    actions.register(ActionDefinition("test.first", "Почати", "Тест", "Ctrl+1"))
    actions.register(ActionDefinition("test.second", "Завершити", "Тест", "Ctrl+2"))
    return Keymap(store, actions)


@pytest.mark.parametrize(
    "document",
    [
        '{"format_version":1,"format_version":1,"bindings":{}}',
        '{"format_version":1,"bindings":{"test.first":"Ctrl+3","test.first":"Ctrl+4"}}',
        '{"format_version":true,"bindings":{"test.first":"Ctrl+3"}}',
        '{"format_version":1.0,"bindings":{"test.first":"Ctrl+3"}}',
        '{"format_version":NaN,"bindings":{"test.first":"Ctrl+3"}}',
        '{"format_version":1e400,"bindings":{"test.first":"Ctrl+3"}}',
        '{"format_version":1,"bindings":{"test.first":Infinity}}',
        '{"format_version":1,"bindings":{"test.first":null,"test.first":"Ctrl+3"}}',
        '{"format_version":1,"bindings":[]}',
        '[]',
        'null',
    ],
)
def test_ambiguous_or_invalid_documents_cannot_change_keymap(
    keymap: Keymap, document: str
) -> None:
    with pytest.raises((TypeError, ValueError)):
        keymap.import_json(document)

    assert keymap.resolve("test.first") == "Ctrl+1"
    assert keymap.resolve("test.second") == "Ctrl+2"
    with keymap._store.connection() as conn:
        count = conn.execute("SELECT COUNT(*) FROM keymap_overrides").fetchone()[0]
    assert count == 0


@pytest.mark.parametrize("document", [None, b'{"format_version":1,"bindings":{}}', 17])
def test_nontext_import_is_rejected_without_database_effect(
    keymap: Keymap, document: object
) -> None:
    with pytest.raises(TypeError, match="keymap import must be text"):
        keymap.import_json(document)  # type: ignore[arg-type]
    assert keymap.resolve("test.first") == "Ctrl+1"


def test_unicode_invalid_import_is_rejected_without_database_effect(keymap: Keymap) -> None:
    with pytest.raises(ValueError, match="valid UTF-8"):
        keymap.import_json('{"format_version":1,"bindings":{},"note":"' + chr(0xD800) + '"}')
    assert keymap.resolve("test.first") == "Ctrl+1"


@pytest.mark.parametrize(
    "document",
    [
        " " * 1_048_577,
        '{"format_version":1,"bindings":{},"note":"' + "ї" * 524_280 + '"}',
    ],
)
def test_oversize_ascii_or_utf8_import_fails_before_parsing(
    keymap: Keymap, document: str
) -> None:
    with pytest.raises(ValueError, match="byte limit"):
        keymap.import_json(document)
    assert keymap.resolve("test.first") == "Ctrl+1"


def test_overdeep_input_is_a_controlled_error_and_cannot_write(keymap: Keymap) -> None:
    document = "[" * 1200 + "0" + "]" * 1200
    with pytest.raises((ValueError, TypeError)):
        keymap.import_json(document)
    assert keymap.resolve("test.first") == "Ctrl+1"


def test_valid_partial_import_and_existing_export_remain_compatible(keymap: Keymap) -> None:
    keymap.import_json(
        json.dumps({"format_version":1, "bindings":{"test.first":"Shift+Control+K"}})
    )
    assert keymap.resolve("test.first") == "Ctrl+Shift+K"
    assert keymap.resolve("test.second") == "Ctrl+2"
    exported = json.loads(keymap.export_json())
    assert exported["format_version"] == 1
    assert exported["bindings"]["test.first"] == "Ctrl+Shift+K"

@pytest.mark.parametrize(
    "document",
    [
        r'{"format_version":1,"bindings":{"test.first":"Ctrl+\ud800"}}',
        r'{"format_version":1,"bindings":{"test.\udfff":"Ctrl+3"}}',
        r'{"format_version":1,"bindings":{},"note":"\ud800"}',
    ],
)
def test_json_escaped_invalid_unicode_fails_before_sqlite(
    keymap: Keymap, document: str
) -> None:
    with pytest.raises(ValueError, match="valid UTF-8"):
        keymap.import_json(document)
    assert keymap.resolve("test.first") == "Ctrl+1"
    with keymap._store.connection() as conn:
        count = conn.execute("SELECT COUNT(*) FROM keymap_overrides").fetchone()[0]
    assert count == 0


def test_valid_json_escaped_surrogate_pair_is_accepted(keymap: Keymap) -> None:
    keymap.import_json(r'{"format_version":1,"bindings":{},"note":"\ud83d\ude00"}')
    assert keymap.resolve("test.first") == "Ctrl+1"


@pytest.mark.parametrize(
    "binding",
    [
        "Ctrl+\x00K",
        "Ctrl+\x85K",
        "Ctrl+\n+K",
        "Ctrl+\u202eK",
        "Ctrl+\u200dK",
        "Ctrl+\ud800K",
    ],
)
def test_invalid_direct_shortcut_is_rejected_before_sqlite(
    keymap: Keymap, binding: str
) -> None:
    with pytest.raises(ValueError, match="shortcut binding"):
        keymap.set_binding("test.first", binding)
    assert keymap.resolve("test.first") == "Ctrl+1"
    with keymap._store.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM keymap_overrides").fetchone()[0] == 0


def test_nontext_direct_binding_is_a_controlled_error(keymap: Keymap) -> None:
    with pytest.raises(TypeError, match="shortcut binding must be text"):
        keymap.set_binding("test.first", 1)  # type: ignore[arg-type]
    assert keymap.resolve("test.first") == "Ctrl+1"


def test_valid_ukrainian_primary_key_remains_supported(keymap: Keymap) -> None:
    keymap.set_binding("test.first", "Shift+Control+Ї")
    assert keymap.resolve("test.first") == "Ctrl+Shift+Ї"


def test_corrupt_persisted_binding_blocks_read_and_mutation(keymap: Keymap) -> None:
    with keymap._store.connection() as conn:
        conn.execute(
            "INSERT INTO keymap_overrides(action_id, binding, updated_at) VALUES (?, ?, ?)",
            ("test.first", "Ctrl+\x00K", "corrupt"),
        )
    with pytest.raises(ValueError, match="unsupported control characters"):
        keymap.resolve("test.first")
    with pytest.raises(ValueError, match="unsupported control characters"):
        keymap.set_binding("test.second", "Ctrl+K")
    with keymap._store.connection() as conn:
        rows = conn.execute("SELECT action_id, binding FROM keymap_overrides").fetchall()
    assert [(row["action_id"], row["binding"]) for row in rows] == [
        ("test.first", "Ctrl+\x00K")
    ]


def test_corrupt_persisted_action_identity_cannot_be_coerced_away(
    keymap: Keymap,
) -> None:
    with keymap._store.connection() as conn:
        conn.execute(
            "INSERT INTO keymap_overrides(action_id, binding, updated_at) VALUES (?, ?, ?)",
            (sqlite3.Binary(b"test.first"), "Ctrl+K", "corrupt"),
        )
    with pytest.raises(TypeError, match="stored keymap action ID must be text"):
        keymap.export_json()
    with pytest.raises(TypeError, match="stored keymap action ID must be text"):
        keymap.import_json('{"format_version":1,"bindings":{"test.second":"Ctrl+K"}}')
    with keymap._store.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM keymap_overrides").fetchone()[0] == 1


@pytest.mark.parametrize("binding", ["Ctrl+" + "K" * 252, "Ctrl+" + "ї" * 126])
def test_oversized_direct_binding_is_rejected_without_sqlite_effect(
    keymap: Keymap, binding: str
) -> None:
    with pytest.raises(ValueError, match="shortcut binding exceeds the byte limit"):
        keymap.set_binding("test.first", binding)
    assert keymap.resolve("test.first") == "Ctrl+1"
    with keymap._store.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM keymap_overrides").fetchone()[0] == 0


def test_oversized_imported_binding_is_rejected_without_sqlite_effect(keymap: Keymap) -> None:
    payload = json.dumps({"format_version": 1, "bindings": {"test.first": "K" * 257}})
    with pytest.raises(ValueError, match="shortcut binding exceeds the byte limit"):
        keymap.import_json(payload)
    assert keymap.resolve("test.first") == "Ctrl+1"
    with keymap._store.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM keymap_overrides").fetchone()[0] == 0
