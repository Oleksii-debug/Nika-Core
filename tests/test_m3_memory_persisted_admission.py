from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog
from nika_core.memory import MemoryScope, MemoryService


def _memory(tmp_path: Path) -> tuple[SQLiteStore, MemoryService]:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    return store, MemoryService(store)


def _put(
    memory: MemoryService,
    *,
    scope: MemoryScope = MemoryScope.TASK,
    key: str = "entry",
) -> None:
    memory.put(
        scope=scope,
        owner_id="owner",
        namespace="scratch",
        key=key,
        value={"safe": True},
        user_approved=scope is MemoryScope.USER,
        expires_at=datetime(2038, 1, 1, tzinfo=UTC),
    )


@pytest.mark.parametrize(
    ("stored", "message"),
    [
        ("NaN", "invalid stored memory JSON constant"),
        ("Infinity", "invalid stored memory JSON constant"),
        ("-Infinity", "invalid stored memory JSON constant"),
        ("1e400", "invalid stored memory JSON number"),
        ("-1e400", "invalid stored memory JSON number"),
        ('{"nested": [1e400]}', "invalid stored memory JSON number"),
        ('{"nested": [NaN]}', "invalid stored memory JSON constant"),
        ('{"key": 1, "key": 2}', "duplicate stored memory JSON object key"),
        ('{"nested": {"key": 1, "key": 2}}', "duplicate stored memory JSON object key"),
    ],
)
def test_corrupt_stored_json_is_not_rehydrated(
    tmp_path: Path, stored: str, message: str
) -> None:
    store, memory = _memory(tmp_path)
    _put(memory)
    with store.connection() as conn:
        conn.execute(
            "UPDATE memory_records SET value_json = ? WHERE memory_key = 'entry'",
            (stored,),
        )
    with pytest.raises(ValueError, match=message):
        memory.get(scope=MemoryScope.TASK, owner_id="owner", namespace="scratch", key="entry")
    with pytest.raises(ValueError, match=message):
        memory.list_namespace(scope=MemoryScope.TASK, owner_id="owner", namespace="scratch")


@pytest.mark.parametrize(
    "stored",
    [
        '"\\ud800"',
        '"\\udfff"',
        '{"\\ud800": true}',
        '{"nested": ["\\udfff"]}',
    ],
)
def test_unpaired_surrogate_is_not_restored(tmp_path: Path, stored: str) -> None:
    store, memory = _memory(tmp_path)
    _put(memory)
    with store.connection() as conn:
        conn.execute(
            "UPDATE memory_records SET value_json = ? WHERE memory_key = 'entry'",
            (stored,),
        )
    with pytest.raises(ValueError, match="invalid Unicode"):
        memory.get(scope=MemoryScope.TASK, owner_id="owner", namespace="scratch", key="entry")


def test_valid_unicode_escape_pair_is_preserved(tmp_path: Path) -> None:
    store, memory = _memory(tmp_path)
    _put(memory)
    with store.connection() as conn:
        conn.execute(
            "UPDATE memory_records SET value_json = ? WHERE memory_key = 'entry'",
            ('"\\ud83d\\ude00"',),
        )
    record = memory.get(
        scope=MemoryScope.TASK, owner_id="owner", namespace="scratch", key="entry"
    )
    assert record is not None and record.value == "😀"


def test_non_text_json_blob_is_rejected_before_rehydration(tmp_path: Path) -> None:
    store, memory = _memory(tmp_path)
    _put(memory)
    with store.connection() as conn:
        conn.execute(
            "UPDATE memory_records SET value_json = X'7B7D' WHERE memory_key = 'entry'"
        )
    with pytest.raises(ValueError, match="JSON must be text"):
        memory.get(scope=MemoryScope.TASK, owner_id="owner", namespace="scratch", key="entry")


def test_user_record_without_stored_approval_is_rejected(tmp_path: Path) -> None:
    store, memory = _memory(tmp_path)
    _put(memory, scope=MemoryScope.USER)
    with store.connection() as conn:
        conn.execute(
            "UPDATE memory_records SET user_approved = 0 WHERE memory_key = 'entry'"
        )
    with pytest.raises(ValueError, match="lacks durable explicit approval"):
        memory.get(scope=MemoryScope.USER, owner_id="owner", namespace="scratch", key="entry")
    with pytest.raises(ValueError, match="lacks durable explicit approval"):
        memory.list_namespace(scope=MemoryScope.USER, owner_id="owner", namespace="scratch")


