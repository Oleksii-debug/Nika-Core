from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import nika_core.packaging.recovery as recovery_module
from nika_core.packaging.recovery import (
    DatabaseRecoveryError,
    create_database_snapshot,
    restore_database_snapshot,
    verify_database_file_against_snapshot,
    verify_database_snapshot,
)

SHA_OLD = "1" * 40
SHA_NEW = "2" * 40


def make_db(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (13, 'now')"
        )
        connection.execute(
            "CREATE TABLE product_project_schema_migrations "
            "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO product_project_schema_migrations(version, applied_at) "
            "VALUES (3, 'now')"
        )
        connection.execute("CREATE TABLE payload (value TEXT NOT NULL)")
        connection.execute("INSERT INTO payload(value) VALUES (?)", (value,))


def read_value(path: Path) -> str:
    with sqlite3.connect(path) as connection:
        return str(connection.execute("SELECT value FROM payload").fetchone()[0])


def test_snapshot_is_consistent_immutable_and_bound_to_release(tmp_path: Path) -> None:
    database = tmp_path / "дані з пробілом" / "ніка.db"
    make_db(database, "before")
    snapshot = tmp_path / "rollback snapshots" / "old release"

    manifest = create_database_snapshot(database, snapshot, source_release_sha=SHA_OLD)
    assert manifest.source_release_sha == SHA_OLD
    assert manifest.core_schema_version == 13
    assert manifest.product_project_schema_version == 3
    assert read_value(snapshot / "database.sqlite3") == "before"

    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE payload SET value = 'after'")

    replay = create_database_snapshot(database, snapshot, source_release_sha=SHA_OLD)
    assert replay == manifest
    assert read_value(snapshot / "database.sqlite3") == "before"


def test_online_snapshot_captures_committed_wal_state(tmp_path: Path) -> None:
    database = tmp_path / "wal.db"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE payload (value TEXT NOT NULL)")
        connection.execute("INSERT INTO payload(value) VALUES ('committed-in-wal')")
        connection.commit()
        snapshot = tmp_path / "wal-snapshot"
        create_database_snapshot(database, snapshot, source_release_sha=SHA_OLD)
        assert read_value(snapshot / "database.sqlite3") == "committed-in-wal"


def test_snapshot_tampering_fails_before_restore(tmp_path: Path) -> None:
    database = tmp_path / "source.db"
    make_db(database, "safe")
    snapshot = tmp_path / "snapshot"
    create_database_snapshot(database, snapshot, source_release_sha=SHA_OLD)
    backup = snapshot / "database.sqlite3"
    backup.write_bytes(backup.read_bytes() + b"tamper")

    with pytest.raises(DatabaseRecoveryError, match="size does not match"):
        verify_database_snapshot(snapshot, expected_source_release_sha=SHA_OLD)


def test_manifest_unknown_fields_and_bool_integer_fail_closed(tmp_path: Path) -> None:
    database = tmp_path / "source.db"
    make_db(database, "safe")
    snapshot = tmp_path / "snapshot"
    create_database_snapshot(database, snapshot, source_release_sha=SHA_OLD)
    manifest_path = snapshot / "snapshot-manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["database_size"] = True
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(DatabaseRecoveryError, match="database_size"):
        verify_database_snapshot(snapshot)

    payload["database_size"] = 1
    payload["unexpected"] = "field"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(DatabaseRecoveryError, match="fields do not match"):
        verify_database_snapshot(snapshot)


def test_restore_requires_preserving_replaced_database(tmp_path: Path) -> None:
    database = tmp_path / "nika.db"
    make_db(database, "old")
    snapshot = tmp_path / "old"
    create_database_snapshot(database, snapshot, source_release_sha=SHA_OLD)
    database.unlink()
    make_db(database, "new")

    with pytest.raises(DatabaseRecoveryError, match="requires current release SHA"):
        restore_database_snapshot(
            snapshot,
            database,
            expected_source_release_sha=SHA_OLD,
        )
    assert read_value(database) == "new"


def test_restore_preserves_current_state_and_is_restart_idempotent(tmp_path: Path) -> None:
    database = tmp_path / "ніка core" / "state.db"
    make_db(database, "old")
    snapshot = tmp_path / "snapshots" / "before-update"
    old_manifest = create_database_snapshot(database, snapshot, source_release_sha=SHA_OLD)
    database.unlink()
    make_db(database, "new")
    preservation = tmp_path / "snapshots" / "failed-update"

    result = restore_database_snapshot(
        snapshot,
        database,
        expected_source_release_sha=SHA_OLD,
        current_release_sha=SHA_NEW,
        preserve_current_to=preservation,
    )
    assert result.restored is True
    assert result.already_restored is False
    assert result.preserved_current is not None
    assert result.preserved_current.source_release_sha == SHA_NEW
    assert read_value(database) == "old"
    assert read_value(preservation / "database.sqlite3") == "new"
    assert verify_database_file_against_snapshot(database, old_manifest)

    replay = restore_database_snapshot(
        snapshot,
        database,
        expected_source_release_sha=SHA_OLD,
        current_release_sha=SHA_NEW,
        preserve_current_to=preservation,
    )
    assert replay.restored is False
    assert replay.already_restored is True
    assert read_value(database) == "old"
    assert read_value(preservation / "database.sqlite3") == "new"


