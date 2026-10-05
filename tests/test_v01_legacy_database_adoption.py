from __future__ import annotations

import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import pytest

from nika_core.config import AppConfig
from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.multi_agent import MultiAgentStore, TeamQuota
from nika_core.reliability import legacy_database as adoption
from nika_core.reliability.backup import SQLiteRecoveryManager
from nika_core.runtime.idempotency import IdempotencyLedger


class ProcessLoss(BaseException):
    pass


def _legacy(path: Path):
    store = SQLiteStore(path)
    store.initialize()
    task = TaskQueue(store).create(workspace_id="w", agent_id="a", payload={"text": "Дані"})
    teams = MultiAgentStore(store)
    teams.create_team(
        team_id="team",
        root_member_id="root",
        root_agent_id="a",
        root_agent_version=1,
        root_thread_id="thread",
        root_grants=(),
        quota=TeamQuota(max_total_agents=3, max_parallel=2),
    )
    for name in ("worker-a", "worker-b"):
        teams.spawn_child(
            team_id="team",
            parent_id="root",
            child_id=name,
            agent_id=name,
            agent_version=1,
            thread_id=name,
            requested_grants=(),
        )
    IdempotencyLedger(store).reserve(
        operation_key="one-effect",
        task_id=task.task_id,
        operation_type="write",
        input_fingerprint="fingerprint",
    )
    with store.connection() as db:
        db.execute(
            "INSERT INTO scheduled_jobs VALUES "
            "('job', 'read', 'date', '{}', '{}', 1, 1, 1, NULL, 't', 't')"
        )
    return store, task


def _rows(path: Path, table: str):
    with closing(sqlite3.connect(path)) as db:
        return db.execute(f'SELECT * FROM "{table}"').fetchall()


@pytest.mark.parametrize("existing_empty", [False, True])
def test_adoption_preserves_authority_rows_and_restart_never_reimports(tmp_path, existing_empty):
    source = tmp_path / "Стара папка" / "data" / "nika_core.db"
    target = tmp_path / "Нові дані" / "nika_core.db"
    old, task = _legacy(source)
    before = source.read_bytes()
    tables = (
        "tasks",
        "multi_agent_teams",
        "multi_agent_members",
        "scheduled_jobs",
        "idempotency_records",
    )
    expected = {table: _rows(source, table) for table in tables}
    if existing_empty:
        SQLiteStore(target).initialize()
    adoption.prepare_default_database(target, [source])
    assert source.read_bytes() == before
    for table in tables:
        assert _rows(target, table) == expected[table]
    assert TaskQueue(SQLiteStore(target)).get(task.task_id).payload == {"text": "Дані"}
    new_task = TaskQueue(SQLiteStore(target)).create(
        workspace_id="w", agent_id="a", payload={"new": 1}
    )
    # A different launch directory still checks the original source from the receipt.
    adoption.prepare_default_database(target, [])
    assert TaskQueue(SQLiteStore(target)).get(new_task.task_id).payload == {"new": 1}
    assert len(list((target.parent / "legacy-adoption-backups").glob("*.sqlite3"))) == 2
    assert old.path.exists()


def test_online_snapshot_includes_uncheckpointed_wal(tmp_path):
    source, target = tmp_path / "old.db", tmp_path / "new" / "nika.db"
    _legacy(source)
    with closing(sqlite3.connect(source)) as live:
        live.execute("PRAGMA journal_mode=WAL")
        live.execute("PRAGMA wal_autocheckpoint=0")
        live.execute("UPDATE tasks SET payload_json = ?", ('{"from_wal":true}',))
        live.commit()
        assert source.with_name(source.name + "-wal").stat().st_size > 0
        adoption.prepare_default_database(target, [source])
        assert json.loads(_rows(target, "tasks")[0][4]) == {"from_wal": True}


@pytest.mark.parametrize(
    "conflict", ["two_sources", "canonical_state", "corrupt", "newer", "empty_file"]
)
def test_ambiguous_or_invalid_sources_never_create_empty_success(tmp_path, conflict):
    source, target = tmp_path / "old.db", tmp_path / "new" / "nika.db"
    _legacy(source)
    sources = [source]
    if conflict == "two_sources":
        second = tmp_path / "other.db"
        _legacy(second)
        sources.append(second)
    elif conflict == "canonical_state":
        _legacy(target)
    elif conflict in {"corrupt", "empty_file"}:
        source.write_bytes(b"PRIVATE_CORRUPTION_CANARY" if conflict == "corrupt" else b"")
    else:
        with closing(sqlite3.connect(source)) as db:
            db.execute("INSERT INTO schema_migrations VALUES (99, 'future')")
            db.commit()
    before = target.read_bytes() if target.exists() else None
    with pytest.raises(adoption.LegacyDatabaseConflict) as error:
        adoption.prepare_default_database(target, sources)
    assert "PRIVATE_CORRUPTION_CANARY" not in str(error.value)
    assert (target.read_bytes() if target.exists() else None) == before