def test_corrupt_namespace_read_rolls_back_expiry_cleanup(tmp_path: Path) -> None:
    store, memory = _memory(tmp_path)
    _put(memory, key="corrupt")
    _put(memory, key="expired")
    with store.connection() as conn:
        conn.execute(
            "UPDATE memory_records SET expires_at = ? WHERE memory_key = 'expired'",
            (datetime(2035, 1, 1, tzinfo=UTC).isoformat(),),
        )
        conn.execute(
            "UPDATE memory_records SET value_json = 'NaN' WHERE memory_key = 'corrupt'"
        )
    with pytest.raises(ValueError, match="invalid stored memory JSON constant"):
        memory.list_namespace(
            scope=MemoryScope.TASK,
            owner_id="owner",
            namespace="scratch",
            now=datetime(2036, 1, 1, tzinfo=UTC),
        )
    with store.connection() as conn:
        keys = [
            row[0] for row in conn.execute(
                "SELECT memory_key FROM memory_records ORDER BY memory_key"
            )
        ]
    assert keys == ["corrupt", "expired"]


def test_valid_user_and_task_records_survive_restart(tmp_path: Path) -> None:
    store, memory = _memory(tmp_path)
    _put(memory, scope=MemoryScope.USER, key="user")
    _put(memory, scope=MemoryScope.TASK, key="task")
    restarted = MemoryService(store)
    for scope, key, approved in (
        (MemoryScope.USER, "user", True),
        (MemoryScope.TASK, "task", False),
    ):
        record = restarted.get(
            scope=scope, owner_id="owner", namespace="scratch", key=key
        )
        assert record is not None
        assert record.value == {"safe": True}
        assert record.user_approved is approved


@pytest.mark.parametrize(
    "value",
    [
        "\ud800",
        {"nested": ["\udfff"]},
        {"\ud800": "invalid key"},
    ],
)
def test_invalid_unicode_write_preserves_existing_record(tmp_path: Path, value: object) -> None:
    store, memory = _memory(tmp_path)
    _put(memory)
    with pytest.raises(ValueError, match="memory JSON contains invalid Unicode"):
        memory.put(
            scope=MemoryScope.TASK,
            owner_id="owner",
            namespace="scratch",
            key="entry",
            value=value,
        )
    record = memory.get(
        scope=MemoryScope.TASK, owner_id="owner", namespace="scratch", key="entry"
    )
    assert record is not None and record.value == {"safe": True}
    with store.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM memory_records").fetchone()[0] == 1


def test_finite_exponent_still_rehydrates_after_restart(tmp_path: Path) -> None:
    store, memory = _memory(tmp_path)
    _put(memory)
    with store.connection() as conn:
        conn.execute(
            "UPDATE memory_records SET value_json = ? WHERE memory_key = 'entry'",
            ('{"range": [1e308, -1e308, 0.125]}',),
        )
    restarted = MemoryService(store)
    record = restarted.get(
        scope=MemoryScope.TASK, owner_id="owner", namespace="scratch", key="entry"
    )
    assert record is not None
    assert record.value == {"range": [1e308, -1e308, 0.125]}


@pytest.mark.parametrize("invalid_scope", ["user", None, SimpleNamespace(value="user")])
def test_fake_user_scope_cannot_bypass_write_approval(
    tmp_path: Path, invalid_scope: object
) -> None:
    store, memory = _memory(tmp_path)
    with pytest.raises(ValueError, match="scope must be a MemoryScope"):
        memory.put(
            scope=invalid_scope,
            owner_id="owner",
            namespace="scratch",
            key="entry",
            value={"unapproved": True},
            user_approved=False,
        )
    with store.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM memory_records").fetchone()[0] == 0


@pytest.mark.parametrize("operation", ["get", "list", "delete"])
def test_fake_scope_cannot_read_or_delete_memory(tmp_path: Path, operation: str) -> None:
    store, memory = _memory(tmp_path)
    _put(memory, scope=MemoryScope.USER)
    fake_scope = SimpleNamespace(value="user")
    with pytest.raises(ValueError, match="scope must be a MemoryScope"):
        if operation == "get":
            memory.get(
                scope=fake_scope, owner_id="owner", namespace="scratch", key="entry"
            )
        elif operation == "list":
            memory.list_namespace(
                scope=fake_scope, owner_id="owner", namespace="scratch"
            )
        else:
            memory.delete(
                scope=fake_scope, owner_id="owner", namespace="scratch", key="entry"
            )
    record = memory.get(
        scope=MemoryScope.USER, owner_id="owner", namespace="scratch", key="entry"
    )
    assert record is not None and record.user_approved is True


