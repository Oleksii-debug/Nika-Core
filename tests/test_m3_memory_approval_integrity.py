from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.memory import MemoryScope, MemoryService


def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    return store


@pytest.mark.parametrize("approval", [1, "yes", None])
def test_user_long_term_write_requires_literal_boolean_approval(
    tmp_path: Path,
    approval: object,
) -> None:
    memory = MemoryService(_store(tmp_path))
    with pytest.raises(TypeError, match="user_approved must be a boolean"):
        memory.put(
            scope=MemoryScope.USER_LONG_TERM,
            owner_id="user-1",
            namespace="preferences",
            key="language",
            value="uk",
            user_approved=approval,
        )


def test_invalid_approval_type_fails_before_non_user_memory_write(tmp_path: Path) -> None:
    store = _store(tmp_path)
    memory = MemoryService(store)
    with pytest.raises(TypeError, match="user_approved must be a boolean"):
        memory.put(
            scope=MemoryScope.WORKSPACE,
            owner_id="workspace-1",
            namespace="scratch",
            key="x",
            value=1,
            user_approved=1,
        )
    assert memory.get(
        scope=MemoryScope.WORKSPACE,
        owner_id="workspace-1",
        namespace="scratch",
        key="x",
    ) is None


@pytest.mark.parametrize("corrupt_flag", [0, 2])
def test_corrupt_durable_user_approval_fails_closed(
    tmp_path: Path,
    corrupt_flag: int,
) -> None:
    store = _store(tmp_path)
    memory = MemoryService(store)
    memory.put(
        scope=MemoryScope.USER_LONG_TERM,
        owner_id="user-1",
        namespace="preferences",
        key="language",
        value="uk",
        user_approved=True,
    )

    with sqlite3.connect(store.path) as conn:
        if corrupt_flag not in {0, 1}:
            conn.execute("PRAGMA ignore_check_constraints = ON")
        conn.execute(
            """UPDATE memory_records
            SET user_approved = ?
            WHERE scope = ? AND owner_id = ? AND namespace = ? AND memory_key = ?""",
            (
                corrupt_flag,
                MemoryScope.USER.value,
                "user-1",
                "preferences",
                "language",
            ),
        )

    with pytest.raises(RuntimeError, match="approval"):
        memory.get(
            scope=MemoryScope.USER_LONG_TERM,
            owner_id="user-1",
            namespace="preferences",
            key="language",
        )
