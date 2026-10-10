from datetime import UTC, datetime, timedelta
from pathlib import Path

from nika_core.data.sqlite import SQLiteStore
from nika_core.memory.contracts import MemoryScope
from nika_core.memory.service import MemoryService


def _service(db_path: Path) -> tuple[SQLiteStore, MemoryService]:
    store = SQLiteStore(db_path)
    store.initialize()
    return store, MemoryService(store)


def test_namespace_read_does_not_purge_unrelated_expired_memory(tmp_path: Path) -> None:
    store, memory = _service(tmp_path / "nika.db")
    baseline = datetime.now(UTC)

    memory.put(
        scope=MemoryScope.WORKSPACE,
        owner_id="workspace-a",
        namespace="notes",
        key="live",
        value={"text": "keep"},
        expires_at=baseline + timedelta(days=60),
    )
    memory.put(
        scope=MemoryScope.AGENT,
        owner_id="agent-b",
        namespace="private",
        key="unrelated",
        value={"text": "separate"},
        expires_at=baseline + timedelta(days=30),
    )

    observed_at = baseline + timedelta(days=45)
    records = memory.list_namespace(
        scope=MemoryScope.WORKSPACE,
        owner_id="workspace-a",
        namespace="notes",
        now=observed_at,
    )

    assert [record.key for record in records] == ["live"]
    with store.connection() as conn:
        unrelated = conn.execute(
            "SELECT 1 FROM memory_records WHERE scope = ? AND owner_id = ? "
            "AND namespace = ? AND memory_key = ?",
            (MemoryScope.AGENT.value, "agent-b", "private", "unrelated"),
        ).fetchone()
    assert unrelated is not None

    assert memory.purge_expired(now=observed_at) == 1
    with store.connection() as conn:
        remaining = conn.execute(
            "SELECT scope, owner_id, namespace, memory_key FROM memory_records"
        ).fetchall()
    assert [tuple(row) for row in remaining] == [
        (MemoryScope.WORKSPACE.value, "workspace-a", "notes", "live")
    ]


def test_namespace_read_omits_own_expired_records_without_deleting_them(tmp_path: Path) -> None:
    store, memory = _service(tmp_path / "nika.db")
    baseline = datetime.now(UTC)

    memory.put(
        scope=MemoryScope.WORKSPACE,
        owner_id="workspace-a",
        namespace="notes",
        key="expired",
        value={"text": "old"},
        expires_at=baseline + timedelta(days=30),
    )
    memory.put(
        scope=MemoryScope.WORKSPACE,
        owner_id="workspace-a",
        namespace="notes",
        key="live",
        value={"text": "new"},
        expires_at=baseline + timedelta(days=60),
    )

    observed_at = baseline + timedelta(days=45)
    records = memory.list_namespace(
        scope=MemoryScope.WORKSPACE,
        owner_id="workspace-a",
        namespace="notes",
        now=observed_at,
    )

    assert [record.key for record in records] == ["live"]
    with store.connection() as conn:
        keys = [
            row["memory_key"]
            for row in conn.execute(
                "SELECT memory_key FROM memory_records "
                "WHERE scope = ? AND owner_id = ? AND namespace = ? ORDER BY memory_key",
                (MemoryScope.WORKSPACE.value, "workspace-a", "notes"),
            ).fetchall()
        ]
    assert keys == ["expired", "live"]


def test_namespace_read_is_exact_after_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "nika.db"
    _, memory = _service(db_path)

    identities = (
        (MemoryScope.TASK, "task-a", "context", "target"),
        (MemoryScope.TASK, "task-a", "other", "same-owner-other-namespace"),
        (MemoryScope.TASK, "task-b", "context", "same-namespace-other-owner"),
        (MemoryScope.AGENT, "task-a", "context", "same-owner-other-scope"),
    )
    for scope, owner_id, namespace, marker in identities:
        memory.put(
            scope=scope,
            owner_id=owner_id,
            namespace=namespace,
            key="shared-key",
            value={"marker": marker},
        )
    memory.put(
        scope=MemoryScope.USER,
        owner_id="task-a",
        namespace="context",
        key="shared-key",
        value={"marker": "user-context"},
        user_approved=True,
    )

    _, restarted = _service(db_path)
    records = restarted.list_namespace(
        scope=MemoryScope.TASK,
        owner_id="task-a",
        namespace="context",
    )

    assert [(record.key, record.value) for record in records] == [
        ("shared-key", {"marker": "target"})
    ]