@pytest.mark.parametrize(
    ("field", "invalid", "message"),
    [
        ("owner_id", 123, "owner_id must be text"),
        ("namespace", None, "namespace must be text"),
        ("key", [], "key must be text"),
        ("owner_id", "\ud800", "owner_id must be valid UTF-8"),
        ("namespace", "\udfff", "namespace must be valid UTF-8"),
        ("key", "\ud800", "key must be valid UTF-8"),
    ],
)
def test_invalid_write_identity_preserves_existing_record(
    tmp_path: Path, field: str, invalid: object, message: str
) -> None:
    store, memory = _memory(tmp_path)
    _put(memory)
    params = {
        "scope": MemoryScope.TASK,
        "owner_id": "owner",
        "namespace": "scratch",
        "key": "entry",
        "value": "replacement",
    }
    params[field] = invalid
    with pytest.raises(ValueError, match=message):
        memory.put(**params)
    with store.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM memory_records").fetchone()[0] == 1
    assert memory.get(
        scope=MemoryScope.TASK, owner_id="owner", namespace="scratch", key="entry"
    ).value == {"safe": True}


def test_public_identity_admission_is_consistent_across_crud(tmp_path: Path) -> None:
    _store, memory = _memory(tmp_path)
    memory.put(
        scope=MemoryScope.TASK,
        owner_id=" owner ",
        namespace=" scratch ",
        key=" entry ",
        value={"safe": True},
    )

    record = memory.get(
        scope=MemoryScope.TASK,
        owner_id=" owner ",
        namespace=" scratch ",
        key=" entry ",
    )
    assert record is not None
    assert (record.owner_id, record.namespace, record.key) == (
        "owner",
        "scratch",
        "entry",
    )
    assert [
        item.key
        for item in memory.list_namespace(
            scope=MemoryScope.TASK,
            owner_id=" owner ",
            namespace=" scratch ",
        )
    ] == ["entry"]
    assert memory.delete(
        scope=MemoryScope.TASK,
        owner_id=" owner ",
        namespace=" scratch ",
        key=" entry ",
    )
    assert memory.get(
        scope=MemoryScope.TASK,
        owner_id="owner",
        namespace="scratch",
        key="entry",
    ) is None


@pytest.mark.parametrize("operation", ["get", "list", "delete"])
def test_public_read_delete_identity_rejects_invalid_unicode(
    tmp_path: Path, operation: str
) -> None:
    _store, memory = _memory(tmp_path)
    _put(memory)
    with pytest.raises(ValueError, match="must be valid UTF-8"):
        if operation == "get":
            memory.get(
                scope=MemoryScope.TASK,
                owner_id="owner",
                namespace="scratch",
                key="\ud800",
            )
        elif operation == "list":
            memory.list_namespace(
                scope=MemoryScope.TASK,
                owner_id="\ud800",
                namespace="scratch",
            )
        else:
            memory.delete(
                scope=MemoryScope.TASK,
                owner_id="owner",
                namespace="scratch",
                key="\ud800",
            )
    assert memory.get(
        scope=MemoryScope.TASK,
        owner_id="owner",
        namespace="scratch",
        key="entry",
    ) is not None


@pytest.mark.parametrize(
    ("field", "stored", "operation", "message"),
    [
        ("memory_key", b"entry", "list", "stored memory key must be text"),
        (
            "created_at",
            b"2038-01-01T00:00:00+00:00",
            "get",
            "stored memory created_at must be text",
        ),
        (
            "updated_at",
            "2038-01-01T00:00:00",
            "get",
            "stored memory updated_at must be timezone-aware",
        ),
        (
            "expires_at",
            b"2038-01-01T00:00:00+00:00",
            "get",
            "stored memory expiry must be text",
        ),
    ],
)
def test_corrupt_persisted_identity_and_datetime_carriers_fail_closed(
    tmp_path: Path,
    field: str,
    stored: object,
    operation: str,
    message: str,
) -> None:
    store, memory = _memory(tmp_path)
    _put(memory)
    with store.connection() as conn:
        conn.execute(
            f"UPDATE memory_records SET {field} = ? WHERE memory_key = 'entry'",
            (stored,),
        )

    with pytest.raises(ValueError, match=message):
        if operation == "list":
            memory.list_namespace(
                scope=MemoryScope.TASK,
                owner_id="owner",
                namespace="scratch",
            )
        else:
            memory.get(
                scope=MemoryScope.TASK,
                owner_id="owner",
                namespace="scratch",
                key="entry",
            )


