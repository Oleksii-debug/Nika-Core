from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from nika_core.data.sqlite import SQLiteStore
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