def test_restore_resumes_after_preservation_checkpoint(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    make_db(database, "old")
    snapshot = tmp_path / "old-snapshot"
    create_database_snapshot(database, snapshot, source_release_sha=SHA_OLD)
    database.unlink()
    make_db(database, "new")
    preservation = tmp_path / "new-preservation"

    create_database_snapshot(database, preservation, source_release_sha=SHA_NEW)
    result = restore_database_snapshot(
        snapshot,
        database,
        expected_source_release_sha=SHA_OLD,
        current_release_sha=SHA_NEW,
        preserve_current_to=preservation,
    )
    assert result.restored is True
    assert read_value(database) == "old"
    assert read_value(preservation / "database.sqlite3") == "new"


def test_restore_rejects_stale_preservation_checkpoint(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    make_db(database, "old")
    snapshot = tmp_path / "old-snapshot"
    create_database_snapshot(database, snapshot, source_release_sha=SHA_OLD)
    database.unlink()
    make_db(database, "stale-current")
    preservation = tmp_path / "preserved"
    create_database_snapshot(database, preservation, source_release_sha=SHA_NEW)
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE payload SET value = 'changed-after-preservation'")

    with pytest.raises(DatabaseRecoveryError, match="does not match current database"):
        restore_database_snapshot(
            snapshot,
            database,
            expected_source_release_sha=SHA_OLD,
            current_release_sha=SHA_NEW,
            preserve_current_to=preservation,
        )
    assert read_value(database) == "changed-after-preservation"


def test_restore_fails_closed_when_sqlite_sidecar_exists(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    make_db(database, "old")
    snapshot = tmp_path / "snapshot"
    create_database_snapshot(database, snapshot, source_release_sha=SHA_OLD)
    Path(f"{database}-wal").write_bytes(b"active")

    with pytest.raises(DatabaseRecoveryError, match="requires quiescent SQLite state"):
        restore_database_snapshot(
            snapshot,
            database,
            expected_source_release_sha=SHA_OLD,
            current_release_sha=SHA_NEW,
            preserve_current_to=tmp_path / "preserved",
        )


def test_restore_rejects_destination_or_preservation_inside_snapshot(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    make_db(database, "old")
    snapshot = tmp_path / "snapshot"
    create_database_snapshot(database, snapshot, source_release_sha=SHA_OLD)

    with pytest.raises(DatabaseRecoveryError, match="destination may not be inside"):
        restore_database_snapshot(
            snapshot,
            snapshot / "replacement.db",
            expected_source_release_sha=SHA_OLD,
        )

    database.unlink()
    make_db(database, "new")
    with pytest.raises(DatabaseRecoveryError, match="preservation path may not be inside"):
        restore_database_snapshot(
            snapshot,
            database,
            expected_source_release_sha=SHA_OLD,
            current_release_sha=SHA_NEW,
            preserve_current_to=snapshot / "preserve",
        )


def test_snapshot_rejects_wrong_release_identity(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    make_db(database, "old")
    snapshot = tmp_path / "snapshot"
    create_database_snapshot(database, snapshot, source_release_sha=SHA_OLD)

    with pytest.raises(DatabaseRecoveryError, match="does not match expectation"):
        verify_database_snapshot(snapshot, expected_source_release_sha=SHA_NEW)
    with pytest.raises(ValueError, match="exact 40-character"):
        create_database_snapshot(database, tmp_path / "bad", source_release_sha="deadbeef")


def test_snapshot_rejects_unexpected_directory_entries(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    make_db(database, "old")
    snapshot = tmp_path / "snapshot"
    create_database_snapshot(database, snapshot, source_release_sha=SHA_OLD)
    (snapshot / "unexpected.txt").write_text("extra", encoding="utf-8")

    with pytest.raises(DatabaseRecoveryError, match="contents do not match schema"):
        verify_database_snapshot(snapshot)


def test_restore_fails_closed_when_recovery_lock_is_already_held(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    make_db(database, "old")
    snapshot = tmp_path / "snapshot"
    create_database_snapshot(database, snapshot, source_release_sha=SHA_OLD)

    with (
        recovery_module._database_recovery_lock(database),
        pytest.raises(DatabaseRecoveryError, match="recovery operation is active"),
    ):
        restore_database_snapshot(
            snapshot,
            database,
            expected_source_release_sha=SHA_OLD,
        )