def test_noncanonical_persisted_key_is_not_returned(tmp_path: Path) -> None:
    store, memory = _memory(tmp_path)
    _put(memory)
    with store.connection() as conn:
        conn.execute(
            "UPDATE memory_records SET memory_key = ' entry ' "
            "WHERE memory_key = 'entry'"
        )
    with pytest.raises(ValueError, match="stored memory key is not canonical"):
        memory.list_namespace(
            scope=MemoryScope.TASK,
            owner_id="owner",
            namespace="scratch",
        )


def test_corrupt_created_at_blocks_put_before_mutation(tmp_path: Path) -> None:
    store, memory = _memory(tmp_path)
    _put(memory)
    with store.connection() as conn:
        conn.execute(
            "UPDATE memory_records SET created_at = ? WHERE memory_key = 'entry'",
            ("2038-01-01T00:00:00",),
        )

    with pytest.raises(ValueError, match="stored memory created_at must be timezone-aware"):
        memory.put(
            scope=MemoryScope.TASK,
            owner_id="owner",
            namespace="scratch",
            key="entry",
            value={"replacement": True},
        )

    with store.connection() as conn:
        row = conn.execute(
            "SELECT value_json, created_at FROM memory_records "
            "WHERE memory_key = 'entry'"
        ).fetchone()
        assert row["value_json"] == '{"safe":true}'
        assert row["created_at"] == "2038-01-01T00:00:00"


@pytest.mark.parametrize(
    ("scope", "column", "stored", "message"),
    [
        (
            MemoryScope.TASK,
            "value_json",
            "NaN",
            "invalid stored memory JSON constant",
        ),
        (
            MemoryScope.TASK,
            "updated_at",
            "2038-01-01T00:00:00",
            "stored memory updated_at must be timezone-aware",
        ),
        (
            MemoryScope.TASK,
            "expires_at",
            b"2038-01-01T00:00:00+00:00",
            "stored memory expiry must be text",
        ),
        (
            MemoryScope.USER,
            "user_approved",
            0,
            "user memory lacks durable explicit approval",
        ),
    ],
)
def test_put_preserves_corrupt_existing_record_before_replacement(
    tmp_path: Path,
    scope: MemoryScope,
    column: str,
    stored: object,
    message: str,
) -> None:
    store, memory = _memory(tmp_path)
    _put(memory, scope=scope)
    with store.connection() as conn:
        conn.execute(
            f"UPDATE memory_records SET {column} = ? WHERE memory_key = 'entry'",
            (stored,),
        )

    with pytest.raises(ValueError, match=message):
        memory.put(
            scope=scope,
            owner_id="owner",
            namespace="scratch",
            key="entry",
            value={"replacement": True},
            user_approved=scope is MemoryScope.USER,
        )

    with store.connection() as conn:
        row = conn.execute(
            f"SELECT {column}, value_json FROM memory_records "
            "WHERE memory_key = 'entry'"
        ).fetchone()
    assert row is not None
    assert row[column] == stored
    if column != "value_json":
        assert row["value_json"] == '{"safe":true}'


def test_rejected_replacement_put_does_not_emit_second_upsert_audit(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    audit = AuditLog(store)
    memory = MemoryService(store, audit)
    _put(memory)
    with store.connection() as conn:
        conn.execute(
            "UPDATE memory_records SET value_json = 'NaN' "
            "WHERE memory_key = 'entry'"
        )

    with pytest.raises(ValueError, match="invalid stored memory JSON constant"):
        memory.put(
            scope=MemoryScope.TASK,
            owner_id="owner",
            namespace="scratch",
            key="entry",
            value={"replacement": True},
        )

    events = audit.list_for(
        entity_type="memory",
        entity_id="task:owner:scratch:entry",
    )
    assert [event.event_type for event in events] == ["memory.upserted"]


@pytest.mark.parametrize(
    "invalid",
    [
        "bad\x00key",
        "bad\nkey",
        "bad\u200bkey",
        "bad\u2028key",
    ],
)
def test_ambiguous_identity_characters_fail_before_memory_effect(
    tmp_path: Path, invalid: str
) -> None:
    store, memory = _memory(tmp_path)
    with pytest.raises(ValueError, match="control or invisible"):
        memory.put(
            scope=MemoryScope.TASK,
            owner_id="owner",
            namespace="scratch",
            key=invalid,
            value={"unsafe": True},
        )
    with store.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM memory_records").fetchone()[0] == 0


def test_normal_unicode_memory_identity_remains_supported(tmp_path: Path) -> None:
    _store, memory = _memory(tmp_path)
    record = memory.put(
        scope=MemoryScope.TASK,
        owner_id="користувач один",
        namespace="нотатки шахи",
        key="позиція № 1",
        value={"мова": "українська"},
    )
    assert (record.owner_id, record.namespace, record.key) == (
        "користувач один",
        "нотатки шахи",
        "позиція № 1",
    )


def test_get_does_not_delete_expired_record_before_full_carrier_validation(
    tmp_path: Path,
) -> None:
    store, memory = _memory(tmp_path)
    _put(memory)
    with store.connection() as conn:
        conn.execute(
            "UPDATE memory_records SET expires_at = ?, updated_at = ? "
            "WHERE memory_key = 'entry'",
            (
                datetime(2035, 1, 1, tzinfo=UTC).isoformat(),
                "2038-01-01T00:00:00",
            ),
        )

    with pytest.raises(ValueError, match="stored memory updated_at must be timezone-aware"):
        memory.get(
            scope=MemoryScope.TASK,
            owner_id="owner",
            namespace="scratch",
            key="entry",
            now=datetime(2036, 1, 1, tzinfo=UTC),
        )

    with store.connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM memory_records WHERE memory_key = 'entry'"
        ).fetchone()[0] == 1


