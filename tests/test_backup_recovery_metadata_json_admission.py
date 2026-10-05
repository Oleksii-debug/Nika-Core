from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.reliability.backup import BackupVerificationError, SQLiteRecoveryManager


def _manager(tmp_path: Path) -> tuple[SQLiteRecoveryManager, Path]:
    database = tmp_path / "nika.db"
    store = SQLiteStore(database)
    store.initialize()
    return SQLiteRecoveryManager(store), database


def _manifest_path(manager: SQLiteRecoveryManager, backup: Path) -> Path:
    return manager._manifest_path(backup)


def test_backup_manifest_rejects_duplicate_authority_key(tmp_path: Path) -> None:
    manager, _database = _manager(tmp_path)
    backup = tmp_path / "snapshot.db"
    manager.create_backup(backup)
    manifest = _manifest_path(manager, backup)
    encoded = manifest.read_text(encoding="utf-8")
    encoded = encoded.replace(
        '"format_version":1',
        '"format_version":2,"format_version":1',
        1,
    )
    manifest.write_text(encoded, encoding="utf-8")

    with pytest.raises(BackupVerificationError, match="JSON recovery metadata"):
        manager.verify_backup(backup)


def test_backup_manifest_rejects_boolean_format_version(tmp_path: Path) -> None:
    manager, _database = _manager(tmp_path)
    backup = tmp_path / "snapshot.db"
    manager.create_backup(backup)
    manifest = _manifest_path(manager, backup)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["format_version"] = True
    manifest.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )

    with pytest.raises(BackupVerificationError, match="manifest format"):
        manager.verify_backup(backup)


@pytest.mark.parametrize("field", ["size_bytes", "schema_version"])
def test_backup_manifest_rejects_string_numeric_authority(
    tmp_path: Path,
    field: str,
) -> None:
    manager, _database = _manager(tmp_path)
    backup = tmp_path / "snapshot.db"
    manager.create_backup(backup)
    manifest = _manifest_path(manager, backup)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload[field] = str(payload[field])
    manifest.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )

    with pytest.raises(BackupVerificationError, match="numeric fields"):
        manager.verify_backup(backup)


def test_backup_manifest_is_byte_bounded_before_decode(tmp_path: Path) -> None:
    manager, _database = _manager(tmp_path)
    backup = tmp_path / "snapshot.db"
    manager.create_backup(backup)
    manifest = _manifest_path(manager, backup)
    manifest.write_bytes(b'{"padding":"' + b"x" * (1024 * 1024) + b'"}')

    with pytest.raises(BackupVerificationError) as caught:
        manager.verify_backup(backup)

    assert caught.value.__cause__ is not None
    assert "byte limit" in str(caught.value.__cause__)


def test_backup_manifest_rejects_excessive_nesting(tmp_path: Path) -> None:
    manager, _database = _manager(tmp_path)
    backup = tmp_path / "snapshot.db"
    manager.create_backup(backup)
    manifest = _manifest_path(manager, backup)
    nested = "0"
    for _ in range(70):
        nested = '{"nested":' + nested + "}"
    manifest.write_text(nested, encoding="utf-8")

    with pytest.raises(BackupVerificationError) as caught:
        manager.verify_backup(backup)

    assert caught.value.__cause__ is not None
    assert "nesting limit" in str(caught.value.__cause__)


@pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity", "1e400"])
def test_backup_manifest_rejects_non_finite_numbers(
    tmp_path: Path,
    token: str,
) -> None:
    manager, _database = _manager(tmp_path)
    backup = tmp_path / "snapshot.db"
    manager.create_backup(backup)
    manifest = _manifest_path(manager, backup)
    payload = manifest.read_text(encoding="utf-8")
    payload = payload.replace('"format_version":1', f'"format_version":{token}', 1)
    manifest.write_text(payload, encoding="utf-8")

    with pytest.raises(BackupVerificationError) as caught:
        manager.verify_backup(backup)

    assert caught.value.__cause__ is not None
    assert "non-finite" in str(caught.value.__cause__)


def test_interrupted_restore_marker_rejects_duplicate_version_before_state_change(
    tmp_path: Path,
) -> None:
    manager, database = _manager(tmp_path)
    target = database.resolve()
    marker_path = manager._restore_marker_path(target)
    current_sha = hashlib.sha256(target.read_bytes()).hexdigest()
    marker = {
        "format_version": 1,
        "target_file": target.name,
        "stage_file": ".nika.stage.tmp",
        "stage_sha256": "1" * 64,
        "quarantine_file": "nika.quarantine.db",
        "quarantine_wal_file": "nika.quarantine.db-wal",
        "quarantine_shm_file": "nika.quarantine.db-shm",
        "current_sha256": current_sha,
        "backup_sha256": "2" * 64,
        "created_at": datetime.now(UTC).isoformat(),
    }
    encoded = json.dumps(marker, sort_keys=True, separators=(",", ":"))
    encoded = encoded.replace(
        '"format_version":1',
        '"format_version":2,"format_version":1',
        1,
    )
    marker_path.write_text(encoded, encoding="utf-8")

    with pytest.raises(BackupVerificationError, match="JSON recovery metadata"):
        manager.recover_interrupted_restore()

    assert marker_path.exists()