def test_lazy_expiry_cleanup_deletes_only_exact_identity_after_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "nika.db"
    _, memory = _service(db_path)
    baseline = datetime.now(UTC)

    target = {
        "scope": MemoryScope.TASK,
        "owner_id": "task-a",
        "namespace": "context",
        "key": "shared-key",
    }
    memory.put(
        **target,
        value={"marker": "expired-target"},
        expires_at=baseline + timedelta(days=30),
    )
    survivors = (
        (MemoryScope.TASK, "task-a", "other", "same-owner-other-namespace"),
        (MemoryScope.TASK, "task-b", "context", "same-namespace-other-owner"),
        (MemoryScope.AGENT, "task-a", "context", "same-owner-other-scope"),
    )
    for scope, owner_id, namespace, marker in survivors:
        memory.put(
            scope=scope,
            owner_id=owner_id,
            namespace=namespace,
            key="shared-key",
            value={"marker": marker},
            expires_at=baseline + timedelta(days=60),
        )

    store, restarted = _service(db_path)
    observed_at = baseline + timedelta(days=45)

    assert restarted.get(**target, now=observed_at) is None
    with store.connection() as conn:
        remaining = conn.execute(
            "SELECT scope, owner_id, namespace, memory_key, value_json "
            "FROM memory_records ORDER BY scope, owner_id, namespace"
        ).fetchall()

    assert len(remaining) == 3
    assert {
        (row["scope"], row["owner_id"], row["namespace"], row["memory_key"])
        for row in remaining
    } == {
        (scope.value, owner_id, namespace, "shared-key")
        for scope, owner_id, namespace, _ in survivors
    }


def test_scoped_write_and_delete_do_not_cross_project_after_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "nika.db"
    _, memory = _service(db_path)
    target = {
        "scope": MemoryScope.WORKSPACE,
        "owner_id": "project-a",
        "namespace": "task-context",
        "key": "state",
    }

    memory.put(**target, value={"project": "a-v1"})
    memory.put(
        scope=MemoryScope.WORKSPACE,
        owner_id="project-a",
        namespace="other-context",
        key="state",
        value={"project": "a-other"},
    )
    memory.put(
        scope=MemoryScope.WORKSPACE,
        owner_id="project-b",
        namespace="task-context",
        key="state",
        value={"project": "b"},
    )

    _, restarted = _service(db_path)
    restarted.put(**target, value={"project": "a-v2"})

    same_owner_other_namespace = restarted.get(
        scope=MemoryScope.WORKSPACE,
        owner_id="project-a",
        namespace="other-context",
        key="state",
    )
    other_project = restarted.get(
        scope=MemoryScope.WORKSPACE,
        owner_id="project-b",
        namespace="task-context",
        key="state",
    )
    assert same_owner_other_namespace is not None
    assert same_owner_other_namespace.value == {"project": "a-other"}
    assert other_project is not None
    assert other_project.value == {"project": "b"}

    assert restarted.delete(**target) is True
    assert restarted.get(**target) is None

    same_owner_other_namespace = restarted.get(
        scope=MemoryScope.WORKSPACE,
        owner_id="project-a",
        namespace="other-context",
        key="state",
    )
    other_project = restarted.get(
        scope=MemoryScope.WORKSPACE,
        owner_id="project-b",
        namespace="task-context",
        key="state",
    )
    assert same_owner_other_namespace is not None
    assert same_owner_other_namespace.value == {"project": "a-other"}
    assert other_project is not None
    assert other_project.value == {"project": "b"}