@pytest.mark.parametrize("phase", ["before", "reserved_empty", "after"])
def test_process_loss_resumes_without_overwriting_newer_canonical_work(
    tmp_path, monkeypatch, phase
):
    source, target = tmp_path / "old.db", tmp_path / "new" / "nika.db"
    _legacy(source)
    original = SQLiteRecoveryManager.restore

    def interrupted(self, plan, **kwargs):
        if plan.target_path != target:
            return original(self, plan, **kwargs)
        if phase == "after":
            original(self, plan, **kwargs)
        elif phase == "reserved_empty":
            target.touch()
        raise ProcessLoss()

    monkeypatch.setattr(SQLiteRecoveryManager, "restore", interrupted)
    with pytest.raises(ProcessLoss):
        adoption.prepare_default_database(target, [source])
    pending = target.with_name(f".{target.name}.legacy-adoption.json")
    assert pending.exists()
    new_task = None
    if phase == "after":
        new_task = TaskQueue(SQLiteStore(target)).create(
            workspace_id="w", agent_id="a", payload={"after": 1}
        )
    monkeypatch.setattr(SQLiteRecoveryManager, "restore", original)
    adoption.prepare_default_database(target, [source])
    assert not pending.exists()
    assert len(_rows(target, "multi_agent_members")) == 3
    if new_task:
        assert TaskQueue(SQLiteStore(target)).get(new_task.task_id).payload == {"after": 1}


def test_changed_legacy_after_adoption_requires_recovery_decision(tmp_path):
    source, target = tmp_path / "old.db", tmp_path / "new" / "nika.db"
    store, _ = _legacy(source)
    adoption.prepare_default_database(target, [source])
    before = target.read_bytes()
    TaskQueue(store).create(workspace_id="w", agent_id="a", payload={"later": 1})
    with pytest.raises(adoption.LegacyDatabaseConflict):
        adoption.prepare_default_database(target, [])
    assert target.read_bytes() == before


def test_source_aliases_are_one_candidate(tmp_path):
    source, target = tmp_path / "old.db", tmp_path / "new" / "nika.db"
    _legacy(source)
    alias = tmp_path / "same.db"
    try:
        alias.hardlink_to(source)
    except OSError:
        pytest.skip("File system does not support hard links")
    adoption.prepare_default_database(target, [source, alias])
    adoption.prepare_default_database(target, [alias])
    assert len(_rows(target, "tasks")) == 1


def test_packaged_conflict_is_displayed_before_any_runtime_starts(monkeypatch):
    from scripts import nika_windows

    def conflict(_cls):
        raise adoption.LegacyDatabaseConflict("Потрібне відновлення даних Nika.")

    shown = []
    monkeypatch.setattr(AppConfig, "from_environment", classmethod(conflict))
    monkeypatch.setattr("nika_core.ui.startup_error.show_recovery_error", shown.append)
    monkeypatch.setattr(
        nika_windows, "build_windows_bridge", lambda *_args: pytest.fail("runtime started")
    )
    assert nika_windows.main([]) == 1
    assert shown == ["Потрібне відновлення даних Nika."]


def test_startup_lock_has_one_owner_and_is_released_on_process_scope_exit(tmp_path):
    target = tmp_path / "nika.db"
    with adoption._startup_lock(target), pytest.raises(adoption.LegacyDatabaseConflict):
        adoption.prepare_default_database(target, [])
    adoption.prepare_default_database(target, [])


@pytest.mark.parametrize("hold_open", [False, True])
def test_late_wal_writer_is_detected_inside_restore_lock(tmp_path, monkeypatch, hold_open):
    source, target = tmp_path / "old.db", tmp_path / "new" / "nika.db"
    _legacy(source)
    SQLiteStore(target).initialize()
    original = SQLiteRecoveryManager._copy_database
    injected = False
    guard_seen = False
    original_guard = adoption._require_empty_target

    def observe_guard(db):
        nonlocal guard_seen
        guard_seen = True
        return original_guard(db)

    monkeypatch.setattr(adoption, "_require_empty_target", observe_guard)

    def inject(source_path, destination, **kwargs):
        nonlocal injected
        if destination == target and kwargs.get("target_guard") is not None:
            injected = True
            with closing(sqlite3.connect(target)) as writer:
                writer.execute("PRAGMA journal_mode=WAL")
                writer.execute("PRAGMA wal_autocheckpoint=0")
                writer.execute("INSERT INTO keymap_overrides VALUES ('task.create', 'Ctrl+J', 't')")
                writer.commit()
                if hold_open:
                    return original(source_path, destination, **kwargs)
        return original(source_path, destination, **kwargs)

    monkeypatch.setattr(SQLiteRecoveryManager, "_copy_database", staticmethod(inject))
    with pytest.raises(adoption.LegacyDatabaseConflict):
        adoption.prepare_default_database(target, [source])
    assert injected
    if not hold_open:
        assert guard_seen
    assert _rows(target, "keymap_overrides") == [("task.create", "Ctrl+J", "t")]
    assert _rows(target, "tasks") == []


@pytest.mark.parametrize("alias", ["NIKA_DB_PATH", "NIKA_DATABASE_PATH"])
def test_explicit_configuration_bypasses_legacy_discovery(tmp_path, monkeypatch, alias):
    for name in ("NIKA_DB_PATH", "NIKA_DATABASE_PATH"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    requested = tmp_path / "chosen.db"
    monkeypatch.setenv(alias, str(requested))
    monkeypatch.setattr(
        adoption, "default_legacy_locations", lambda: pytest.fail("explicit path lost authority")
    )
    assert AppConfig.from_environment().database_path == requested


def test_default_packaged_startup_adopts_and_source_runtime_does_not(tmp_path, monkeypatch):
    source = tmp_path / "launch" / "data" / "nika_core.db"
    target_root = tmp_path / "user-data"
    _legacy(source)
    for name in ("NIKA_DB_PATH", "NIKA_DATABASE_PATH"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(source.parent.parent)
    monkeypatch.setattr("nika_core.config.user_data_path", lambda *_args, **_kwargs: target_root)
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    target = AppConfig.from_environment().database_path
    assert not target.exists()
    monkeypatch.setattr(sys, "frozen", True)
    assert AppConfig.from_environment().database_path == target
    assert len(_rows(target, "tasks")) == 1