def test_list_does_not_delete_expired_record_with_corrupt_persisted_key(
    tmp_path: Path,
) -> None:
    store, memory = _memory(tmp_path)
    _put(memory)
    with store.connection() as conn:
        conn.execute(
            "UPDATE memory_records SET expires_at = ?, memory_key = ? "
            "WHERE memory_key = 'entry'",
            (datetime(2035, 1, 1, tzinfo=UTC).isoformat(), b"entry"),
        )

    with pytest.raises(ValueError, match="stored memory key must be text"):
        memory.list_namespace(
            scope=MemoryScope.TASK,
            owner_id="owner",
            namespace="scratch",
            now=datetime(2036, 1, 1, tzinfo=UTC),
        )

    with store.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM memory_records").fetchone()[0] == 1


def test_purge_does_not_delete_expired_record_before_full_carrier_validation(
    tmp_path: Path,
) -> None:
    store, memory = _memory(tmp_path)
    _put(memory)
    with store.connection() as conn:
        conn.execute(
            "UPDATE memory_records SET expires_at = ?, value_json = 'NaN' "
            "WHERE memory_key = 'entry'",
            (datetime(2035, 1, 1, tzinfo=UTC).isoformat(),),
        )

    with pytest.raises(ValueError, match="invalid stored memory JSON constant"):
        memory.purge_expired(now=datetime(2036, 1, 1, tzinfo=UTC))

    with store.connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM memory_records WHERE memory_key = 'entry'"
        ).fetchone()[0] == 1


@pytest.mark.parametrize(
    ("scope", "column", "stored", "message"),
    [
        (
            MemoryScope.TASK,
            "value_json",
            "NaN",
            "invalid stored memory JSON constant",
        ),
        (
            MemoryScope.TASK,
            "updated_at",
            "2038-01-01T00:00:00",
            "stored memory updated_at must be timezone-aware",
        ),
        (
            MemoryScope.USER,
            "user_approved",
            0,
            "user memory lacks durable explicit approval",
        ),
    ],
)
def test_explicit_delete_preserves_corrupt_durable_record(
    tmp_path: Path,
    scope: MemoryScope,
    column: str,
    stored: object,
    message: str,
) -> None:
    store, memory = _memory(tmp_path)
    _put(memory, scope=scope)
    with store.connection() as conn:
        conn.execute(
            f"UPDATE memory_records SET {column} = ? WHERE memory_key = 'entry'",
            (stored,),
        )

    with pytest.raises(ValueError, match=message):
        memory.delete(
            scope=scope,
            owner_id="owner",
            namespace="scratch",
            key="entry",
        )

    with store.connection() as conn:
        row = conn.execute(
            "SELECT value_json, user_approved, updated_at FROM memory_records "
            "WHERE memory_key = 'entry'"
        ).fetchone()
    assert row is not None
    assert row[column] == stored

def test_rejected_explicit_delete_does_not_emit_deleted_audit(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    audit = AuditLog(store)
    memory = MemoryService(store, audit)
    _put(memory)
    with store.connection() as conn:
        conn.execute(
            "UPDATE memory_records SET value_json = 'NaN' "
            "WHERE memory_key = 'entry'"
        )

    with pytest.raises(ValueError, match="invalid stored memory JSON constant"):
        memory.delete(
            scope=MemoryScope.TASK,
            owner_id="owner",
            namespace="scratch",
            key="entry",
        )

    events = audit.list_for(
        entity_type="memory",
        entity_id="task:owner:scratch:entry",
    )
    assert [event.event_type for event in events] == ["memory.upserted"]

